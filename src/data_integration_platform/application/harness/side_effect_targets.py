"""九类副作用「动什么」的**唯一**推导入口（T-36 / FR-57）。

每个操作一份推导函数，读的都是**业务真相**（业务申请行、执行记录、管道版本），
不读请求体。Prepare、审批前校验、Commit、执行前校验全部经 `derive_facts` 走同一份函数：
三处各推一遍的话，任何一处漏了某个输入都会让指纹在两个阶段之间漂移，
而漂移看起来像「事实被改过」，排查方向全错（这是执行链路已经踩过的坑）。

**这里不做任何 I/O**：只查库、只做纯计算。Prepare 的验收口径是「外部系统零副作用」，
所以推导函数里不许出现 HTTP 客户端、Secret Provider 与引擎适配器。

关于凭据类事实里的引用：只记 **Provider 名与对象标识**，不记完整 `secret_ref`。
引用本身不是明文凭据，但它带路径信息，而审计与证据面里没有任何理由需要它。

关于**数据分级**：凭据类操作的对象就是凭据，按 `SECRET` 记 —— 这不是保守猜测，是事实。
执行取消与 Doris 回滚/清理不读写业务数据，但平台也无法从摘要反推被处理数据的分级
（准备单里存的是 `data_classification_sha256`，摘要不可逆），因此按 `UNKNOWN` 记：
比已知受限更该被盯着，又不会让任何未分类数据凭空出不了门（见 HANDOVER §6.7）。

关于**轮换的三类**：一个轮换申请就是一个聚合对象，其准备单的 `operation` 记
`SECRET_ROTATION_STAGE`（流水线的第一段）。把一次轮换拆成三张准备单会让一次人工审批
变成三次，而三者动的是同一把凭据、同一个连接、同一份事实。三段的**执行**仍然分先后，
由执行器按 暂存 → 验证 → 激活 串起来。
"""

import uuid
from collections.abc import Awaitable, Callable, Mapping
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from data_integration_platform.application.harness.side_effects import SideEffectFacts
from data_integration_platform.config import Settings
from data_integration_platform.domain.errors import DomainError, ResourceNotFound
from data_integration_platform.domain.harness import (
    DataSensitivity,
    ResourceScope,
    ToolEffect,
    ToolIntent,
)
from data_integration_platform.domain.side_effects import SideEffectOperation
from data_integration_platform.infrastructure.models import (
    DataConnection,
    ExecutionPreparation,
    ExecutionRun,
    Pipeline,
    PipelineVersion,
    SecretDestructionRequest,
    SecretMigrationRequest,
    SecretRotationRequest,
    SecretRotationRollbackRequest,
)
from data_integration_platform.infrastructure.secret_factory import provider_name_of

# 聚合对象类型：准备单用它 + `aggregate_id` 反查业务事实。
AGGREGATE_EXECUTION_RUN = "execution_run"
AGGREGATE_SECRET_MIGRATION = "secret_migration_request"
AGGREGATE_SECRET_ROTATION = "secret_rotation_request"
AGGREGATE_SECRET_ROTATION_ROLLBACK = "secret_rotation_rollback_request"
AGGREGATE_SECRET_DESTRUCTION = "secret_destruction_request"

# 工具名的**唯一**登记处：效果只能从这里取，推导函数不许自己填。
# 版本为 `None` 表示「随部署走」——只有引擎类工具会这样，平台自己的工具版本写死。
ENGINE_VERSION = None

_TOOLS: Mapping[str, tuple[str | None, ToolEffect]] = {
    "seatunnel.cancel_job": (ENGINE_VERSION, ToolEffect.WRITE),
    "doris.atomic_swap": ("3.0.8", ToolEffect.DESTRUCTIVE),
    "doris.drop_shadow_table": ("3.0.8", ToolEffect.DESTRUCTIVE),
    "secret.migrate": ("1.0.0", ToolEffect.SECRET_USE),
    "secret.rotate": ("1.0.0", ToolEffect.SECRET_USE),
    "secret.rollback_rotation": ("1.0.0", ToolEffect.SECRET_USE),
    "secret.destroy": ("1.0.0", ToolEffect.DESTRUCTIVE),
}

