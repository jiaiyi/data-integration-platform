"""Commit：服务端重建事实并签发单次能力（T-35 / FR-55）。

这一层的全部要点是**不信客户端**（FR-55）：风险等级、工具版本、资源范围、
输入指纹一个都不从请求里来，全部从业务真相重新推导，再和准备单冻结的值逐项比对。
对不上就作废重来，而不是「以新的事实继续」—— 那等于让审批批了一个不存在的东西。

一条事务里做完四件事（SPEC §7.4 的关键不变量）：

```
消费 Capability  +  创建 ExecutionRun  +  写 Outbox  +  准备单转 COMMITTED
```

**执行本身不在这里发生**：Commit 之前外部系统零副作用。真正提交给引擎是 Worker
拿到 Outbox 命令之后的事，而且它要先验签。所以 Commit 走的「执行器」其实是
**把这次运行记下来并排进待投递队列**，这一点在下面 `_recorder` 的注释里说清楚了。

能力令牌**不进队列**：Worker 按 `execution_run_id` 从 `harness_capability_grants`
取声明体与签名来验，队列里只放命令本身。少一份凭据在消息中间件里流转。
"""

import uuid
from collections.abc import Mapping
from dataclasses import replace
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from data_integration_platform.application.access import require_project_roles
from data_integration_platform.application.audit import record_audit_event
from data_integration_platform.application.harness import grants, ledger, outbox
from data_integration_platform.application.harness.approval import require_usable
from data_integration_platform.application.harness.broker import (
    ToolBroker,
    ToolCall,
    ToolExecutorPort,
)
from data_integration_platform.application.harness.capability import (
    CapabilityExpectation,
    issue_capability,
)
from data_integration_platform.application.harness.pdp import build_policy_bundle
from data_integration_platform.application.harness.preparation import (
    PreparedFacts,
    derived_facts_for,
)
from data_integration_platform.application.harness.signing import SignerPort, VerifierPort
from data_integration_platform.application.harness.tools import (
    ToolRegistry,
    ToolSpec,
    build_tool_registry,
)
from data_integration_platform.config import Settings, get_settings
from data_integration_platform.domain.enums import (
    AuditEventType,
    ExecutionStatus,
    ExecutionTrigger,
    ProjectRole,
)
from data_integration_platform.domain.errors import DomainError, ResourceNotFound
from data_integration_platform.domain.harness import (
    ExecutionPreparationStatus,
    ToolIntent,
)
from data_integration_platform.infrastructure.ed25519_signing import (
    create_capability_signer,
    create_capability_verifier,
)
from data_integration_platform.infrastructure.models import (
    ExecutionPreparation,
    ExecutionRun,
    Pipeline,
    PipelineVersion,
)

WRITABLE = frozenset({ProjectRole.ADMIN, ProjectRole.DATA_ENGINEER})

# 执行工具提交的配置格式；与生成阶段产出的制品一致（HOCON）。
CONFIG_FORMAT = "HOCON"


