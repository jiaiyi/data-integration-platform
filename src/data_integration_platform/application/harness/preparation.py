"""Prepare：生成执行准备单（T-33 / FR-51、FR-52）。

准备单把「这次执行到底动什么」冻下来：输入/资源/制品摘要、数据分级、影响范围、
运行预算与回滚方案。**推导全部在服务端完成**（FR-55 的前半条），客户端只给一个
幂等键 —— 风险等级、工具版本、资源范围一个都不许从请求里来。

**零外部副作用**（FR-51 的验收口径）：这一层只读数据库、只做纯计算。
它不连引擎、不读凭据、不投任务。守这条不是靠自觉：`prepare` 里没有任何
HTTP 客户端与 Secret Provider，测试也断言准备单落库后外部系统没有任何交互。

有效期默认 24 小时（FR-52）。准备单过期与能力令牌 5 分钟 TTL 是**两件独立的事**：
前者管「审批对象还在不在」，后者管「这张票还能不能用」。

前置是版本**已批准**：一个还没被人批准过的内容，不该先进到执行准备里。
"""

import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from data_integration_platform.application.access import require_project_roles
from data_integration_platform.application.audit import record_audit_event
from data_integration_platform.application.generation import EtlPlan
from data_integration_platform.application.harness.classification import (
    ClassificationReport,
    classify,
)
from data_integration_platform.application.harness.pdp import PolicyBundle, build_policy_bundle
from data_integration_platform.application.harness.tools import ToolRegistry, build_tool_registry
from data_integration_platform.application.secret_governance import as_utc
from data_integration_platform.config import Settings, get_settings
from data_integration_platform.domain.enums import (
    AuditEventType,
    DataEndpointRole,
    PipelineVersionStatus,
    ProjectRole,
)
from data_integration_platform.domain.errors import DomainError, ResourceNotFound, VersionConflict
from data_integration_platform.domain.harness import (
    ExecutionPreparationStatus,
    PolicyDecision,
    ResourceScope,
    ToolIntent,
    ToolIntentOutcome,
    canonical_sha256,
)
from data_integration_platform.domain.supervision import RuntimeBudget
from data_integration_platform.infrastructure.models import (
    DataConnection,
    DataProfile,
    ExecutionPreparation,
    HarnessPolicyDecision,
    Pipeline,
    PipelineVersion,
)

WRITABLE = frozenset({ProjectRole.ADMIN, ProjectRole.DATA_ENGINEER})

# 执行工具固定为引擎提交；版本随部署走（见 `Settings.seatunnel_version`）。
EXECUTION_TOOL = "seatunnel.submit_job"

# ADR-05：写入端是原子发布的唯一实现 —— 先写影子表，校验通过再换名。
PUBLISH_STRATEGY = "SHADOW_SWAP"


@dataclass(frozen=True, slots=True)
class PreparedFacts:
    """Prepare 推导出的全部事实，**没有一个来自请求体**。"""

    spec_tool_name: str
    spec_tool_version: str
    intent: ToolIntent
    decision: PolicyDecision
    scope: ResourceScope
    classification: ClassificationReport
    budget: RuntimeBudget
    impact_report: dict[str, Any]
    rollback_plan: dict[str, Any]

    @property
    def input_payload(self) -> dict[str, Any]:
        """输入指纹覆盖的载荷：动这份执行所依赖的一切输入。

        刻意不含「谁在什么时候点的按钮」—— 那是操作事实，不是输入；
        放进来会让同一份输入因为换了个人重做而指纹不同。
        """
        plan = self.intent.scope
        return {
            "tool": {"name": self.spec_tool_name, "version": self.spec_tool_version},
            "resource_fingerprint": plan.fingerprint(),
            "source": self.impact_report["source"],
            "target": self.impact_report["target"],
            "incremental": self.impact_report["incremental"],
            "mappings": self.impact_report["mappings"],
            # 读的是哪条连接属于「这次执行依赖的输入」：换了连接就是换了输入。
            "connections": self.impact_report["connections"],
        }

    def input_fingerprint(self) -> str:
        return canonical_sha256(self.input_payload)


