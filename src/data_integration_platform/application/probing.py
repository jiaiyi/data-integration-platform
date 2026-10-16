"""连通性测试与元数据探查的编排（T-10 ~ T-12 的调用侧）。

这一层只做**编排**：取凭据 → 组装受管输入 → 交给注册表里的 Adapter → 记录事实。
SQL 方言与驱动细节全部留在 Adapter 里。

失败处理有一条硬要求：**任何异常都必须先落成事实再决定是否重试**。
否则一旦向上抛出，任务事务回滚，连接会永久停在 `TESTING`、Profile 会永久停在 `RUNNING`。
因此这里捕获异常、记录终态、返回结果对象，由调用方在**事务提交之后**再决定重试。
"""

import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Protocol

from sqlalchemy.ext.asyncio import AsyncSession

from data_integration_platform.application.audit import record_audit_event
from data_integration_platform.application.connections import ConnectionService
from data_integration_platform.application.masking import scrub_secrets
from data_integration_platform.application.profiling import (
    ProbeRegistryPort,
    ProbeTarget,
    ProfileService,
    connection_is_probeable,
)
from data_integration_platform.application.secrets import (
    ConnectionCredentials,
    SecretProviderPort,
)
from data_integration_platform.config import get_settings
from data_integration_platform.domain.enums import AuditEventType, DataProfileStatus
from data_integration_platform.domain.errors import DomainError, ExternalServiceUnavailable
from data_integration_platform.infrastructure.models import DataConnection, DataProfile


class CredentialVerifier(Protocol):
    """「用给定凭据验一次连通性」的能力。

    由 `ProbingService` 实现。定义成端口而不是让调用方直接依赖 `ProbingService`，
    是为了让轮换验证既能跑真库、也能注入假实现。
    """

    async def verify_credentials(
        self, *, connection: DataConnection, credentials: ConnectionCredentials
    ) -> "ProbeOutcome":
        """只读连通性验证；不读写连接的任何状态。"""
        ...


@dataclass(frozen=True, slots=True)
class ProbeOutcome:
    """一次探查的结论；调用方据 `retryable` 在**事务提交之后**决定是否重试。"""

    succeeded: bool
    retryable: bool
    reason: str | None = None

    @classmethod
    def success(cls) -> "ProbeOutcome":
        return cls(succeeded=True, retryable=False)

    @classmethod
    def failure(cls, reason: str, *, retryable: bool) -> "ProbeOutcome":
        return cls(succeeded=False, retryable=retryable, reason=reason)