Deriver = Callable[[AsyncSession, str, Settings], Awaitable[SideEffectFacts]]


async def derive_facts(
    session: AsyncSession,
    *,
    operation: SideEffectOperation,
    aggregate_id: str,
    settings: Settings,
) -> SideEffectFacts:
    """九类操作共用的推导入口；未登记的操作直接拒绝，不做「默认怎么推」。"""
    deriver = _DERIVERS.get(operation)
    if deriver is None:
        raise DomainError(f"未登记副作用操作 {operation.value}，无法推导事实")
    return await deriver(session, aggregate_id, settings)


def aggregate_type_of(operation: SideEffectOperation) -> str:
    aggregate_type = _AGGREGATE_TYPES.get(operation)
    if aggregate_type is None:
        raise DomainError(f"未登记副作用操作 {operation.value}")
    return aggregate_type


# ------------------------------------------------------------------ 执行侧三类
async def _cancel_job_facts(
    session: AsyncSession, aggregate_id: str, settings: Settings
) -> SideEffectFacts:
    """取消一次执行。**不碰引擎**：这里只冻结「要取消哪个作业」。"""
    run, preparation = await _require_run(session, aggregate_id)
    if not run.seatunnel_job_id:
        raise DomainError("这次执行还没有提交到引擎，没有可取消的作业")
    project_id = await project_of_run(session, run)
    return SideEffectFacts(
        operation=SideEffectOperation.EXECUTION_CANCEL,
        project_id=project_id,
        aggregate_type=AGGREGATE_EXECUTION_RUN,
        aggregate_id=str(run.id),
        intent=_intent(
            name="seatunnel.cancel_job",
            project_id=project_id,
            objects=(f"execution_run:{run.id}",),
            sensitivity=DataSensitivity.UNKNOWN,
            settings=settings,
        ),
        fact_summary={
            "run_id": str(run.id),
            "seatunnel_job_id": run.seatunnel_job_id,
            "pipeline_version_id": str(run.pipeline_version_id),
            "tool_version": preparation.tool_version,
        },
        impact_report={
            "objects": [f"execution_run:{run.id}"],
            "seatunnel_job_id": run.seatunnel_job_id,
            "run_status": run.status.value,
            "note": "取消请求成功 != 引擎已停：状态先到 CANCELLING，确认后才进 CANCELLED",
        },
        rollback_plan=_no_compensation("取消本身是补偿动作，没有第二层补偿"),
    )


async def _doris_rollback_facts(
    session: AsyncSession, aggregate_id: str, settings: Settings
) -> SideEffectFacts:
    """回滚一次发布的换名：把影子表换回成正式表。"""
    run, preparation = await _require_run(session, aggregate_id)
    target, shadow = _shadow_of(preparation)
    project_id = await project_of_run(session, run)
    return SideEffectFacts(
        operation=SideEffectOperation.DORIS_ROLLBACK,
        project_id=project_id,
        aggregate_type=AGGREGATE_EXECUTION_RUN,
        aggregate_id=str(run.id),
        intent=_intent(
            name="doris.atomic_swap",
            project_id=project_id,
            objects=(target, shadow),
            sensitivity=DataSensitivity.UNKNOWN,
            settings=settings,
        ),
        fact_summary={
            "run_id": str(run.id),
            "target_table": target,
            "shadow_table": shadow,
            "strategy": preparation.rollback_plan.get("strategy"),
        },
        impact_report={
            "objects": sorted([target, shadow]),
            "target_table": target,
            "shadow_table": shadow,
            "note": "用影子表换回正式表名；不改写原执行记录（FR-73）",
        },
        rollback_plan=_no_compensation("回滚本身是补偿动作，没有第二层补偿"),
    )


