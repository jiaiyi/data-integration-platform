"""受管文件资产（T-16 / FR-09）。

上传先做**三道校验再落盘**，避免把超限文件写进磁盘后才拒绝：
大小上限 → 格式是否受支持 → 行数上限。

存储布局：`<受管根>/<project_id>/<uuid><后缀>`。保留后缀是刻意的 ——
探查阶段按后缀判定格式，改名会让 `detect_format` 认不出来。
"""

import hashlib
import uuid
from collections.abc import Sequence
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from data_integration_platform.application.access import require_project_roles
from data_integration_platform.application.audit import record_audit_event
from data_integration_platform.config import get_settings
from data_integration_platform.domain.enums import (
    AuditEventType,
    DataEndpointRole,
    ProjectRole,
)
from data_integration_platform.domain.errors import DomainError, ResourceNotFound
from data_integration_platform.infrastructure.file_formats import detect_format, inspect_file
from data_integration_platform.infrastructure.models import FileAsset
from data_integration_platform.infrastructure.probe_support import run_blocking

WRITABLE = frozenset({ProjectRole.ADMIN, ProjectRole.DATA_ENGINEER})
READABLE = frozenset(
    {ProjectRole.ADMIN, ProjectRole.DATA_ENGINEER, ProjectRole.APPROVER, ProjectRole.VIEWER}
)


class FileAssetService:
    def __init__(self, root: Path | None = None) -> None:
        self._root = root

    def root(self) -> Path:
        """受管根目录；只来自平台配置，不接受请求指定。"""
        return self._root if self._root is not None else get_settings().file_asset_root

    async def upload(
        self,
        session: AsyncSession,
        *,
        project_id: uuid.UUID,
        actor_id: uuid.UUID,
        filename: str,
        data: bytes,
        endpoint_role: DataEndpointRole = DataEndpointRole.SOURCE,
        trace_id: str | None = None,
    ) -> FileAsset:
        await require_project_roles(
            session, project_id=project_id, actor_id=actor_id, allowed=WRITABLE, write=True
        )
        settings = get_settings()
        display_name = _safe_display_name(filename)

        if not data:
            raise DomainError("上传内容为空")
        if len(data) > settings.file_asset_max_bytes:
            raise DomainError(
                f"文件超过上限 {settings.file_asset_max_bytes} 字节（实际 {len(data)} 字节）"
            )
        file_format = detect_format(display_name)
        inspection = await run_blocking(inspect_file, data, file_format=file_format, sample_limit=1)
        if inspection.row_count is not None and inspection.row_count > settings.file_asset_max_rows:
            raise DomainError(
                f"文件行数超过上限 {settings.file_asset_max_rows}（实际 {inspection.row_count}）"
            )

        suffix = Path(display_name).suffix.casefold()
        stored_name = f"{uuid.uuid4().hex}{suffix}"
        storage_ref = f"{project_id}/{stored_name}"
        target = self.root() / str(project_id) / stored_name
        await run_blocking(_write_bytes, target, data)

        asset = FileAsset(
            project_id=project_id,
            name=display_name,
            format=file_format,
            endpoint_role=endpoint_role,
            size_bytes=len(data),
            # 引擎/格式给不出行数时留空，不填 0 冒充已知（FR-61 的同一条原则）。
            row_count=inspection.row_count,
            storage_ref=storage_ref,
            checksum_sha256=hashlib.sha256(data).hexdigest(),
            created_by=actor_id,
        )
        session.add(asset)
        await session.flush()
        record_audit_event(
            session,
            event_type=AuditEventType.FILE_ASSET_UPLOADED,
            target_type="file_asset",
            project_id=project_id,
            actor_id=actor_id,
            target_id=str(asset.id),
            change_summary={
                "name": display_name,
                "format": file_format.value,
                "size_bytes": len(data),
                "row_count": inspection.row_count,
                "checksum_sha256": asset.checksum_sha256,
            },
            trace_id=trace_id,
        )
        return asset

    async def list_for_project(
        self, session: AsyncSession, *, project_id: uuid.UUID, actor_id: uuid.UUID
    ) -> Sequence[FileAsset]:
        await require_project_roles(
            session, project_id=project_id, actor_id=actor_id, allowed=READABLE
        )
        return (
            await session.scalars(
                select(FileAsset)
                .where(FileAsset.project_id == project_id)
                .order_by(FileAsset.created_at.desc())
            )
        ).all()

    async def get(
        self, session: AsyncSession, *, asset_id: uuid.UUID, actor_id: uuid.UUID
    ) -> FileAsset:
        asset = await session.get(FileAsset, asset_id)
        if asset is None:
            raise ResourceNotFound("文件资产不存在")
        await require_project_roles(
            session, project_id=asset.project_id, actor_id=actor_id, allowed=READABLE
        )
        return asset

    async def delete(
        self,
        session: AsyncSession,
        *,
        asset_id: uuid.UUID,
        actor_id: uuid.UUID,
        trace_id: str | None = None,
    ) -> None:
        asset = await self.get(session, asset_id=asset_id, actor_id=actor_id)
        await require_project_roles(
            session, project_id=asset.project_id, actor_id=actor_id, allowed=WRITABLE, write=True
        )
        await session.delete(asset)
        await run_blocking(_remove_if_exists, self.root() / asset.storage_ref)
        record_audit_event(
            session,
            event_type=AuditEventType.FILE_ASSET_DELETED,
            target_type="file_asset",
            project_id=asset.project_id,
            actor_id=actor_id,
            target_id=str(asset.id),
            change_summary={"name": asset.name, "format": asset.format.value},
            trace_id=trace_id,
        )


def _safe_display_name(filename: str) -> str:
    """校验上传文件名：不接受路径分隔符，避免写出受管目录之外。"""
    candidate = (filename or "").strip().replace("\\", "/").split("/")[-1]
    if not candidate:
        raise DomainError("上传文件名不能为空")
    if candidate in {".", ".."}:
        raise DomainError("上传文件名不合法")
    return candidate


def _write_bytes(target: Path, data: bytes) -> None:
    """在工作线程里落盘；目录按项目隔离。"""
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(data)


def _remove_if_exists(path: Path) -> None:
    path.unlink(missing_ok=True)


def asset_probe_location(asset: FileAsset) -> tuple[str, str]:
    """把资产映射成 LOCAL_FILE 探查器的（模式, 表名）=（项目目录, 存储文件名）。"""
    project_id, _, stored_name = asset.storage_ref.partition("/")
    return project_id, stored_name