class ProbingService:
    def __init__(self, *, registry: ProbeRegistryPort, secret_provider: SecretProviderPort) -> None:
        self._registry = registry
        self._secrets = secret_provider
        self._connections = ConnectionService(secret_provider)

    async def test_connection(
        self, session: AsyncSession, *, connection_id: uuid.UUID
    ) -> ProbeOutcome:
        """只读连通性测试；结论落库为连接状态（FR-08）。

        `TESTING` 由 API 侧在投递任务前同步置位，Worker 只负责记录终态 ——
        这样 Worker 不需要冒充一个操作者身份去做权限校验（授权在 API 侧已完成）。
        """
        connection = await self._require_connection(session, connection_id)
        credentials = await self._load_credentials(connection)
        try:
            await self._registry.get(connection.connector_type).test_connection(
                target=_target(connection), credentials=credentials
            )
        except DomainError as exc:
            reason = scrub_secrets(str(exc))
            await self._record_test(session, connection, ready=False, reason=reason)
            return ProbeOutcome.failure(reason, retryable=_is_retryable(exc))
        except Exception as exc:  # 驱动层漏网的异常：同样先落事实，不留悬挂状态
            reason = scrub_secrets(f"{type(exc).__name__}: {exc}")
            await self._record_test(session, connection, ready=False, reason=reason)
            return ProbeOutcome.failure(reason, retryable=False)
        await self._record_test(session, connection, ready=True, reason=None)
        return ProbeOutcome.success()

    async def run_profile(
        self,
        session: AsyncSession,
        *,
        profile_id: uuid.UUID,
        sample_limit: int | None = None,
    ) -> ProbeOutcome:
        """执行一次元数据探查并落 Profile；原始样本只在内存中停留，落库前统一脱敏。"""
        profiles = ProfileService()
        await profiles.mark_running(session, profile_id=profile_id)
        profile = await session.get(DataProfile, profile_id)
        if profile is None:
            return ProbeOutcome.failure("Profile 不存在", retryable=False)
        connection = await self._require_connection(session, profile.connection_id)
        settings = get_settings()
        limit = sample_limit or settings.probe_sample_limit_default
        credentials = await self._load_credentials(connection)
        try:
            connection_is_probeable(connection)
            result = await self._registry.get(connection.connector_type).probe_table(
                target=_target(connection),
                credentials=credentials,
                schema_name=profile.schema_name,
                table_name=profile.table_name,
                sample_limit=limit,
            )
        except DomainError as exc:
            reason = scrub_secrets(str(exc))
            await profiles.fail(session, profile_id=profile_id, error=reason)
            return ProbeOutcome.failure(reason, retryable=_is_retryable(exc))
        except Exception as exc:
            reason = scrub_secrets(f"{type(exc).__name__}: {exc}")
            await profiles.fail(session, profile_id=profile_id, error=reason)
            return ProbeOutcome.failure(reason, retryable=False)

        await profiles.complete(session, profile_id=profile_id, result=result, sample_limit=limit)
        record_audit_event(
            session,
            event_type=AuditEventType.PROFILE_COMPLETED,
            target_type="data_profile",
            project_id=connection.project_id,
            target_id=str(profile_id),
            change_summary={
                "schema_name": result.schema_name,
                "table_name": result.table_name,
                "columns": len(result.columns),
                "sample_size": min(len(result.sample_rows), limit),
                "status": DataProfileStatus.READY.value,
            },
        )
        return ProbeOutcome.success()

    async def verify_credentials(
        self,
        *,
        connection: DataConnection,
        credentials: ConnectionCredentials,
    ) -> ProbeOutcome:
        """用**给定**凭据做一次只读连通性验证，不读写连接的任何状态。

        轮换的「暂存 → 验证」阶段靠它：新凭据必须先证明可用，才允许进入审批（FR-19）。
        在这里失败只返回结果，不影响连接当前正在使用的凭据。
        """
        try:
            await self._registry.get(connection.connector_type).test_connection(
                target=_target(connection), credentials=credentials
            )
        except DomainError as exc:
            reason = scrub_secrets(str(exc))
            return ProbeOutcome.failure(reason, retryable=_is_retryable(exc))
        except Exception as exc:
            return ProbeOutcome.failure(
                scrub_secrets(f"{type(exc).__name__}: {exc}"), retryable=False
            )
        return ProbeOutcome.success()

    async def _record_test(
        self,
        session: AsyncSession,
        connection: DataConnection,
        *,
        ready: bool,
        reason: str | None,
    ) -> None:
        await self._connections.record_test_result(
            session, connection_id=connection.id, ready=ready, error=reason
        )
        record_audit_event(
            session,
            event_type=AuditEventType.CONNECTION_TESTED,
            target_type="data_connection",
            project_id=connection.project_id,
            target_id=str(connection.id),
            change_summary={"ready": ready, "reason": reason},
        )

    async def _load_credentials(self, connection: DataConnection) -> ConnectionCredentials:
        """无凭据的连接（本地文件、无鉴权 REST）返回空凭据，而不是报错。"""
        if not connection.secret_ref:
            return ConnectionCredentials()
        return await self._secrets.load(connection.secret_ref)

    async def _require_connection(
        self, session: AsyncSession, connection_id: uuid.UUID
    ) -> DataConnection:
        connection = await session.get(DataConnection, connection_id)
        if connection is None:
            raise DomainError("数据连接不存在")
        return connection


def _is_retryable(exc: DomainError) -> bool:
    """只有外部依赖失败值得重试；业务规则违反重试多少次都是同样的结果。"""
    return isinstance(exc, ExternalServiceUnavailable)


def _target(connection: DataConnection) -> ProbeTarget:
    """把连接行映射成受管探查输入；**不含凭据**，以免进入日志与指纹。"""
    return ProbeTarget(
        connector_type=connection.connector_type,
        host=connection.host,
        port=connection.port,
        database_name=connection.database_name,
        options=_string_options(connection.options or {}),
    )


def _string_options(options: Mapping[str, object]) -> dict[str, str]:
    return {str(key): str(value) for key, value in options.items()}
