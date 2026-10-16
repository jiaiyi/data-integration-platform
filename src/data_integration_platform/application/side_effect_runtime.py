"""副作用的执行侧：三道闸 + 执行器（T-36 / FR-57）。

执行一颗受管副作用的顺序是死的，跟执行链路（T-37）同款：

```
① 取执行租约（SideEffectLeaseBusy 就安全延迟重试，不记失败）
② 验签（确认这条副作用确实由持钥方授权过）
③ 重算事实（跟准备单冻结的那份比对，漂移了就不执行）
④ 真正动手
⑤ 落终态并释放租约
```

①②③ 只有在全部通过之后才会走到 ④。这不是靠注释保证的：测试用会记账的假执行器，
断言令牌不成立 / 事实漂移时**执行器的调用次数是 0**。

九类操作里，本模块直接实现的是执行侧的三类（取消、Doris 回滚、Doris 清理）——
它们没有自己的业务申请表，`execution_run` 就是它们的聚合对象。凭据侧的六类
由各自的业务服务在自己的 `execute()` 里调 `begin_execution`/`finish_execution`，
业务逻辑一行没动，多出来的是这三道闸。

**Doris 维护执行器尚未接入**：`create_doris_maintenance` 会明确报错，不静默降级 ——
与生成后端遇到未接入的 `seatunnel-cli` 同款姿态。真实的换名/删表 SQL 属 M6 的 T-45
（SPEC 的划分），本轮交付的是它的执行器接口与受管入口。
"""

import os
import socket
import uuid
from datetime import UTC, datetime
from typing import Protocol

from sqlalchemy.ext.asyncio import AsyncSession

from data_integration_platform.application.audit import record_audit_event
from data_integration_platform.application.harness import side_effects
from data_integration_platform.application.harness.engine import ExecutionEnginePort
from data_integration_platform.application.harness.side_effect_targets import (
    aggregate_type_of,
    derive_facts,
)
from data_integration_platform.application.harness.signing import SignerPort, VerifierPort
from data_integration_platform.application.secret_governance import OperationOutcome
from data_integration_platform.config import Settings, get_settings
from data_integration_platform.domain.enums import AuditEventType, ExecutionStatus
from data_integration_platform.domain.errors import DomainError, ResourceNotFound
from data_integration_platform.domain.harness import (
    ExecutionPreparationStatus,
    HarnessApprovalValue,
    HarnessRole,
)
from data_integration_platform.domain.side_effects import SideEffectOperation
from data_integration_platform.infrastructure.ed25519_signing import create_capability_verifier
from data_integration_platform.infrastructure.models import (
    ExecutionRun,
    HarnessSideEffectPreparation,
)
from data_integration_platform.infrastructure.seatunnel_execution import create_execution_engine


async def prepare_for(
    session: AsyncSession,
    *,
    operation: SideEffectOperation,
    aggregate_id: str,
    actor_id: uuid.UUID,
    settings: Settings | None = None,
    now: datetime | None = None,
    trace_id: str | None = None,
) -> HarnessSideEffectPreparation:
    """受理一次副作用申请：推导事实 → 生成准备单。

    六个凭据类操作与三个执行类操作都从这里进，因为「申请」这一步对九类是同一件事。
    幂等键由操作与聚合对象推导，不由调用方给：同一个聚合对象上的同一类操作
    只应该有一张申请单。
    """
    resolved = settings if settings is not None else get_settings()
    facts = await derive_facts(
        session, operation=operation, aggregate_id=aggregate_id, settings=resolved
    )
    return await side_effects.prepare(
        session,
        facts=facts,
        actor_id=actor_id,
        idempotency_key=side_effects.side_effect_idempotency_key(
            operation=operation,
            aggregate_type=facts.aggregate_type,
            aggregate_id=facts.aggregate_id,
        ),
        settings=resolved,
        now=now,
        trace_id=trace_id,
    )


async def decide_for(
    session: AsyncSession,
    *,
    operation: SideEffectOperation,
    aggregate_id: str,
    actor_id: uuid.UUID,
    role: HarnessRole | None,
    decision: HarnessApprovalValue,
    reason: str,
    settings: Settings | None = None,
    signer: SignerPort | None = None,
    now: datetime | None = None,
    trace_id: str | None = None,
) -> HarnessSideEffectPreparation:
    """记一条具名审批；**审批齐备即提交**。

    「审批完还要再点一次提交」对凭据类操作是多余的：审批人按下批准时，
    要动的凭据、影响面、回滚方案都已经冻在准备单上，再要一个人去点一次提交
    并不会让这件事更安全，只会让一次轮换变成两次人工往返。所以这里把
    `commit` 紧接着 `decide` 做完 —— 提交本身仍然是**独立的一步**（有自己的
    能力令牌、证据与审计事件），只是由同一个请求触发。
    """
    resolved = settings if settings is not None else get_settings()
    facts = await derive_facts(
        session, operation=operation, aggregate_id=aggregate_id, settings=resolved
    )
    preparation = await side_effects.find_for_aggregate(
        session, aggregate_type=facts.aggregate_type, aggregate_id=aggregate_id
    )
    if preparation is None:
        raise DomainError("这次操作没有对应的受管申请单，拒绝审批")
    preparation = await side_effects.decide(
        session,
        preparation_id=preparation.id,
        actor_id=actor_id,
        facts=facts,
        role=role,
        decision=decision,
        reason=reason,
        settings=resolved,
        now=now,
        trace_id=trace_id,
    )
    if preparation.status is ExecutionPreparationStatus.READY:
        preparation = await side_effects.commit(
            session,
            preparation_id=preparation.id,
            actor_id=actor_id,
            facts=facts,
            settings=resolved,
            signer=signer,
            now=now,
            trace_id=trace_id,
        )
    return preparation