async def _doris_cleanup_facts(
    session: AsyncSession, aggregate_id: str, settings: Settings
) -> SideEffectFacts:
    """清理影子表：释放未发布或已回滚后残留的资源。"""
    run, preparation = await _require_run(session, aggregate_id)
    target, shadow = _shadow_of(preparation)
    project_id = await project_of_run(session, run)
    return SideEffectFacts(
        operation=SideEffectOperation.DORIS_CLEANUP,
        project_id=project_id,
        aggregate_type=AGGREGATE_EXECUTION_RUN,
        aggregate_id=str(run.id),
        intent=_intent(
            name="doris.drop_shadow_table",
            project_id=project_id,
            objects=(shadow,),
            sensitivity=DataSensitivity.UNKNOWN,
            settings=settings,
        ),
        fact_summary={
            "run_id": str(run.id),
            "shadow_table": shadow,
            "target_table": target,
        },
        impact_report={
            "objects": [shadow],
            "shadow_table": shadow,
            "note": "只删影子表；正式表不在清理范围内",
        },
        rollback_plan=_no_compensation("影子表是可重建的中间产物，删除没有第二层补偿"),
    )


# ------------------------------------------------------------------ 凭据侧六类
async def _migration_facts(
    session: AsyncSession, aggregate_id: str, settings: Settings
) -> SideEffectFacts:
    request = await session.get(SecretMigrationRequest, _uuid(aggregate_id))
    if request is None:
        raise ResourceNotFound("迁移申请不存在")
    connection = await _connection(session, request.connection_id)
    return SideEffectFacts(
        operation=SideEffectOperation.SECRET_MIGRATION,
        project_id=connection.project_id,
        aggregate_type=AGGREGATE_SECRET_MIGRATION,
        aggregate_id=str(request.id),
        intent=_intent(
            name="secret.migrate",
            project_id=connection.project_id,
            objects=(
                _connection_object(connection.id),
                f"provider:{request.target_provider}",
            ),
            sensitivity=DataSensitivity.SECRET,
            settings=settings,
        ),
        fact_summary={
            "connection_id": str(connection.id),
            "migration_request_id": str(request.id),
            "source_provider": request.source_provider,
            "target_provider": request.target_provider,
            "source_retained": request.source_retained,
        },
        impact_report={
            "objects": [
                _connection_object(connection.id),
                f"provider:{request.target_provider}",
            ],
            "source_provider": request.source_provider,
            "target_provider": request.target_provider,
            "source_retained": request.source_retained,
            "note": "迁移只换存放位置、不换凭据内容；源凭据保留，因此可回退",
        },
        rollback_plan={
            "strategy": "SOURCE_RETAINED",
            "steps": [
                "源凭据不销毁，迁移前的引用留在 source_secret_ref",
                "需要回退时把连接的 secret_ref 切回该引用",
            ],
        },
    )


async def _rotation_facts(
    session: AsyncSession, aggregate_id: str, settings: Settings
) -> SideEffectFacts:
    request = await session.get(SecretRotationRequest, _uuid(aggregate_id))
    if request is None:
        raise ResourceNotFound("轮换申请不存在")
    connection = await _connection(session, request.connection_id)
    provider = provider_name_of(request.previous_secret_ref)
    return SideEffectFacts(
        operation=SideEffectOperation.SECRET_ROTATION_STAGE,
        project_id=connection.project_id,
        aggregate_type=AGGREGATE_SECRET_ROTATION,
        aggregate_id=str(request.id),
        intent=_intent(
            name="secret.rotate",
            project_id=connection.project_id,
            objects=(_connection_object(connection.id), f"provider:{provider}"),
            sensitivity=DataSensitivity.SECRET,
            settings=settings,
        ),
        fact_summary={
            "connection_id": str(connection.id),
            "rotation_request_id": str(request.id),
            "provider": provider,
            "phases": [
                SideEffectOperation.SECRET_ROTATION_STAGE.value,
                SideEffectOperation.SECRET_ROTATION_VERIFY.value,
                SideEffectOperation.SECRET_ROTATION_ACTIVATE.value,
            ],
        },
        impact_report={
            "objects": [_connection_object(connection.id), f"provider:{provider}"],
            "phases": "暂存新凭据 → 连通性验证 → 激活（验证不通过绝不激活）",
            "note": "审批通过之前不接触任何凭据",
        },
        rollback_plan={
            "strategy": "ROLLBACK_WINDOW",
            "steps": [
                "激活后开启回退窗口，窗口内可申请回退到旧凭据",
                "回退会把连接的 secret_ref 与凭据签发时间一并还原",
            ],
            "rollback_window_days": settings.secret_rotation_rollback_days,
        },
    )


