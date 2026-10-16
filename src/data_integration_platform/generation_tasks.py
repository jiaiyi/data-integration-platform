"""生成任务的执行侧（生成链路接线 / FR-15、FR-25 ~ FR-35）。

API 只落一条 `AgentRun` 再投递任务；**模型调用、确定性门禁、真实引擎检查与冻结
全部在 Worker 里发生** —— 与探查同一条口径：控制面不直连外部系统。

事务分三段：**短事务取上下文 → 不占事务跑状态机 → 短事务落结论**。
把模型调用套进一个事务里，会让一次几分钟的生成一直占着数据库连接，
而 `task_acks_late` 下 Worker 被拉走时那条连接还会跟着悬着。

Checkpoint 落**独立库**（`DIP_CHECKPOINT_DATABASE_URL`）：中断与恢复用的图状态
不属于业务真相，不该进业务库的治理范围。生产用 PostgreSQL Saver，测试注入内存实现。
"""

import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from langgraph.types import Command
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from data_integration_platform.agent.graph import (
    GenerationPhase,
    GenerationState,
    build_generation_graph,
    initial_state,
)
from data_integration_platform.application.engine_check import EngineCheckPort, build_gate_runner
from data_integration_platform.application.generation import GeneratedCandidate, GenerationPort
from data_integration_platform.application.generation_runs import (
    GenerationContext,
    GenerationRunService,
    RunSettlement,
)
from data_integration_platform.application.masking import scrub_secrets
from data_integration_platform.asyncio_compat import run_async
from data_integration_platform.celery_app import celery_app
from data_integration_platform.config import get_settings
from data_integration_platform.domain.enums import AgentRunStatus
from data_integration_platform.domain.errors import (
    DomainError,
    ExternalServiceUnavailable,
    ResourceNotFound,
)
from data_integration_platform.infrastructure.database import SessionFactory
from data_integration_platform.infrastructure.direct_generation_adapter import (
    create_generation_adapter,
)
from data_integration_platform.infrastructure.models import AgentRun
from data_integration_platform.infrastructure.seatunnel_engine_check import SeaTunnelEngineCheck

# Checkpoint 表结构只需建一次；每个任务都建一遍是白等一个往返。
_checkpoint_ready = False


@celery_app.task(name="data_integration_platform.generation_tasks.run_generation")
def run_generation(run_id: str, answer: str | None = None) -> dict[str, Any]:
    """跑一次生成；带 `answer` 时从同一 `thread_id` 恢复澄清中断（FR-27）。

    外部依赖不可用时**先落失败事实再抛出**：让 Celery 把任务标记为失败，
    而不是让一条永远跑不完的运行悄悄消失。
    """
    settlement = run_async(_run_generation(uuid.UUID(run_id), answer=answer))
    if settlement.retryable:
        raise ExternalServiceUnavailable(settlement.failure_reason or "生成依赖暂时不可用")
    return {
        "agent_run_id": run_id,
        "status": settlement.status.value,
        "phase": settlement.phase,
    }


async def _run_generation(
    run_id: uuid.UUID,
    *,
    answer: str | None = None,
    checkpointer: BaseCheckpointSaver[Any] | None = None,
    adapter: GenerationPort | None = None,
    engine: EngineCheckPort | None = None,
    session_factory: async_sessionmaker[AsyncSession] | None = None,
) -> RunSettlement:
    """三段式执行；`checkpointer` / `adapter` / `engine` / `session_factory` 是测试缝。

    **这里不能用 `run_in_transaction`**：它内部走 `run_async` → `run_until_complete`，
    而本协程已经在那个事件循环上跑着，再进去就是「事件循环已在运行」。
    会话边界直接在这里开。
    """
    factory = session_factory if session_factory is not None else SessionFactory
    async with factory() as session:
        async with session.begin():
            context = await _prepare(session, run_id)
    if context is None:
        # 重复投递或运行已结束：静默跳过而不是重跑一遍模型。
        return RunSettlement(
            phase=GenerationPhase.FAILED.value,
            status=AgentRunStatus.FAILED,
            candidates=0,
            repairs=0,
            failure_reason="生成记录不在可执行状态，已跳过",
        )

    try:
        settlement = await _invoke(
            context,
            answer=answer,
            checkpointer=checkpointer,
            adapter=adapter,
            engine=engine,
        )
    except DomainError as exc:
        settlement = RunSettlement(
            phase=GenerationPhase.FAILED.value,
            status=AgentRunStatus.FAILED,
            candidates=0,
            repairs=0,
            failure_reason=scrub_secrets(str(exc)),
            retryable=isinstance(exc, ExternalServiceUnavailable),
        )
    except Exception as exc:
        # 驱动层漏网的异常（HTTP 客户端、Checkpointer 的实现细节）**同样要先落事实**。
        # 否则任务抛出去、事务回滚，运行会永久停在 RUNNING —— 与探查那边
        # 「失败也要先记录终态」是同一条要求。
        settlement = RunSettlement(
            phase=GenerationPhase.FAILED.value,
            status=AgentRunStatus.FAILED,
            candidates=0,
            repairs=0,
            failure_reason=scrub_secrets(f"{type(exc).__name__}: {exc}"),
            retryable=False,
        )

    async with factory() as session:
        async with session.begin():
            await GenerationRunService().settle(session, run_id=run_id, settlement=settlement)
    return settlement