def approval_verdict(preparation: HarnessSideEffectPreparation) -> bool | None:
    """审批后业务行该怎么走：`True` 通过并已提交、`False` 被驳回、`None` 槽位尚未齐备。

    第三种是必须存在的：`secret.destroy` 这类破坏性动作要两个槽位，第一个人投完票之后
    业务行不该显示「已批准」—— 它还没批准。把「投了一票」当成「已批准」会让一次
    只有一个槽位的销毁看起来可以直接执行。
    """
    if preparation.status is ExecutionPreparationStatus.REJECTED:
        return False
    if preparation.status is ExecutionPreparationStatus.COMMITTED:
        return True
    return None


async def abandon_for(
    session: AsyncSession,
    *,
    operation: SideEffectOperation,
    aggregate_id: str,
    reason: str,
    trace_id: str | None = None,
) -> None:
    """业务前置失败时把准备单一起作废。

    不这么做的话，一次验证未通过的轮换会留下一张永远停在「待审批」的申请单 ——
    审批人点进去只会被业务状态机挡住，没人知道它其实已经死了。
    """
    aggregate_type = aggregate_type_of(operation)
    preparation = await side_effects.find_for_aggregate(
        session, aggregate_type=aggregate_type, aggregate_id=aggregate_id
    )
    if preparation is not None:
        await side_effects.abandon(
            session, preparation=preparation, reason=reason, trace_id=trace_id
        )


def worker_owner() -> str:
    """执行租约的持有人标识：进程级唯一，便于事后回答「谁在跑这一条」。"""
    return f"side-effect:{socket.gethostname()}:{os.getpid()}"


async def preparation_id_for(
    session: AsyncSession, *, operation: SideEffectOperation, aggregate_id: str
) -> uuid.UUID:
    """按业务事实反查准备单；没有受管申请单就不许执行。

    这是「不存在绕过申请—审批—提交的直接执行路径」（FR-23）在代码里的落点：
    各业务的 `execute()` 第一件事就是来这里取准备单，取不到直接拒绝。
    """
    preparation = await side_effects.find_for_aggregate(
        session, aggregate_type=aggregate_type_of(operation), aggregate_id=aggregate_id
    )
    if preparation is None:
        raise DomainError("这次操作没有对应的受管申请单，拒绝执行")
    return preparation.id


async def begin_execution(
    session: AsyncSession,
    *,
    preparation_id: uuid.UUID,
    owner: str,
    settings: Settings | None = None,
    verifier: VerifierPort | None = None,
    now: datetime | None = None,
) -> HarnessSideEffectPreparation:
    """①②③：取租约 → 验签 → 重算事实。全部通过才返回，返回即可执行。

    三道闸的顺序有讲究：**租约在最前面**。反过来先验签再抢租约的话，两个 Worker
    会同时通过验签、同时开始动手，租约就白设了。
    """
    resolved = settings if settings is not None else get_settings()
    moment = now if now is not None else datetime.now(UTC)
    preparation = await side_effects.claim_execution(
        session, preparation_id=preparation_id, owner=owner, settings=resolved, now=moment
    )
    await side_effects.verify_grant(
        session,
        preparation=preparation,
        verifier=verifier if verifier is not None else create_capability_verifier(),
        now=moment,
    )
    facts = await derive_facts(
        session,
        operation=preparation.operation,
        aggregate_id=preparation.aggregate_id,
        settings=resolved,
    )
    # 漂移了就抛出去 —— 但租约已经拿在手里，所以调用方必须落一次终态（`finish_execution`），
    # 否则这张准备单会一直卡在「有人正在做」上，谁也接手不了。`execute_side_effect` 负责这件事。
    await side_effects.require_not_drifted(session, preparation=preparation, facts=facts)
    return preparation


async def settle(
    session: AsyncSession,
    *,
    preparation_id: uuid.UUID,
    owner: str,
    outcome: OperationOutcome,
    trace_id: str | None = None,
) -> HarnessSideEffectPreparation:
    """⑤：落终态并释放租约。成功与失败都要落 —— 失败也是一种结论。"""
    return await side_effects.finish_execution(
        session,
        preparation_id=preparation_id,
        owner=owner,
        succeeded=outcome.succeeded,
        failure_reason=outcome.reason,
        trace_id=trace_id,
    )