async def _rotation_rollback_facts(
    session: AsyncSession, aggregate_id: str, settings: Settings
) -> SideEffectFacts:
    request = await session.get(SecretRotationRollbackRequest, _uuid(aggregate_id))
    if request is None:
        raise ResourceNotFound("轮换回退申请不存在")
    connection = await _connection(session, request.connection_id)
    return SideEffectFacts(
        operation=SideEffectOperation.SECRET_ROTATION_ROLLBACK,
        project_id=connection.project_id,
        aggregate_type=AGGREGATE_SECRET_ROTATION_ROLLBACK,
        aggregate_id=str(request.id),
        intent=_intent(
            name="secret.rollback_rotation",
            project_id=connection.project_id,
            objects=(
                _connection_object(connection.id),
                f"rotation:{request.rotation_request_id}",
            ),
            sensitivity=DataSensitivity.SECRET,
            settings=settings,
        ),
        fact_summary={
            "connection_id": str(connection.id),
            "rollback_request_id": str(request.id),
            "rotation_request_id": str(request.rotation_request_id),
        },
        impact_report={
            "objects": [
                _connection_object(connection.id),
                f"rotation:{request.rotation_request_id}",
            ],
            "note": "只在回退窗口内可申请；还原 secret_ref 与凭据签发时间",
        },
        rollback_plan=_no_compensation("回退本身是补偿动作，没有第二层补偿"),
    )


async def _destruction_facts(
    session: AsyncSession, aggregate_id: str, settings: Settings
) -> SideEffectFacts:
    request = await session.get(SecretDestructionRequest, _uuid(aggregate_id))
    if request is None:
        raise ResourceNotFound("销毁申请不存在")
    connection = await _connection(session, request.connection_id)
    return SideEffectFacts(
        operation=SideEffectOperation.SECRET_DESTRUCTION,
        project_id=connection.project_id,
        aggregate_type=AGGREGATE_SECRET_DESTRUCTION,
        aggregate_id=str(request.id),
        intent=_intent(
            # 销毁的对象是凭据本身，按 SECRET_USE 记效果不够 —— 它不可逆，按 DESTRUCTIVE 记，
            # PDP 因此会要求数据审批人与安全审批人各一名。
            name="secret.destroy",
            project_id=connection.project_id,
            objects=(
                _connection_object(connection.id),
                f"destroy-target:{request.target.value}",
            ),
            sensitivity=DataSensitivity.SECRET,
            settings=settings,
        ),
        fact_summary={
            "connection_id": str(connection.id),
            "destruction_request_id": str(request.id),
            "target": request.target.value,
            "source_rotation_request_id": (
                str(request.source_rotation_request_id)
                if request.source_rotation_request_id
                else None
            ),
        },
        impact_report={
            "objects": [
                _connection_object(connection.id),
                f"destroy-target:{request.target.value}",
            ],
            "target": request.target.value,
            "note": "销毁对象由轮换记录推导，不接受调用方直接指定引用",
        },
        rollback_plan=_no_compensation("销毁不可逆；确需恢复只能重新录入凭据"),
    )


# ------------------------------------------------------------------ 装配
_DERIVERS: Mapping[SideEffectOperation, Deriver] = {
    SideEffectOperation.EXECUTION_CANCEL: _cancel_job_facts,
    SideEffectOperation.DORIS_ROLLBACK: _doris_rollback_facts,
    SideEffectOperation.DORIS_CLEANUP: _doris_cleanup_facts,
    SideEffectOperation.SECRET_MIGRATION: _migration_facts,
    SideEffectOperation.SECRET_ROTATION_STAGE: _rotation_facts,
    SideEffectOperation.SECRET_ROTATION_VERIFY: _rotation_facts,
    SideEffectOperation.SECRET_ROTATION_ACTIVATE: _rotation_facts,
    SideEffectOperation.SECRET_ROTATION_ROLLBACK: _rotation_rollback_facts,
    SideEffectOperation.SECRET_DESTRUCTION: _destruction_facts,
}