def derive_facts(
    *,
    version: PipelineVersion,
    project_id: uuid.UUID,
    environment: str,
    registry: ToolRegistry,
    bundle: PolicyBundle,
    platform_budget: RuntimeBudget,
    connections: Mapping[str, str] | None = None,
) -> PreparedFacts:
    """从**业务真相**推导准备单的全部事实；纯计算，不碰外部系统。"""
    if version.artifact_sha256 is None or version.plan is None:
        raise DomainError("版本尚未冻结，没有可准备执行的内容")
    plan = EtlPlan.model_validate(version.plan)
    spec = registry.resolve(name=EXECUTION_TOOL, version=_tool_version(registry))

    scope = _scope(project_id=project_id, plan=plan)
    classification = _classify(plan, artifact_sha256=version.artifact_sha256)
    budget = _budget(plan=plan, platform=platform_budget)
    intent = ToolIntent(
        tool_name=spec.manifest.name,
        tool_version=spec.manifest.version,
        effect=spec.manifest.effect,
        subject_id="",  # 主体由调用方在 Commit 时补齐；Prepare 阶段只关心「做什么」
        subject_project_id=str(project_id),
        scope=scope,
        environment=environment,
        data_sensitivity=classification.overall,
    )
    decision = bundle.evaluate(intent, spec=spec)
    if decision.outcome not in (ToolIntentOutcome.ALLOW, ToolIntentOutcome.REQUIRE_APPROVAL):
        # 策略明确不放行的意图不该生成准备单：让它走到审批那里只是浪费人的时间。
        raise DomainError(f"策略判定为 {decision.outcome.value}，不生成准备单：{decision.reason}")

    return PreparedFacts(
        spec_tool_name=spec.manifest.name,
        spec_tool_version=spec.manifest.version,
        intent=intent,
        decision=decision,
        scope=scope,
        classification=classification,
        budget=budget,
        impact_report=_impact_report(
            plan=plan,
            scope=scope,
            classification=classification,
            budget=budget,
            connections=connections or {},
        ),
        rollback_plan=_rollback_plan(plan),
    )


async def prepare(
    session: AsyncSession,
    *,
    version_id: uuid.UUID,
    actor_id: uuid.UUID,
    idempotency_key: str,
    settings: Settings | None = None,
    registry: ToolRegistry | None = None,
    bundle: PolicyBundle | None = None,
    now: datetime | None = None,
    trace_id: str | None = None,
) -> ExecutionPreparation:
    """生成（或按幂等键取回）一份准备单。"""
    resolved = settings if settings is not None else get_settings()
    version = await _require_version(session, version_id)
    pipeline = await session.get(Pipeline, version.pipeline_id)
    if pipeline is None:
        raise ResourceNotFound("管道不存在")
    await require_project_roles(
        session,
        project_id=pipeline.project_id,
        actor_id=actor_id,
        allowed=WRITABLE,
        write=True,
    )
    if version.status is not PipelineVersionStatus.APPROVED:
        raise DomainError(f"版本当前状态为 {version.status.value}，只有已批准的版本才能准备执行")
    existing = await session.scalar(
        select(ExecutionPreparation).where(ExecutionPreparation.idempotency_key == idempotency_key)
    )
    if existing is not None:
        # 幂等键命中就返回原准备单：重试不该产出第二份待审批的准备单。
        if existing.pipeline_version_id != version.id:
            raise VersionConflict("该幂等键已用于另一个版本的准备单")
        return existing

    facts = await derived_facts_for(
        session,
        version=version,
        project_id=pipeline.project_id,
        environment=resolved.environment,
        registry=registry if registry is not None else build_tool_registry(),
        bundle=bundle
        if bundle is not None
        else build_policy_bundle(version=resolved.harness_policy_bundle_version),
        platform_budget=resolved.harness_runtime_budget(),
    )
    moment = now if now is not None else datetime.now(UTC)
    decision_row = _record_decision(session, facts=facts)
    await session.flush()

    needs_approval = bool(facts.decision.required_roles) or facts.decision.required_approvals > 0
    preparation = ExecutionPreparation(
        policy_decision_id=decision_row.id,
        pipeline_version_id=version.id,
        # 不需要审批的意图（例如只读工具）直接就绪；挂在 WAITING_APPROVAL 上会变成
        # 一张谁也推不动的准备单 —— 没有槽位可填，自然永远凑不齐审批。
        status=(
            ExecutionPreparationStatus.WAITING_APPROVAL
            if needs_approval
            else ExecutionPreparationStatus.READY
        ),
        risk_level=facts.decision.risk_level,
        policy_bundle_version=facts.decision.policy_bundle_version,
        environment=resolved.environment,
        tool_name=facts.spec_tool_name,
        tool_version=facts.spec_tool_version,
        artifact_sha256=version.artifact_sha256 or "",
        input_fingerprint=facts.input_fingerprint(),
        resource_fingerprint=facts.scope.fingerprint(),
        supervision_contract_sha256=facts.budget.fingerprint(),
        data_classification_sha256=facts.classification.sha256(),
        impact_report=facts.impact_report,
        rollback_plan=facts.rollback_plan,
        required_roles=[role.value for role in facts.decision.required_roles],
        required_approvals=max(facts.decision.required_approvals, 0),
        idempotency_key=idempotency_key,
        expires_at=moment + timedelta(seconds=resolved.harness_preparation_ttl_seconds),
        created_by=actor_id,
    )
    session.add(preparation)
    await session.flush()

    record_audit_event(
        session,
        event_type=AuditEventType.EXECUTION_PREPARED,
        target_type="execution_preparation",
        project_id=pipeline.project_id,
        actor_id=actor_id,
        target_id=str(preparation.id),
        change_summary={
            "version_id": str(version.id),
            "risk_level": facts.decision.risk_level.value,
            "required_roles": preparation.required_roles,
            "required_approvals": preparation.required_approvals,
            "artifact_sha256": preparation.artifact_sha256,
        },
        trace_id=trace_id,
    )
    return preparation