# ------------------------------------------------------------------ 执行器接口
class DorisMaintenancePort(Protocol):
    """Doris 维护动作（换名 / 删影子表）。

    单独定义成 Port 而不是直接连库：真实 SQL 属 M6 的 T-45，本轮的交付物是
    「受管入口 + 执行器接口」，接口先定下来，T-45 填实现、不改调用方。
    """

    async def atomic_swap(self, *, target_table: str, shadow_table: str) -> None: ...

    async def drop_shadow_table(self, *, table: str) -> None: ...


def create_doris_maintenance() -> DorisMaintenancePort:
    """未接入就明确报错。

    返回一个「什么都不做但看起来成功」的假实现会更糟：那会让一次回滚在证据上成功、
    在数据上没发生 —— 而回滚恰恰是最不能假装成功的动作。
    """
    raise DomainError(
        "Doris 维护执行器尚未接入：回滚与清理的受管入口已经就绪，真实换名/删表由 M6 的 T-45 交付"
    )


async def apply_side_effect(
    session: AsyncSession,
    *,
    preparation: HarnessSideEffectPreparation,
    engine: ExecutionEnginePort | None = None,
    doris: DorisMaintenancePort | None = None,
    now: datetime | None = None,
    trace_id: str | None = None,
) -> OperationOutcome:
    """按操作分发到执行器；只覆盖没有专属业务申请表的执行侧三类。每个操作一行，
    不做「通用执行器框架」——九类里只有三类没有自己的业务执行路径。"""
    match preparation.operation:
        case SideEffectOperation.EXECUTION_CANCEL:
            return await cancel_execution(
                session, preparation=preparation, engine=engine, now=now, trace_id=trace_id
            )
        case SideEffectOperation.DORIS_ROLLBACK:
            return await _atomic_swap(preparation=preparation, doris=doris)
        case SideEffectOperation.DORIS_CLEANUP:
            return await _drop_shadow(preparation=preparation, doris=doris)
        case _:
            # 凭据类六种走各自业务服务的 `execute()`，不从这里分发 ——
            # 它们有自己的申请表与状态机，硬塞进来只会多一层转接。
            raise DomainError(
                f"{preparation.operation.value} 的执行由对应业务服务负责，不在通用执行器里"
            )


async def cancel_execution(
    session: AsyncSession,
    *,
    preparation: HarnessSideEffectPreparation,
    engine: ExecutionEnginePort | None = None,
    now: datetime | None = None,
    trace_id: str | None = None,
) -> OperationOutcome:
    """把取消请求送到引擎。

    **取消请求成功 != 引擎已停**（SPEC §8.3）：这里只把运行推进到 `CANCELLING`，
    确认引擎真的停下之后才进 `CANCELLED` —— 那一步靠对账，不靠这一句返回。
    """
    moment = now if now is not None else datetime.now(UTC)
    run = await session.get(ExecutionRun, _uuid(preparation.aggregate_id))
    if run is None:
        raise ResourceNotFound("执行记录不存在")
    if not run.seatunnel_job_id:
        return OperationOutcome.failure("这次执行没有引擎作业 id，无法取消")

    resolved_engine = engine if engine is not None else create_execution_engine()
    await resolved_engine.cancel(job_id=run.seatunnel_job_id)

    run.status = ExecutionStatus.CANCELLING
    # 记的是「请求送达引擎」的时刻，不是申请的时刻 —— 申请可能还压在审批里。
    # `cancel_reason` 由发起申请时写入（`request_cancellation`），这里不动它。
    run.cancel_requested_at = moment
    await session.flush()
    record_audit_event(
        session,
        event_type=AuditEventType.EXECUTION_CANCEL_REQUESTED,
        target_type="execution_run",
        target_id=str(run.id),
        change_summary={
            "seatunnel_job_id": run.seatunnel_job_id,
            "status": run.status.value,
            "preparation_id": str(preparation.id),
        },
        trace_id=trace_id,
    )
    return OperationOutcome.success()


async def _atomic_swap(
    *,
    preparation: HarnessSideEffectPreparation,
    doris: DorisMaintenancePort | None,
) -> OperationOutcome:
    target = str(preparation.fact_summary.get("target_table") or "")
    shadow = str(preparation.fact_summary.get("shadow_table") or "")
    if not target or not shadow:
        return OperationOutcome.failure("准备单没有冻结目标表与影子表，拒绝回滚")
    resolved = doris if doris is not None else create_doris_maintenance()
    await resolved.atomic_swap(target_table=target, shadow_table=shadow)
    return OperationOutcome.success()


async def _drop_shadow(
    *,
    preparation: HarnessSideEffectPreparation,
    doris: DorisMaintenancePort | None,
) -> OperationOutcome:
    shadow = str(preparation.fact_summary.get("shadow_table") or "")
    if not shadow:
        return OperationOutcome.failure("准备单没有冻结影子表名，拒绝清理")
    resolved = doris if doris is not None else create_doris_maintenance()
    await resolved.drop_shadow_table(table=shadow)
    return OperationOutcome.success()


def _uuid(value: str) -> uuid.UUID:
    try:
        return uuid.UUID(value)
    except ValueError as exc:
        raise DomainError(f"聚合对象标识不是合法的 UUID：{value!r}") from exc
