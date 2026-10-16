"""数据源连接管理（T-10 / FR-07 ~ FR-09）。

**凭据只以 `secret_ref` 引用存在**：明文交给 SecretProvider 加密后即丢弃，
本模块的返回对象、审计摘要与日志都不含明文（G-07）。

**更换凭据不是普通配置更新**：凭据轮换/迁移/销毁属受管副作用（FR-23），
必须走 T-15 的申请—审批—提交链路，因此 `update` 不接受凭据。
"""

import uuid
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from data_integration_platform.application.access import require_project_roles
from data_integration_platform.application.audit import record_audit_event
from data_integration_platform.application.secrets import (
    ConnectionCredentials,
    SecretProviderPort,
)
from data_integration_platform.domain.enums import (
    AuditEventType,
    ConnectionStatus,
    ConnectorType,
    DataEndpointRole,
    ProjectRole,
)
from data_integration_platform.domain.errors import DomainError, ResourceNotFound, VersionConflict
from data_integration_platform.infrastructure.models import DataConnection, DataProfile

WRITABLE = frozenset({ProjectRole.ADMIN, ProjectRole.DATA_ENGINEER})
READABLE = frozenset(
    {ProjectRole.ADMIN, ProjectRole.DATA_ENGINEER, ProjectRole.APPROVER, ProjectRole.VIEWER}
)

# 登记时就必须齐备的连接信息：缺了这些，后面无论怎么探查都不会成功，
# 因此在这里失败比拖到 Worker 里失败更省事。
_REQUIRED_OPTIONS: Mapping[ConnectorType, tuple[str, ...]] = {
    ConnectorType.S3_MINIO: ("bucket",),
    ConnectorType.REST_API: ("url",),
    ConnectorType.HDFS: ("base_path",),
}

# 需要主机端点的连接器；文件与 REST 由各自选项表达位置。
_REQUIRES_HOST = frozenset(
    {
        ConnectorType.MYSQL,
        ConnectorType.POSTGRESQL,
        ConnectorType.ORACLE,
        ConnectorType.DORIS,
        ConnectorType.CLICKHOUSE,
        ConnectorType.SQL_SERVER,
        ConnectorType.HIVE,
        ConnectorType.S3_MINIO,
        ConnectorType.HDFS,
    }
)


def validate_connection_target(
    *,
    connector_type: ConnectorType,
    name: str,
    host: str | None,
    options: Mapping[str, str],
) -> None:
    """登记期校验：缺什么就说什么，不让错误推迟到 Worker 里才暴露。"""
    if not name.strip():
        raise DomainError("连接名称不能为空")
    if connector_type in _REQUIRES_HOST and not (host or "").strip():
        raise DomainError(f"{connector_type.value} 连接需要填写主机地址")
    missing = [key for key in _REQUIRED_OPTIONS.get(connector_type, ()) if not options.get(key)]
    if missing:
        raise DomainError(f"{connector_type.value} 连接缺少必填选项：{', '.join(missing)}")


