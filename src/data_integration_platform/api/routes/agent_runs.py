"""生成任务接口（SPEC §6.1、API-02 / API-03）。

API 侧**不跑模型**：只落一条 `AgentRun` 事实再投递任务，真正的生成、门禁与冻结
都在 Worker 里发生（与探查同一条口径，FR-15）。

投递统一走 `celery_app.dispatch` 卸载到线程：`.delay()` 是同步阻塞调用，
在 async 路由里直接调会把整个事件循环卡住（实测踩过）。
"""

import uuid

from fastapi import APIRouter, Depends, status
from sqlalchemy.ext.asyncio import AsyncSession

from data_integration_platform.api.dependencies import ActorId, TraceId
from data_integration_platform.api.schemas import (
    AgentRunResponse,
    AnswerAgentRunRequest,
    StartAgentRunRequest,
)
from data_integration_platform.application.generation_runs import GenerationRunService
from data_integration_platform.celery_app import dispatch
from data_integration_platform.generation_tasks import run_generation
from data_integration_platform.infrastructure.database import get_session

router = APIRouter(tags=["agent-runs"])


@router.post(
    "/pipeline-versions/{version_id}/agent-runs",
    response_model=AgentRunResponse,
    status_code=status.HTTP_201_CREATED,
)
async def start_agent_run(
    version_id: uuid.UUID,
    request: StartAgentRunRequest,
    actor: ActorId,
    trace_id: TraceId,
    session: AsyncSession = Depends(get_session),
) -> AgentRunResponse:
    """发起生成（API-02）。`status=WAITING_INPUT` 表示模型在反问，用回答接口补充（API-03）。"""
    async with session.begin():
        run = await GenerationRunService().start(
            session,
            version_id=version_id,
            actor_id=actor,
            profile_ids=request.profile_ids,
            quality_contract=request.quality_contract,
            incremental_contract=request.incremental_contract,
            row_llm_contract=request.row_llm_contract,
            trace_id=trace_id,
        )
    # 事务已提交再投递：投递成功而事务回滚会留下一条永远等不到上下文的运行。
    await dispatch(run_generation.delay, str(run.id))
    return AgentRunResponse.model_validate(run)


@router.post("/agent-runs/{run_id}/answers", response_model=AgentRunResponse)
async def answer_agent_run(
    run_id: uuid.UUID,
    request: AnswerAgentRunRequest,
    actor: ActorId,
    session: AsyncSession = Depends(get_session),
) -> AgentRunResponse:
    """回答澄清问题（API-03）：从**同一 `thread_id`** 恢复，不重跑（FR-27）。

    答案随任务载荷走到 Worker 并交给 `Command(resume=...)`，不落业务库 ——
    生成过程的中间状态归 Checkpoint 库管。
    """
    async with session.begin():
        run = await GenerationRunService().answer(
            session, run_id=run_id, actor_id=actor, answer=request.answer
        )
    await dispatch(run_generation.delay, str(run.id), request.answer)
    return AgentRunResponse.model_validate(run)


@router.get("/agent-runs/{run_id}", response_model=AgentRunResponse)
async def get_agent_run(
    run_id: uuid.UUID,
    actor: ActorId,
    session: AsyncSession = Depends(get_session),
) -> AgentRunResponse:
    """查询生成记录：阶段、候选与修复计数（共享同一份预算，FR-31）、诊断。"""
    run = await GenerationRunService().get(session, run_id=run_id, actor_id=actor)
    return AgentRunResponse.model_validate(run)