_AGGREGATE_TYPES: Mapping[SideEffectOperation, str] = {
    SideEffectOperation.EXECUTION_CANCEL: AGGREGATE_EXECUTION_RUN,
    SideEffectOperation.DORIS_ROLLBACK: AGGREGATE_EXECUTION_RUN,
    SideEffectOperation.DORIS_CLEANUP: AGGREGATE_EXECUTION_RUN,
    SideEffectOperation.SECRET_MIGRATION: AGGREGATE_SECRET_MIGRATION,
    SideEffectOperation.SECRET_ROTATION_STAGE: AGGREGATE_SECRET_ROTATION,
    SideEffectOperation.SECRET_ROTATION_VERIFY: AGGREGATE_SECRET_ROTATION,
    SideEffectOperation.SECRET_ROTATION_ACTIVATE: AGGREGATE_SECRET_ROTATION,
    SideEffectOperation.SECRET_ROTATION_ROLLBACK: AGGREGATE_SECRET_ROTATION_ROLLBACK,
    SideEffectOperation.SECRET_DESTRUCTION: AGGREGATE_SECRET_DESTRUCTION,
}


def _no_compensation(reason: str) -> dict[str, Any]:
    return {"strategy": "NONE", "steps": [reason]}


def _intent(
    *,
    name: str,
    project_id: uuid.UUID,
    objects: tuple[str, ...],
    sensitivity: DataSensitivity,
    settings: Settings,
) -> ToolIntent:
    """构造意图；版本与效果**从工具名列**取，推导函数不能自己指定。

    能自己填 `effect` 的话，「这个工具危不危险」就有了两个说法：PDP 拿意图里的效果与
    清单里的效果比对，对不上是拒绝，而不是纠正 —— 那等于把一处错误变成一条难以解释的拒绝。
    """
    registered = _TOOLS.get(name)
    if registered is None:
        raise DomainError(f"未登记副作用工具：{name}")
    version, effect = registered
    return ToolIntent(
        tool_name=name,
        tool_version=version if version is not None else settings.seatunnel_version,
        effect=effect,
        # 主体由 Commit 阶段补齐；申请阶段只关心「做什么」。
        subject_id="",
        subject_project_id=str(project_id),
        scope=ResourceScope(project_id=str(project_id), objects=objects),
        environment=settings.environment,
        data_sensitivity=sensitivity,
    )


async def _require_run(
    session: AsyncSession, aggregate_id: str
) -> tuple[ExecutionRun, ExecutionPreparation]:
    run = await session.get(ExecutionRun, _uuid(aggregate_id))
    if run is None:
        raise ResourceNotFound("执行记录不存在")
    preparation = await session.get(ExecutionPreparation, run.harness_preparation_id)
    if preparation is None:
        raise ResourceNotFound("执行记录对应的准备单不存在")
    return run, preparation


async def project_of_run(session: AsyncSession, run: ExecutionRun) -> uuid.UUID:
    version = await session.get(PipelineVersion, run.pipeline_version_id)
    if version is None:
        raise ResourceNotFound("管道版本不存在")
    project_id = await session.scalar(
        select(Pipeline.project_id).where(Pipeline.id == version.pipeline_id)
    )
    if project_id is None:
        raise ResourceNotFound("管道不存在")
    return uuid.UUID(str(project_id))


def _shadow_of(preparation: ExecutionPreparation) -> tuple[str, str]:
    """从准备单冻结的回滚方案里取目标表与影子表名。

    取的是**服务端冻结的那份**，不是请求里给的：审批人看的就是它。
    """
    target = preparation.rollback_plan.get("target")
    shadow = preparation.rollback_plan.get("shadow_table")
    if not target or not shadow:
        raise DomainError("准备单没有冻结目标表与影子表，无法推导回滚/清理对象")
    return str(target), str(shadow)


async def _connection(session: AsyncSession, connection_id: uuid.UUID) -> DataConnection:
    connection = await session.get(DataConnection, connection_id)
    if connection is None:
        raise ResourceNotFound("数据连接不存在")
    return connection


def _connection_object(connection_id: uuid.UUID) -> str:
    return f"connection:{connection_id}"


def _uuid(value: str) -> uuid.UUID:
    try:
        return uuid.UUID(value)
    except ValueError as exc:
        raise DomainError(f"聚合对象标识不是合法的 UUID：{value!r}") from exc