class ConnectionService:
    def __init__(self, secret_provider: SecretProviderPort) -> None:
        self._secrets = secret_provider

    async def create(
        self,
        session: AsyncSession,
        *,
        project_id: uuid.UUID,
        actor_id: uuid.UUID,
        name: str,
        connector_type: ConnectorType,
        endpoint_role: DataEndpointRole,
        host: str | None = None,
        port: int | None = None,
        database_name: str | None = None,
        options: Mapping[str, str] | None = None,
        credentials: ConnectionCredentials | None = None,
        trace_id: str | None = None,
    ) -> DataConnection:
        await require_project_roles(
            session, project_id=project_id, actor_id=actor_id, allowed=WRITABLE, write=True
        )
        normalized_options = dict(options or {})
        validate_connection_target(
            connector_type=connector_type,
            name=name,
            host=host,
            options=normalized_options,
        )
        existing = await session.scalar(
            select(DataConnection.id).where(
                DataConnection.project_id == project_id, DataConnection.name == name
            )
        )
        if existing is not None:
            raise VersionConflict(f"本项目已存在同名连接：{name}")

        stored_ref: str | None = None
        if credentials is not None:
            stored = await self._secrets.store(
                project_id=project_id, name=name, credentials=credentials
            )
            stored_ref = stored.secret_ref

        connection = DataConnection(
            project_id=project_id,
            name=name,
            connector_type=connector_type,
            endpoint_role=endpoint_role,
            status=ConnectionStatus.UNTESTED,
            host=host,
            port=port,
            database_name=database_name,
            options=normalized_options,
            secret_ref=stored_ref,
            created_by=actor_id,
        )
        session.add(connection)
        await session.flush()
        record_audit_event(
            session,
            event_type=AuditEventType.CONNECTION_CREATED,
            target_type="data_connection",
            project_id=project_id,
            actor_id=actor_id,
            target_id=str(connection.id),
            change_summary={
                "name": name,
                "connector_type": connector_type.value,
                "endpoint_role": endpoint_role.value,
                "host": host,
                "port": port,
                "database_name": database_name,
                "has_secret_ref": stored_ref is not None,
            },
            trace_id=trace_id,
        )
        return connection

    async def update(
        self,
        session: AsyncSession,
        *,
        connection_id: uuid.UUID,
        actor_id: uuid.UUID,
        name: str | None = None,
        host: str | None = None,
        port: int | None = None,
        database_name: str | None = None,
        options: Mapping[str, str] | None = None,
        endpoint_role: DataEndpointRole | None = None,
        trace_id: str | None = None,
    ) -> DataConnection:
        """只更新非凭据配置；改凭据必须走受管的凭据生命周期（T-15）。"""
        connection = await self._require(session, connection_id)
        await require_project_roles(
            session,
            project_id=connection.project_id,
            actor_id=actor_id,
            allowed=WRITABLE,
            write=True,
        )
        changed: dict[str, object] = {}
        if name is not None and name != connection.name:
            changed["name"] = name
            connection.name = name
        if host is not None and host != connection.host:
            changed["host"] = host
            connection.host = host
        if port is not None and port != connection.port:
            changed["port"] = port
            connection.port = port
        if database_name is not None and database_name != connection.database_name:
            changed["database_name"] = database_name
            connection.database_name = database_name
        if endpoint_role is not None and endpoint_role is not connection.endpoint_role:
            changed["endpoint_role"] = endpoint_role.value
            connection.endpoint_role = endpoint_role
        if options is not None:
            changed["options"] = sorted(options)
            connection.options = dict(options)
        validate_connection_target(
            connector_type=connection.connector_type,
            name=connection.name,
            host=connection.host,
            options=connection.options or {},
        )
        if changed:
            # 配置一变，之前的连通性结论就不再成立。
            connection.status = ConnectionStatus.UNTESTED
            connection.last_test_error = None
            await session.flush()
            record_audit_event(
                session,
                event_type=AuditEventType.CONNECTION_UPDATED,
                target_type="data_connection",
                project_id=connection.project_id,
                actor_id=actor_id,
                target_id=str(connection.id),
                change_summary=changed,
                trace_id=trace_id,
            )
        return connection

    async def update_credential_rotation_policy(
        self,
        session: AsyncSession,
        *,
        connection_id: uuid.UUID,
        actor_id: uuid.UUID,
        enabled: bool,
        max_age_days: int | None,
        warning_days: int | None,
        trace_id: str | None = None,
    ) -> DataConnection:
        """更新凭据轮换策略；只影响到期告警的推导，不碰凭据本身。

        刻意**不重置连通性状态**：策略是治理参数，不是连接配置，改它不影响连接能不能用。
        """
        connection = await self._require(session, connection_id)
        await require_project_roles(
            session,
            project_id=connection.project_id,
            actor_id=actor_id,
            allowed=WRITABLE,
            write=True,
        )
        if enabled and max_age_days is None:
            raise DomainError("启用轮换策略必须给出最大年龄（max_age_days）")
        if warning_days is not None and max_age_days is not None and warning_days >= max_age_days:
            raise DomainError("预警天数必须小于最大年龄，否则一启用就已进入预警窗口")
        connection.credential_rotation_enabled = enabled
        connection.credential_max_age_days = max_age_days
        connection.credential_warning_days = warning_days
        await session.flush()
        record_audit_event(
            session,
            event_type=AuditEventType.CONNECTION_UPDATED,
            target_type="data_connection",
            project_id=connection.project_id,
            actor_id=actor_id,
            target_id=str(connection.id),
            change_summary={
                "credential_rotation_enabled": enabled,
                "credential_max_age_days": max_age_days,
                "credential_warning_days": warning_days,
            },
            trace_id=trace_id,
        )
        return connection

    async def mark_testing(
        self, session: AsyncSession, *, connection_id: uuid.UUID, actor_id: uuid.UUID
    ) -> DataConnection:
        connection = await self._require(session, connection_id)
        await require_project_roles(
            session,
            project_id=connection.project_id,
            actor_id=actor_id,
            allowed=WRITABLE,
            write=True,
        )
        connection.status = ConnectionStatus.TESTING
        connection.last_test_error = None
        return connection

    async def record_test_result(
        self,
        session: AsyncSession,
        *,
        connection_id: uuid.UUID,
        ready: bool,
        error: str | None,
    ) -> DataConnection:
        """记录连通性结论；失败原因必须已经过脱敏，只说明原因不含密码明文（FR-08）。"""
        connection = await self._require(session, connection_id)
        connection.status = ConnectionStatus.READY if ready else ConnectionStatus.FAILED
        connection.last_test_error = None if ready else (error or "未知失败原因")[:2000]
        connection.last_tested_at = datetime.now(UTC)
        return connection

    async def list_for_project(
        self, session: AsyncSession, *, project_id: uuid.UUID, actor_id: uuid.UUID
    ) -> Sequence[DataConnection]:
        await require_project_roles(
            session, project_id=project_id, actor_id=actor_id, allowed=READABLE
        )
        return (
            await session.scalars(
                select(DataConnection)
                .where(DataConnection.project_id == project_id)
                .order_by(DataConnection.created_at.desc())
            )
        ).all()

    async def get(
        self, session: AsyncSession, *, connection_id: uuid.UUID, actor_id: uuid.UUID
    ) -> DataConnection:
        connection = await self._require(session, connection_id)
        await require_project_roles(
            session,
            project_id=connection.project_id,
            actor_id=actor_id,
            allowed=READABLE,
        )
        return connection

    async def list_profiles(
        self, session: AsyncSession, *, connection_id: uuid.UUID, actor_id: uuid.UUID
    ) -> Sequence[DataProfile]:
        connection = await self.get(session, connection_id=connection_id, actor_id=actor_id)
        return (
            await session.scalars(
                select(DataProfile)
                .where(DataProfile.connection_id == connection.id)
                .order_by(DataProfile.created_at.desc())
            )
        ).all()

    async def _require(self, session: AsyncSession, connection_id: uuid.UUID) -> DataConnection:
        connection = await session.get(DataConnection, connection_id)
        if connection is None:
            raise ResourceNotFound("数据连接不存在")
        return connection