def require_not_expired(preparation: ExecutionPreparation, *, now: datetime | None = None) -> None:
    """准备单过期后审批与提交都失效（FR-52）；这里只管时间，状态另判。"""
    moment = now if now is not None else datetime.now(UTC)
    if as_utc(preparation.expires_at) <= moment:
        raise DomainError("准备单已过期，请重新发起 Prepare")


async def derived_facts_for(
    session: AsyncSession,
    *,
    version: PipelineVersion,
    project_id: uuid.UUID,
    environment: str,
    registry: ToolRegistry,
    bundle: PolicyBundle,
    platform_budget: RuntimeBudget,
) -> PreparedFacts:
    """**唯一的**事实推导入口：Prepare、审批前校验与 Commit 都必须走这里。

    三处各推一遍的话，任何一处漏了某个输入（这里踩过：Commit 忘了带连接），
    指纹就会在两步之间漂移，而漂移看起来像「事实被改过」，排查方向全错。
    """
    return derive_facts(
        version=version,
        project_id=project_id,
        environment=environment,
        registry=registry,
        bundle=bundle,
        platform_budget=platform_budget,
        connections=await _resolve_connections(
            session,
            project_id=project_id,
            plan=EtlPlan.model_validate(version.plan or {}),
        ),
    )


async def _resolve_connections(
    session: AsyncSession, *, project_id: uuid.UUID, plan: EtlPlan
) -> dict[str, str]:
    """按计划里的库表在项目内找到对应的连接。

    计划里的 `connection_id` 是模型写的、可选；**执行阶段要拿它去取凭据**，
    所以必须由服务端自己查实。查不到或查到多条都拒绝 —— 取错连接等于用错凭据。
    """
    resolved: dict[str, str] = {}
    for key, role, schema_name, table_name in (
        ("source", DataEndpointRole.SOURCE, plan.source.schema_name, plan.source.table_name),
        ("target", DataEndpointRole.SINK, plan.target.schema_name, plan.target.table_name),
    ):
        candidates = (
            await session.scalars(
                select(DataProfile.connection_id)
                .join(DataConnection, DataConnection.id == DataProfile.connection_id)
                .where(
                    DataConnection.project_id == project_id,
                    DataProfile.endpoint_role == role,
                    DataProfile.schema_name == schema_name,
                    DataProfile.table_name == table_name,
                )
                .distinct()
            )
        ).all()
        if len(candidates) != 1:
            raise DomainError(
                f"无法唯一确定 {role.value} 端连接：{schema_name}.{table_name} 在项目内"
                f"匹配到 {len(candidates)} 条连接，执行阶段取凭据需要唯一确定"
            )
        resolved[key] = str(candidates[0])
    return resolved


def _tool_version(registry: ToolRegistry) -> str:
    """工具清单里唯一注册的那个引擎版本（同名多版本在 T-24 已被拒）。"""
    for spec in registry.specs():
        if spec.manifest.name == EXECUTION_TOOL:
            return spec.manifest.version
    raise ResourceNotFound(f"工具清单里没有 {EXECUTION_TOOL}")


def _scope(*, project_id: uuid.UUID, plan: EtlPlan) -> ResourceScope:
    objects = (
        f"{plan.source.schema_name}.{plan.source.table_name}",
        f"{plan.target.schema_name}.{plan.target.table_name}",
    )
    connection_ids = tuple(
        connection
        for connection in (plan.source.connection_id, plan.target.connection_id)
        if connection
    )
    return ResourceScope(project_id=str(project_id), objects=objects, connection_ids=connection_ids)


