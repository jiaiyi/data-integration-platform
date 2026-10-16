"""本地受管文件资产探查器（T-16 / FR-09）。

探查单位是**受管文件资产**：文件已由上传通道校验过大小与行数上限（25 MiB / 10 万行），
因此这里可以整份读取 —— 但路径必须严格限制在受管根目录之内。
"""

from collections.abc import Sequence
from pathlib import Path

from data_integration_platform.application.profiling import ProbeResult, ProbeTarget
from data_integration_platform.application.secrets import ConnectionCredentials
from data_integration_platform.config import get_settings
from data_integration_platform.domain.enums import ConnectorType
from data_integration_platform.domain.errors import DomainError, ResourceNotFound
from data_integration_platform.infrastructure.file_formats import detect_format, inspect_file
from data_integration_platform.infrastructure.probe_support import (
    cell_to_plain,
    require_relative_path,
    run_blocking,
)


class LocalFileMetadataProbe:
    connector_type = ConnectorType.LOCAL_FILE

    async def test_connection(
        self, *, target: ProbeTarget, credentials: ConnectionCredentials
    ) -> None:
        del credentials
        root = self._root(target)
        if not await run_blocking(root.is_dir):
            raise ResourceNotFound(f"受管文件目录不存在：{root}")

    async def list_tables(
        self,
        *,
        target: ProbeTarget,
        credentials: ConnectionCredentials,
        schema_name: str | None,
        limit: int,
    ) -> Sequence[str]:
        del credentials
        root = self._root(target)
        directory = (
            root if not schema_name else root / require_relative_path(schema_name, kind="子目录")
        )
        files = await run_blocking(
            lambda: sorted(p.name for p in directory.iterdir() if p.is_file())
        )
        return files[:limit]

    async def probe_table(
        self,
        *,
        target: ProbeTarget,
        credentials: ConnectionCredentials,
        schema_name: str,
        table_name: str,
        sample_limit: int,
    ) -> ProbeResult:
        del credentials
        root = self._root(target)
        relative = require_relative_path(table_name, kind="文件名")
        path = root / relative
        if not await run_blocking(path.is_file):
            raise ResourceNotFound(f"受管文件不存在：{relative}")
        settings = get_settings()
        size = await run_blocking(lambda: path.stat().st_size)
        if size > settings.file_asset_max_bytes:
            raise DomainError(f"文件超过受管上限 {settings.file_asset_max_bytes} 字节：{relative}")
        data = await run_blocking(path.read_bytes)
        file_format = detect_format(relative)
        inspection = inspect_file(data, file_format=file_format, sample_limit=sample_limit)
        return ProbeResult(
            schema_name=schema_name or ".",
            table_name=relative,
            columns=tuple(inspection.columns),
            primary_keys=(),
            statistics={
                **inspection.statistics,
                "object_bytes": size,
                "primary_key_supported": False,
            },
            sample_rows=tuple(
                {key: cell_to_plain(value) for key, value in row.items()}
                for row in inspection.sample_rows
            ),
            row_count=inspection.row_count,
        )

    def _root(self, target: ProbeTarget) -> Path:
        """受管根目录只能来自连接选项或平台配置，**不接受**请求里任意指定的绝对路径。"""
        configured = target.options.get("root")
        root = Path(configured) if configured else get_settings().file_asset_root
        return root
