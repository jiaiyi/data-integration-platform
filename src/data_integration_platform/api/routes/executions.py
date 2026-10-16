"""执行实例、三阶段协议与受管取消（SPEC §6.1 / API-05 ~ API-08）。

三阶段协议在 HTTP 上的形状与它买的东西一一对应：

| 端点 | 阶段 | 为什么长这样 |
|---|---|---|
| `POST .../execution-preparations` | Prepare | 入参**只有幂等键**，其余事实服务端冻结 |
| `POST .../preparations/{id}/decisions` | Approve | 具名 + 指明槽位；自批与一人两槽被拒 |
| `POST .../preparations/{id}/commit` | Commit | **无入参**：重建事实并重算指纹 |
| `GET /execution-runs/{id}` | 查询 | 只返回聚合指标，无原始行与凭据 |

**取消走的是通用副作用入口**（FR-57 的第 9 类）：发起取消只建一张副作用准备单，
审批通过后才提交、才由 Worker 去碰引擎。所以它的审批端点在 `side_effects` 那一侧 ——
九类操作共用同一套「申请—审批—提交」，不因为它是执行域的事就给它开一条专用通道。
"""

import uuid

from fastapi import APIRouter, Depends, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from data_integration_platform.api.dependencies import ActorId, TraceId
from data_integration_platform.api.schemas import (
    CancelExecutionRequest,
    ExecutionPreparationDecisionRequest,
    ExecutionPreparationResponse,
    ExecutionRunResponse,
    PrepareExecutionRequest,
    SideEffectPreparationResponse,
)
from data_integration_platform.application.executions import (
    get_run,
    list_runs,
    request_cancellation,
    runs_of_version,
)
from data_integration_platform.application.harness.approval import decide as decide_preparation
from data_integration_platform.application.harness.commit import commit as commit_preparation
from data_integration_platform.application.harness.preparation import prepare
from data_integration_platform.application.side_effect_runtime import decide_for
from data_integration_platform.application.versions import PipelineVersionService
from data_integration_platform.celery_app import dispatch
from data_integration_platform.domain.enums import ExecutionStatus
from data_integration_platform.domain.harness import ExecutionPreparationStatus
from data_integration_platform.domain.side_effects import SideEffectOperation
from data_integration_platform.infrastructure.database import get_session
from data_integration_platform.side_effect_tasks import execute_side_effect

router = APIRouter(tags=["executions"])


@router.post(
    "/pipeline-versions/{version_id}/execution-preparations",
    response_model=ExecutionPreparationResponse,
    status_code=status.HTTP_201_CREATED,
)
async def prepare_execution(
    version_id: uuid.UUID,
    request: PrepareExecutionRequest,
    actor: ActorId,
    trace_id: TraceId,
    session: AsyncSession = Depends(get_session),
) -> ExecutionPreparationResponse:
    """Prepare：生成执行准备单，**不产生任何外部副作用**（FR-51）。"""
    async with session.begin():
        row = await prepare(
            session,
            version_id=version_id,
            actor_id=actor,
            idempotency_key=request.idempotency_key,
            trace_id=trace_id,
        )
    return ExecutionPreparationResponse.model_validate(row)


@router.get(
    "/pipeline-versions/{version_id}/execution-preparations",
    response_model=list[ExecutionPreparationResponse],
)
async def list_execution_preparations(
    version_id: uuid.UUID,
    actor: ActorId,
    session: AsyncSession = Depends(get_session),
) -> list[ExecutionPreparationResponse]:
    """某版本下的准备单（最新在前）；「待审批的在哪」是审批台的入口问题。"""
    rows = await PipelineVersionService().preparations_of(
        session, version_id=version_id, actor_id=actor
    )
    return [ExecutionPreparationResponse.model_validate(row) for row in rows]


@router.get(
    "/execution-preparations/{preparation_id}",
    response_model=ExecutionPreparationResponse,
)
async def get_execution_preparation(
    preparation_id: uuid.UUID,
    actor: ActorId,
    session: AsyncSession = Depends(get_session),
) -> ExecutionPreparationResponse:
    row = await PipelineVersionService().preparation(
        session, preparation_id=preparation_id, actor_id=actor
    )
    return ExecutionPreparationResponse.model_validate(row)


@router.post(
    "/execution-preparations/{preparation_id}/decisions",
    response_model=ExecutionPreparationResponse,
)
async def decide_execution_preparation(
    preparation_id: uuid.UUID,
    request: ExecutionPreparationDecisionRequest,
    actor: ActorId,
    trace_id: TraceId,
    session: AsyncSession = Depends(get_session),
) -> ExecutionPreparationResponse:
    """Approve：具名职责审批。自批（SOD-01）与一人占两槽（SOD-02）都会被 403 挡下。"""
    async with session.begin():
        row = await decide_preparation(
            session,
            preparation_id=preparation_id,
            actor_id=actor,
            role=request.role,
            decision=request.decision,
            reason=request.reason,
            trace_id=trace_id,
        )
    return ExecutionPreparationResponse.model_validate(row)


