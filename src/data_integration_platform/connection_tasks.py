"""连接测试与元数据探查的 Celery 任务（T-10 ~ T-12 的执行侧）。

控制面**不直连外部数据库**（SPEC §10.3）：所有探查与连通性测试都在 Worker 进程里执行，
API 只落 PENDING/TESTING 事实并投递任务（FR-15）。

重试语义：只有 `ExternalServiceUnavailable`（外部依赖不可用）才重试，
业务规则违反重试多少次都是同一个结果 —— 这一点由 `ProbeOutcome.retryable` 决定，
**在事务提交之后**才决定是否抛出，避免回滚掉已经记录下来的失败事实。
"""

import uuid
from typing import Any

from data_integration_platform.application.probing import ProbeOutcome, ProbingService
from data_integration_platform.asyncio_compat import run_async
from data_integration_platform.celery_app import celery_app
from data_integration_platform.domain.errors import ExternalServiceUnavailable
from data_integration_platform.infrastructure.database import SessionFactory
from data_integration_platform.infrastructure.probe_registry import ProbeRegistry
from data_integration_platform.infrastructure.secret_factory import create_secret_provider


@celery_app.task(name="data_integration_platform.connection_tasks.test_data_connection")
def test_data_connection(connection_id: str) -> dict[str, Any]:
    """只读连通性测试；结论写入连接状态（FR-08）。"""
    outcome = run_async(_test_connection(uuid.UUID(connection_id)))
    _retry_when_retryable(outcome)
    return {"connection_id": connection_id, "succeeded": outcome.succeeded}


@celery_app.task(name="data_integration_platform.connection_tasks.profile_table")
def profile_table(profile_id: str, sample_limit: int | None = None) -> dict[str, Any]:
    """受限元数据探查；结果脱敏后落 Profile（FR-10 ~ FR-13）。"""
    outcome = run_async(_run_profile(uuid.UUID(profile_id), sample_limit))
    _retry_when_retryable(outcome)
    return {"profile_id": profile_id, "succeeded": outcome.succeeded}


async def _test_connection(connection_id: uuid.UUID) -> ProbeOutcome:
    async with SessionFactory() as session:
        async with session.begin():
            service = ProbingService(
                registry=ProbeRegistry(), secret_provider=create_secret_provider(session)
            )
            return await service.test_connection(session, connection_id=connection_id)


async def _run_profile(profile_id: uuid.UUID, sample_limit: int | None) -> ProbeOutcome:
    async with SessionFactory() as session:
        async with session.begin():
            service = ProbingService(
                registry=ProbeRegistry(), secret_provider=create_secret_provider(session)
            )
            return await service.run_profile(
                session, profile_id=profile_id, sample_limit=sample_limit
            )


def _retry_when_retryable(outcome: ProbeOutcome) -> None:
    """事务已提交，此时再抛出让 Celery 按策略重试是安全的。"""
    if not outcome.succeeded and outcome.retryable:
        raise ExternalServiceUnavailable(outcome.reason or "外部依赖暂时不可用")