def _classify(plan: EtlPlan, *, artifact_sha256: str) -> ClassificationReport:
    """按计划里的映射给字段分级。

    源端字段按名称规则判，目标端字段经**血缘**继承 —— 这正是 T-26 的传播在
    真实制品上的用法：`email_masked` 认不出名字，但它来自 `email`。
    """
    source_columns = [{"name": mapping.source_column} for mapping in plan.mappings]
    target_columns = [{"name": mapping.target_column} for mapping in plan.mappings]
    lineage = {mapping.target_column: [mapping.source_column] for mapping in plan.mappings}
    return classify(
        [*source_columns, *target_columns],
        artifact_sha256=artifact_sha256,
        lineage=lineage,
    )


def _budget(*, plan: EtlPlan, platform: RuntimeBudget) -> RuntimeBudget:
    """计划只能**收紧**平台上限，不能放宽（FR-75 的口径在这里就生效）。"""
    spec = plan.budget
    return RuntimeBudget(
        max_records=_tighter(platform.max_records, spec.max_records),
        max_bytes=_tighter(platform.max_bytes, spec.max_bytes),
        timeout_seconds=_tighter(platform.timeout_seconds, spec.timeout_seconds),
        max_output_ratio=platform.max_output_ratio,
        max_reject_rate=platform.max_reject_rate,
        minimum_quality_rows=platform.minimum_quality_rows,
        warning_ratio=platform.warning_ratio,
    )


def _tighter(platform: int, declared: int | None) -> int:
    return platform if declared is None else min(platform, declared)


def _impact_report(
    *,
    plan: EtlPlan,
    scope: ResourceScope,
    classification: ClassificationReport,
    budget: RuntimeBudget,
    connections: Mapping[str, str],
) -> dict[str, Any]:
    """影响范围。

    **不写「目标表是否存在」**：那要连库才知道，而 Prepare 不碰外部系统。
    目标表是不是新建的，由执行阶段用影子表 + 换名处理（ADR-05），
    在这里猜一个布尔值只会给审批人一个可能错的印象。
    """
    return {
        "objects": sorted(scope.objects),
        # 源/目标端用的是哪条连接：执行阶段要按它去取凭据把占位符物化。
        # 计划里的 `connection_id` 是可选的（模型可能不写），所以这里是**服务端查出来的**。
        "connections": dict(connections),
        "source": {
            "schema_name": plan.source.schema_name,
            "table_name": plan.source.table_name,
        },
        "target": {
            "schema_name": plan.target.schema_name,
            "table_name": plan.target.table_name,
            "primary_key": sorted(plan.target.primary_key),
        },
        "mappings": [
            {"source": mapping.source_column, "target": mapping.target_column}
            for mapping in plan.mappings
        ],
        "quality_rules": [rule.error_code for rule in plan.quality_rules],
        "incremental": plan.incremental.model_dump(mode="json"),
        "dedup_keys": sorted(plan.dedup_keys),
        "data_classification": {
            "overall": classification.overall.value,
            "unresolved": list(classification.unresolved),
        },
        "budget": budget.canonical_payload(),
        "publish_strategy": PUBLISH_STRATEGY,
    }


def _rollback_plan(plan: EtlPlan) -> dict[str, Any]:
    """回滚方案：影子表 + 原子换名，中途失败就丢影子表，正式表全程不动。"""
    shadow = f"{plan.target.table_name}__shadow"
    return {
        "strategy": PUBLISH_STRATEGY,
        "target": f"{plan.target.schema_name}.{plan.target.table_name}",
        "shadow_table": f"{plan.target.schema_name}.{shadow}",
        "steps": [
            "创建影子表（与目标表同构）",
            "写入影子表并核对行数与质量规则",
            "校验通过后原子换名，正式表在换名前始终可读",
            "任一步失败：丢弃影子表，正式表不做任何改动",
        ],
    }


def _record_decision(session: AsyncSession, *, facts: PreparedFacts) -> HarnessPolicyDecision:
    """把 PDP 决策落成事实，作为准备单的可追溯依据（FR-49 的落地点）。"""
    row = HarnessPolicyDecision(
        tool_name=facts.spec_tool_name,
        tool_version=facts.spec_tool_version,
        outcome=facts.decision.outcome,
        risk_level=facts.decision.risk_level,
        policy_bundle_version=facts.decision.policy_bundle_version,
        request_fingerprint=facts.intent.fingerprint(),
        resource_fingerprint=facts.scope.fingerprint(),
        matched_rules=list(facts.decision.matched_rules),
        required_roles=[role.value for role in facts.decision.required_roles],
        required_approvals=facts.decision.required_approvals,
        subject_id=facts.intent.subject_id,
        environment=facts.intent.environment,
        reason=facts.decision.reason,
    )
    session.add(row)
    return row


async def _require_version(session: AsyncSession, version_id: uuid.UUID) -> PipelineVersion:
    version = await session.get(PipelineVersion, version_id)
    if version is None:
        raise ResourceNotFound("管道版本不存在")
    return version