async def commit(
    session: AsyncSession,
    *,
    preparation_id: uuid.UUID,
    actor_id: uuid.UUID,
    settings: Settings | None = None,
    registry: ToolRegistry | None = None,
    signer: SignerPort | None = None,
    verifier: VerifierPort | None = None,
    now: datetime | None = None,
    trace_id: str | None = None,
) -> ExecutionRun:
    """提交一次执行：重建事实 → 签发能力 → 同一事务建 Run 与 Outbox。"""
    resolved = settings if settings is not None else get_settings()
    moment = now if now is not None else datetime.now(UTC)
    preparation = await session.get(ExecutionPreparation, preparation_id)
    if preparation is None:
        raise ResourceNotFound("准备单不存在")
    version = await session.get(PipelineVersion, preparation.pipeline_version_id)
    if version is None:
        raise ResourceNotFound("管道版本不存在")
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
    await require_usable(
        session,
        preparation,
        version=version,
        project_id=pipeline.project_id,
        settings=resolved,
        now=moment,
    )
    if preparation.status is not ExecutionPreparationStatus.READY:
        raise DomainError(f"准备单当前状态为 {preparation.status.value}，只有 READY 才能提交")

    resolved_registry = (
        registry
        if registry is not None
        else build_tool_registry(seatunnel_version=resolved.seatunnel_version)
    )
    bundle = build_policy_bundle(version=resolved.harness_policy_bundle_version)
    # 事实**重新推导一遍**：不接受任何来自请求的风险等级、工具版本或资源范围。
    facts = await derived_facts_for(
        session,
        version=version,
        project_id=pipeline.project_id,
        environment=preparation.environment,
        registry=resolved_registry,
        bundle=bundle,
        platform_budget=resolved.harness_runtime_budget(),
    )
    spec = resolved_registry.resolve(name=facts.spec_tool_name, version=facts.spec_tool_version)
    intent = _subject_bound(facts.intent, subject_id=str(actor_id))
    decision = bundle.evaluate(intent, spec=spec)
    # 比的是**准备单上冻结的那个等级**与当下重新判定的结果。
    # 拿两个都来自当下推导的值互比是永远相等的，等于没检（这里踩过一次）。
    if decision.risk_level is not preparation.risk_level:
        raise DomainError(
            f"风险等级与准备单不一致（准备单 {preparation.risk_level.value}，"
            f"当下判定 {decision.risk_level.value}），审批批的是旧等级，请重新 Prepare"
        )

    payload = _tool_payload(preparation=preparation, facts=facts, version=version)
    expectation = CapabilityExpectation(
        tool_name=spec.manifest.name,
        tool_version=spec.manifest.version,
        subject_id=str(actor_id),
        environment=preparation.environment,
        preparation_id=str(preparation.id),
        artifact_sha256=preparation.artifact_sha256,
        input_fingerprint=preparation.input_fingerprint,
        resource_fingerprint=preparation.resource_fingerprint,
        supervision_contract_sha256=preparation.supervision_contract_sha256,
        data_classification_sha256=preparation.data_classification_sha256,
    )

    run = ExecutionRun(
        pipeline_version_id=version.id,
        harness_preparation_id=preparation.id,
        status=ExecutionStatus.QUEUED,
        idempotency_key=_run_idempotency_key(preparation),
        trigger=ExecutionTrigger.MANUAL,
        supervision_contract_sha256=preparation.supervision_contract_sha256,
    )
    session.add(run)
    await session.flush()

    resolved_signer = signer if signer is not None else create_capability_signer()
    token = issue_capability(
        preparation_id=str(preparation.id),
        tool_name=spec.manifest.name,
        tool_version=spec.manifest.version,
        subject_id=str(actor_id),
        environment=preparation.environment,
        artifact_sha256=preparation.artifact_sha256,
        input_fingerprint=preparation.input_fingerprint,
        resource_fingerprint=preparation.resource_fingerprint,
        supervision_contract_sha256=preparation.supervision_contract_sha256,
        data_classification_sha256=preparation.data_classification_sha256,
        policy_bundle_version=decision.policy_bundle_version,
        risk_level=decision.risk_level,
        ttl_seconds=resolved.harness_capability_ttl_seconds,
        signer=resolved_signer,
        now=moment,
    )
    await grants.record(session, preparation_id=preparation.id, token=token)

    broker = ToolBroker(
        registry=resolved_registry,
        bundle=bundle,
        verifier=verifier if verifier is not None else create_capability_verifier(),
        executor=_recorder(
            session=session,
            run_id=run.id,
            preparation_id=preparation.id,
            command_payload=payload,
        ),
    )
    await broker.invoke(
        session,
        call=ToolCall(
            intent=intent,
            payload=payload,
            expectation=expectation,
            records=facts.budget.max_records,
            bytes_=facts.budget.max_bytes,
        ),
        token=token,
        execution_run_id=run.id,
        now=moment,
    )

    preparation.status = ExecutionPreparationStatus.COMMITTED
    preparation.committed_by = actor_id
    preparation.committed_at = moment
    await session.flush()

    await ledger.append(
        session,
        aggregate_type="execution",
        aggregate_id=str(run.id),
        event_type="EXECUTION_COMMITTED",
        risk_level=decision.risk_level,
        payload={
            "run_id": str(run.id),
            "preparation_id": str(preparation.id),
            "version_id": str(version.id),
            "artifact_sha256": preparation.artifact_sha256,
            "token_id": token.claims.token_id,
            "risk_level": decision.risk_level.value,
            "matched_rules": list(decision.matched_rules),
        },
        signer=resolved_signer,
        trace_id=trace_id,
    )
    record_audit_event(
        session,
        event_type=AuditEventType.EXECUTION_COMMITTED,
        target_type="execution_run",
        project_id=pipeline.project_id,
        actor_id=actor_id,
        target_id=str(run.id),
        change_summary={
            "preparation_id": str(preparation.id),
            "version_id": str(version.id),
            "risk_level": decision.risk_level.value,
            "artifact_sha256": preparation.artifact_sha256,
        },
        trace_id=trace_id,
    )
    return run


def _recorder(
    *,
    session: AsyncSession,
    run_id: uuid.UUID,
    preparation_id: uuid.UUID,
    command_payload: Mapping[str, Any],
) -> ToolExecutorPort:
    """Broker 在 Commit 阶段的「执行器」：把运行排进待投递队列。

    这里**没有任何外部副作用** —— 它只写本地事实。真正的引擎提交由 Worker
    在验签之后发起（SPEC §7.4）。把这一步做成 Broker 的执行器，是为了让
    「所有副作用都经 Broker」这条边界连 Commit 自己都不例外。
    """

    async def record(*, spec: ToolSpec, payload: Mapping[str, Any]) -> Mapping[str, Any]:
        _ = spec  # 工具是谁由 Broker 校验过；执行器只负责把命令排进队列
        command = {
            **dict(command_payload),
            "run_id": str(run_id),
            "preparation_id": str(preparation_id),
        }
        row = await outbox.enqueue(session, preparation_id=preparation_id, command=command)
        return {"accepted": True, "reference": str(row.id)}

    return record


def _subject_bound(intent: ToolIntent, *, subject_id: str) -> ToolIntent:
    """把主体补进意图：Prepare 阶段还不知道是谁来提交。"""
    return replace(intent, subject_id=subject_id)


def _tool_payload(
    *,
    preparation: ExecutionPreparation,
    facts: PreparedFacts,
    version: PipelineVersion,
) -> dict[str, Any]:
    """交给执行工具的输入；字段必须与清单里的输入模型严格一致。"""
    source = facts.impact_report["source"]["table_name"]
    target = facts.impact_report["target"]["table_name"]
    return {
        "preparation_id": str(preparation.id),
        "artifact_sha256": preparation.artifact_sha256,
        "job_name": f"{source}_to_{target}",
        "config_format": CONFIG_FORMAT,
    }


def _run_idempotency_key(preparation: ExecutionPreparation) -> str:
    """执行记录的幂等键由准备单推导：同一份准备单提交两次不可能造出两条运行。"""
    return f"{preparation.idempotency_key}:commit"


async def find_run_for_preparation(
    session: AsyncSession, *, preparation_id: uuid.UUID
) -> ExecutionRun | None:
    return await session.scalar(
        select(ExecutionRun).where(ExecutionRun.harness_preparation_id == preparation_id)
    )