@router.post(
    "/execution-preparations/{preparation_id}/commit",
    response_model=ExecutionRunResponse,
)
async def commit_execution_preparation(
    preparation_id: uuid.UUID,
    actor: ActorId,
    trace_id: TraceId,
    session: AsyncSession = Depends(get_session),
) -> ExecutionRunResponse:
    """Commit：**无入参**。服务端重建事实、重算指纹、签发单次能力，
    并在同一事务里创建执行记录与待投递命令（SPEC §7.4 的关键不变量）。
    """
    async with session.begin():
        run = await commit_preparation(
            session, preparation_id=preparation_id, actor_id=actor, trace_id=trace_id
        )
    return ExecutionRunResponse.model_validate(run)


@router.get("/execution-runs/{run_id}", response_model=ExecutionRunResponse)
async def get_execution_run(
    run_id: uuid.UUID,
    actor: ActorId,
    session: AsyncSession = Depends(get_session),
) -> ExecutionRunResponse:
    """执行查询：只返回脱敏聚合指标，不含原始业务行与凭据（API-08）。"""
    row = await get_run(session, run_id=run_id, actor_id=actor)
    return ExecutionRunResponse.model_validate(row)


@router.get(
    "/projects/{project_id}/execution-runs",
    response_model=list[ExecutionRunResponse],
)
async def list_execution_runs(
    project_id: uuid.UUID,
    actor: ActorId,
    run_status: ExecutionStatus | None = Query(default=None, alias="status"),
    limit: int = Query(default=100, ge=1, le=500),
    session: AsyncSession = Depends(get_session),
) -> list[ExecutionRunResponse]:
    rows = await list_runs(
        session, project_id=project_id, actor_id=actor, status=run_status, limit=limit
    )
    return [ExecutionRunResponse.model_validate(row) for row in rows]


@router.get(
    "/pipeline-versions/{version_id}/execution-runs",
    response_model=list[ExecutionRunResponse],
)
async def list_version_execution_runs(
    version_id: uuid.UUID,
    actor: ActorId,
    session: AsyncSession = Depends(get_session),
) -> list[ExecutionRunResponse]:
    rows = await runs_of_version(session, version_id=version_id, actor_id=actor)
    return [ExecutionRunResponse.model_validate(row) for row in rows]


@router.post(
    "/execution-runs/{run_id}/cancellations",
    response_model=SideEffectPreparationResponse,
    status_code=status.HTTP_201_CREATED,
)
async def request_execution_cancellation(
    run_id: uuid.UUID,
    request: CancelExecutionRequest,
    actor: ActorId,
    trace_id: TraceId,
    session: AsyncSession = Depends(get_session),
) -> SideEffectPreparationResponse:
    """发起受管取消：**只建申请单，不碰引擎**（FR-57 的第 9 类副作用）。"""
    async with session.begin():
        preparation = await request_cancellation(
            session, run_id=run_id, actor_id=actor, reason=request.reason, trace_id=trace_id
        )
    return SideEffectPreparationResponse.model_validate(preparation)


@router.post(
    "/execution-runs/{run_id}/cancellations/decisions",
    response_model=SideEffectPreparationResponse,
)
async def decide_execution_cancellation(
    run_id: uuid.UUID,
    request: ExecutionPreparationDecisionRequest,
    actor: ActorId,
    trace_id: TraceId,
    session: AsyncSession = Depends(get_session),
) -> SideEffectPreparationResponse:
    """审批取消申请；槽位齐备即提交并投递执行 Worker。

    `run_id` 只是定位用的：真正被审批的是那张副作用准备单，槽位与冻结的事实都在它上面。
    """
    async with session.begin():
        preparation = await decide_for(
            session,
            operation=SideEffectOperation.EXECUTION_CANCEL,
            aggregate_id=str(run_id),
            actor_id=actor,
            role=request.role,
            decision=request.decision,
            reason=request.reason,
            trace_id=trace_id,
        )
        snapshot = SideEffectPreparationResponse.model_validate(preparation)
        # 提交后再读 ORM 属性会触发同步惰性加载，在 async 会话里直接抛 MissingGreenlet ——
        # 要用的主键在事务里先取出来。
        preparation_id = preparation.id
    # 判据是事实而不是请求里的意图：槽位没齐时什么都不该发生。
    if snapshot.status is ExecutionPreparationStatus.COMMITTED:
        await dispatch(execute_side_effect.delay, str(preparation_id))
    return snapshot