async def _prepare(session: AsyncSession, run_id: uuid.UUID) -> GenerationContext | None:
    """短事务一：置 RUNNING 并取上下文。已结束/非排队态返回 `None`。"""
    run = await session.get(AgentRun, run_id)
    if run is None:
        raise ResourceNotFound("生成记录不存在")
    if run.status not in (AgentRunStatus.QUEUED, AgentRunStatus.RUNNING):
        return None
    service = GenerationRunService()
    await service.mark_running(session, run_id=run_id)
    return await service.build_context(session, run_id=run_id)


async def _invoke(
    context: GenerationContext,
    *,
    answer: str | None,
    checkpointer: BaseCheckpointSaver[Any] | None,
    adapter: GenerationPort | None,
    engine: EngineCheckPort | None,
) -> RunSettlement:
    """跑状态机。这一段**不占数据库事务**：模型可能要跑几分钟。"""
    settings = get_settings()
    resolved_adapter = adapter if adapter is not None else create_generation_adapter()
    resolved_engine = engine if engine is not None else SeaTunnelEngineCheck()

    async with _saver(checkpointer) as saver:
        graph = build_generation_graph(
            adapter=resolved_adapter,
            gate_runner=build_gate_runner(
                engine=resolved_engine, source=context.source, target=context.target
            ),
            max_repairs=settings.max_enterprise_repair_attempts,
            checkpointer=saver,
        )
        config: RunnableConfig = {"configurable": {"thread_id": context.thread_id}}
        payload: GenerationState | Command[Any] = (
            initial_state(
                requirement=context.requirement,
                source_summary=context.source_summary,
                target_summary=context.target_summary,
                profile_context=context.profile_context,
            )
            if answer is None
            else Command(resume=answer)
        )
        state = await graph.ainvoke(payload, config)
    return _settlement(state)


@asynccontextmanager
async def _saver(
    injected: BaseCheckpointSaver[Any] | None,
) -> AsyncIterator[BaseCheckpointSaver[Any]]:
    """注入的 Checkpointer 直接用；生产按需建 PostgreSQL Saver。"""
    if injected is not None:
        yield injected
        return
    global _checkpoint_ready
    async with AsyncPostgresSaver.from_conn_string(get_settings().checkpoint_database_url) as saver:
        if not _checkpoint_ready:
            await saver.setup()
            _checkpoint_ready = True
        yield saver


def _settlement(state: Any) -> RunSettlement:
    """把图状态翻译成运行结论；不在这里做业务判断，只做映射。"""
    phase = str(state.get("phase") or GenerationPhase.FAILED.value)
    candidate = _candidate_of(state)
    findings = tuple(state.get("gate_findings") or ())
    diagnostics = tuple(state.get("diagnostics") or ())
    candidates = int(state.get("candidates") or 0)
    repairs = int(state.get("repair_count") or 0)
    if phase == GenerationPhase.WAIT_FOR_REVIEW.value and candidate is not None:
        return RunSettlement(
            phase=phase,
            status=AgentRunStatus.WAITING_APPROVAL,
            candidate=candidate,
            gate_findings=findings,
            diagnostics=diagnostics,
            candidates=candidates,
            repairs=repairs,
        )
    if phase == GenerationPhase.CLARIFY.value:
        return RunSettlement(
            phase=phase,
            status=AgentRunStatus.WAITING_INPUT,
            pending_question=str(state.get("pending_question") or "") or None,
            gate_findings=findings,
            diagnostics=diagnostics,
            candidates=candidates,
            repairs=repairs,
        )
    return RunSettlement(
        phase=phase,
        status=AgentRunStatus.FAILED,
        failure_reason=(str(state.get("failure_reason") or "") or f"状态机停在非终态：{phase}"),
        gate_findings=findings,
        diagnostics=diagnostics,
        candidates=candidates,
        repairs=repairs,
    )


def _candidate_of(state: Any) -> GeneratedCandidate | None:
    plan = state.get("plan")
    hocon = state.get("hocon")
    if not plan or not hocon:
        return None
    return GeneratedCandidate.model_validate({"plan": plan, "hocon": hocon})
