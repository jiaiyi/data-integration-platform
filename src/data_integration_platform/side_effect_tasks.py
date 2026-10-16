"""通用副作用的执行 Worker（T-36 / FR-57）。

处理一条已提交的副作用准备单：取租约 → 验签 → 重算事实 → 执行 → 落终态。

**每一段各自一个事务**，这不是随意的切法：

- 开头那一段若因验签失败或事实漂移抛出去，事务回滚，**租约不会留下** ——
  否则这张准备单会永远停在「有人正在做」，谁也接手不了；
- 中间那段真正动手，用独立事务是因为它可能很慢（引擎调用、Provider 写入），
  不该把开头的行锁一直攥在手里；
- 最后一段落终态。

**进程在「动手」与「落终态」之间挂掉**时，租约到期后会被重新领走并重做一次。
所以执行器必须能重做（取消、换名、删表都是幂等的；凭据操作按各自的语义处理）。
这与执行链路的 Outbox 重投是同一套取舍：宁可重做一次，也不要留下「不知道做没做」。
"""

import os
import socket
import uuid
from datetime import UTC, datetime

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from data_integration_platform.application.harness.engine import ExecutionEnginePort
from data_integration_platform.application.harness.signing import VerifierPort
from data_integration_platform.application.masking import scrub_secrets
from data_integration_platform.application.secret_governance import OperationOutcome
from data_integration_platform.application.side_effect_runtime import (
    DorisMaintenancePort,
    apply_side_effect,
    begin_execution,
    settle,
)
from data_integration_platform.asyncio_compat import run_async
from data_integration_platform.celery_app import celery_app
from data_integration_platform.domain.errors import (
    DomainError,
    ExternalServiceUnavailable,
    SideEffectLeaseBusy,
)
from data_integration_platform.infrastructure.database import SessionFactory
from data_integration_platform.infrastructure.models import HarnessSideEffectPreparation


@celery_app.task(name="data_integration_platform.side_effect_tasks.execute_side_effect")
def execute_side_effect(preparation_id: str) -> dict[str, object]:
    """执行一条已提交的副作用；租约被别人占着就抛可重试异常。"""
    outcome = run_async(_execute(uuid.UUID(preparation_id)))
    if outcome["retryable"]:
        raise ExternalServiceUnavailable(str(outcome["reason"]))
    return outcome


async def _execute(
    preparation_id: uuid.UUID,
    *,
    session_factory: async_sessionmaker[AsyncSession] | None = None,
    engine: ExecutionEnginePort | None = None,
    doris: DorisMaintenancePort | None = None,
    verifier: VerifierPort | None = None,
    now: datetime | None = None,
) -> dict[str, object]:
    """三段式执行；除 `preparation_id` 外的参数都是测试缝。"""
    factory = session_factory if session_factory is not None else SessionFactory
    moment = now if now is not None else datetime.now(UTC)
    owner = f"side-effect:{socket.gethostname()}:{os.getpid()}"

    try:
        async with factory() as session:
            async with session.begin():
                preparation = await begin_execution(
                    session,
                    preparation_id=preparation_id,
                    owner=owner,
                    verifier=verifier,
                    now=moment,
                )
                operation = preparation.operation.value
    except SideEffectLeaseBusy as exc:
        # 并发冲突不是失败，是「现在不该做」——交给重试，**不落失败事实**。
        return _payload(preparation_id, None, OperationOutcome.failure(str(exc)), retryable=True)
    except DomainError as exc:
        # 验签不过、事实漂移、状态不对：重试多少次都是同一个结果。
        return _payload(preparation_id, None, OperationOutcome.failure(scrub_secrets(str(exc))))

    async with factory() as session:
        async with session.begin():
            row = await session.get(HarnessSideEffectPreparation, preparation_id)
            assert row is not None
            try:
                outcome = await apply_side_effect(
                    session, preparation=row, engine=engine, doris=doris, now=moment
                )
            except DomainError as exc:
                outcome = OperationOutcome.failure(scrub_secrets(str(exc)))
            except Exception as exc:  # 驱动/网络层漏网异常，同样落成失败事实
                outcome = OperationOutcome.failure(scrub_secrets(f"{type(exc).__name__}: {exc}"))

    async with factory() as session:
        async with session.begin():
            await settle(session, preparation_id=preparation_id, owner=owner, outcome=outcome)
    return _payload(preparation_id, operation, outcome)


def _payload(
    preparation_id: uuid.UUID,
    operation: str | None,
    outcome: OperationOutcome,
    *,
    retryable: bool = False,
) -> dict[str, object]:
    return {
        "preparation_id": str(preparation_id),
        "operation": operation,
        "succeeded": outcome.succeeded,
        "reason": outcome.reason,
        "retryable": retryable,
    }
