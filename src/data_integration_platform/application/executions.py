"""执行实例的查询与受管取消（T-33~T-35 的接口面 + FR-57 的执行取消入口）。

**查询只返回脱敏聚合事实**：执行记录里没有原始业务行、没有凭据、没有物化后的配置 ——
那些东西本来就只在执行 Worker 的作用域里存在（见 `execution_tasks`）。这一层不需要
额外做脱敏，因为源头上就没有可脱敏的东西；`diagnosis` 里的原因文本在落库前已经过
`scrub_secrets`。

**取消是受管副作用**（FR-57 的第 9 类）：发起取消只是建一张准备单，审批通过才提交，
提交之后才由 Worker 带着租约去碰引擎。取消语义按 SPEC §8.3 诚实处理 ——
请求送到引擎不等于引擎已停，所以状态停在 `CANCELLING` 等对账确认。
"""

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from data_integration_platform.application.access import require_project_roles
from data_integration_platform.application.connections import READABLE
from data_integration_platform.application.harness.side_effect_targets import project_of_run
from data_integration_platform.application.side_effect_runtime import prepare_for
from data_integration_platform.domain.enums import ExecutionStatus
from data_integration_platform.domain.errors import DomainError, ResourceNotFound
from data_integration_platform.domain.side_effects import SideEffectOperation
from data_integration_platform.infrastructure.models import (
    ExecutionRun,
    HarnessSideEffectPreparation,
    Pipeline,
    PipelineVersion,
)

# 这些状态下引擎侧的作业还在跑，取消才有意义。
_CANCELLABLE = frozenset(
    {
        ExecutionStatus.QUEUED,
        ExecutionStatus.SUBMITTED,
        ExecutionStatus.RUNNING,
        # 已经在取消中的也接受再次申请：第一次取消可能没送达，重试是合理的，
        # 但状态机不会因为重复申请而跳步（引擎侧仍是同一次 cancel）。
        ExecutionStatus.CANCELLING,
    }
)

DEFAULT_LIST_LIMIT = 100
MAX_LIST_LIMIT = 500


async def get_run(session: AsyncSession, *, run_id: uuid.UUID, actor_id: uuid.UUID) -> ExecutionRun:
    run = await session.get(ExecutionRun, run_id)
    if run is None:
        raise ResourceNotFound("执行记录不存在")
    await _require_readable(session, run=run, actor_id=actor_id)
    return run


async def list_runs(
    session: AsyncSession,
    *,
    project_id: uuid.UUID,
    actor_id: uuid.UUID,
    status: ExecutionStatus | None = None,
    limit: int = DEFAULT_LIST_LIMIT,
) -> list[ExecutionRun]:
    """列出项目内的执行记录。

    经管道版本与管道反查项目：执行记录本身不带项目列，跨项目泄漏必须在这里挡住 ——
    「按项目过滤」而不是「查出来再筛」，后者漏一个分支就是一次越权读取。
    """
    await require_project_roles(session, project_id=project_id, actor_id=actor_id, allowed=READABLE)
    statement = (
        select(ExecutionRun)
        .join(PipelineVersion, PipelineVersion.id == ExecutionRun.pipeline_version_id)
        .join(Pipeline, Pipeline.id == PipelineVersion.pipeline_id)
        .where(Pipeline.project_id == project_id)
    )
    if status is not None:
        statement = statement.where(ExecutionRun.status == status)
    rows = await session.scalars(
        statement.order_by(ExecutionRun.created_at.desc()).limit(min(limit, MAX_LIST_LIMIT))
    )
    return list(rows.all())


async def runs_of_version(
    session: AsyncSession, *, version_id: uuid.UUID, actor_id: uuid.UUID
) -> list[ExecutionRun]:
    """某个管道版本下已有的执行记录（最新在前）。"""
    version = await session.get(PipelineVersion, version_id)
    if version is None:
        raise ResourceNotFound("管道版本不存在")
    project_id = await session.scalar(
        select(Pipeline.project_id).where(Pipeline.id == version.pipeline_id)
    )
    if project_id is None:
        raise ResourceNotFound("管道不存在")
    await require_project_roles(
        session, project_id=uuid.UUID(str(project_id)), actor_id=actor_id, allowed=READABLE
    )
    rows = await session.scalars(
        select(ExecutionRun)
        .where(ExecutionRun.pipeline_version_id == version_id)
        .order_by(ExecutionRun.created_at.desc())
    )
    return list(rows.all())


async def request_cancellation(
    session: AsyncSession,
    *,
    run_id: uuid.UUID,
    actor_id: uuid.UUID,
    reason: str,
    trace_id: str | None = None,
) -> HarnessSideEffectPreparation:
    """发起取消申请：写「为什么要取消」，生成受管准备单，**不碰引擎**。

    理由写在执行记录上而不是准备单的事实里：理由是**发起时的意图**，
    而准备单冻结的事实是「这次要动哪个作业」—— 后者必须在三个阶段的每一次重算里
    都得到同一个值，把自由文本塞进去只会给指纹多一个漂移源。
    """
    run = await session.get(ExecutionRun, run_id)
    if run is None:
        raise ResourceNotFound("执行记录不存在")
    await _require_readable(session, run=run, actor_id=actor_id, write=True)
    if run.status not in _CANCELLABLE:
        raise DomainError(f"执行当前状态为 {run.status.value}，不接受取消申请")
    if not run.seatunnel_job_id:
        raise DomainError("这次执行还没有提交到引擎，没有可取消的作业")

    run.cancel_reason = reason[:2000]
    await session.flush()
    return await prepare_for(
        session,
        operation=SideEffectOperation.EXECUTION_CANCEL,
        aggregate_id=str(run.id),
        actor_id=actor_id,
        trace_id=trace_id,
    )


async def _require_readable(
    session: AsyncSession, *, run: ExecutionRun, actor_id: uuid.UUID, write: bool = False
) -> None:
    await require_project_roles(
        session,
        project_id=await project_of_run(session, run),
        actor_id=actor_id,
        allowed=READABLE,
        write=write,
    )
