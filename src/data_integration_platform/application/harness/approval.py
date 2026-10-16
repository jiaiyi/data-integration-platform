"""Approve：具名职责审批（T-34 / FR-53、FR-54）。

审批的对象是**准备单里冻结的那些事实**，不是「批准这次操作」这么一句话。
因此每次决策之前都要先确认那些事实还成立：

1. **没过期**（FR-52）—— 24 小时之后这份准备单不再代表任何东西；
2. **事实没变** —— 重算指纹与准备单上的比对，不一致说明它已经被时间甩在后面了
   （平台预算、策略包版本变了都会走到这里）。这时**已有的审批一并失效**：
   它们批的是旧事实。
3. **槽位对得上** —— 审批人必须真的持有它声称的那个 Harness 职责槽，
   而且那个槽位在 PDP 给出的 `required_roles` 里。

三条职责分离同时生效：申请人自批（SOD-01）、一人占多槽（SOD-02）、
以及槽位重复占用（同一槽位只能有一个决定）。
"""

import uuid
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from data_integration_platform.application.access import (
    require_harness_roles,
    require_project_roles,
)
from data_integration_platform.application.audit import record_audit_event
from data_integration_platform.application.harness.pdp import build_policy_bundle
from data_integration_platform.application.harness.preparation import (
    derived_facts_for,
    require_not_expired,
)
from data_integration_platform.application.harness.tools import build_tool_registry
from data_integration_platform.config import Settings, get_settings
from data_integration_platform.domain.enums import AuditEventType, ProjectRole
from data_integration_platform.domain.errors import DomainError, ResourceNotFound, VersionConflict
from data_integration_platform.domain.harness import (
    ExecutionPreparationStatus,
    HarnessApprovalValue,
    HarnessRole,
    require_distinct_slot_holders,
    require_no_self_approval,
)
from data_integration_platform.infrastructure.models import (
    ExecutionPreparation,
    HarnessApprovalDecision,
    Pipeline,
    PipelineVersion,
)

APPROVER_ROLES = frozenset({ProjectRole.ADMIN, ProjectRole.APPROVER})


async def decide(
    session: AsyncSession,
    *,
    preparation_id: uuid.UUID,
    actor_id: uuid.UUID,
    role: HarnessRole,
    decision: HarnessApprovalValue,
    reason: str,
    settings: Settings | None = None,
    now: datetime | None = None,
    trace_id: str | None = None,
) -> ExecutionPreparation:
    """记录一条具名审批决定；齐备后准备单进入 `READY`。"""
    resolved = settings if settings is not None else get_settings()
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
        allowed=APPROVER_ROLES,
        write=True,
    )
    # 「有审批角色的成员」与「持有该职责槽的主体」是两件事：前者管能不能进审批台，
    # 后者管能不能签这个槽位（PRD §2.3 的两套角色分离）。
    await require_harness_roles(
        session, project_id=pipeline.project_id, actor_id=actor_id, allowed={role}
    )
    moment = now if now is not None else datetime.now(UTC)
    await require_usable(
        session,
        preparation,
        version=version,
        project_id=pipeline.project_id,
        settings=resolved,
        now=moment,
    )
    if preparation.status is not ExecutionPreparationStatus.WAITING_APPROVAL:
        raise DomainError(f"准备单当前状态为 {preparation.status.value}，不接受审批")
    if role.value not in preparation.required_roles:
        raise DomainError(
            f"该准备单没有 {role.value} 这个审批槽，需要的槽位是 "
            f"{'、'.join(preparation.required_roles) or '（无）'}"
        )
    require_no_self_approval(applicant_id=str(preparation.created_by), approver_id=str(actor_id))

    existing = list(
        (
            await session.scalars(
                select(HarnessApprovalDecision).where(
                    HarnessApprovalDecision.preparation_id == preparation.id
                )
            )
        ).all()
    )
    # SOD-02：同一个人不得在同一张准备单上占两个槽。
    require_distinct_slot_holders(
        role_holders={HarnessRole(item.role): str(item.approver_id) for item in existing}
        | {role: str(actor_id)}
    )

    record = HarnessApprovalDecision(
        preparation_id=preparation.id,
        approver_id=actor_id,
        role=role,
        decision=decision,
        reason=reason,
        decided_at=moment,
    )
    session.add(record)
    try:
        await session.flush()
    except IntegrityError as exc:
        raise VersionConflict(f"该准备单的 {role.value} 槽位已经有决定了") from exc

    if decision is HarnessApprovalValue.REJECT:
        preparation.status = ExecutionPreparationStatus.REJECTED
    else:
        approvals = [*existing, record]
        approved_roles = {
            item.role for item in approvals if item.decision is HarnessApprovalValue.APPROVE
        }
        if (
            set(preparation.required_roles) <= {item.value for item in approved_roles}
            and len(approved_roles) >= preparation.required_approvals
        ):
            preparation.status = ExecutionPreparationStatus.READY
    await session.flush()

    record_audit_event(
        session,
        event_type=AuditEventType.EXECUTION_APPROVED,
        target_type="execution_preparation",
        project_id=pipeline.project_id,
        actor_id=actor_id,
        target_id=str(preparation.id),
        change_summary={
            "role": role.value,
            "decision": decision.value,
            "reason": reason,
            "status": preparation.status.value,
        },
        trace_id=trace_id,
    )
    return preparation


async def require_usable(
    session: AsyncSession,
    preparation: ExecutionPreparation,
    *,
    version: PipelineVersion,
    project_id: uuid.UUID,
    settings: Settings,
    now: datetime | None = None,
) -> None:
    """确认这份准备单还代表当下的事实；不代表就把它作废并拒绝。

    过期与「事实变了」都落到 `EXPIRED`：两者对调用方的含义一样 ——
    这份准备单不能再用了，重新 Prepare 一份。区分它们只体现在错误消息里。
    """
    moment = now if now is not None else datetime.now(UTC)
    if preparation.status in (
        ExecutionPreparationStatus.COMMITTED,
        ExecutionPreparationStatus.REJECTED,
        ExecutionPreparationStatus.DENIED,
    ):
        raise DomainError(f"准备单已经是 {preparation.status.value}，不能再使用")
    try:
        require_not_expired(preparation, now=moment)
    except DomainError:
        preparation.status = ExecutionPreparationStatus.EXPIRED
        await session.flush()
        raise

    facts = await derived_facts_for(
        session,
        version=version,
        project_id=project_id,
        environment=preparation.environment,
        registry=build_tool_registry(seatunnel_version=settings.seatunnel_version),
        bundle=build_policy_bundle(version=settings.harness_policy_bundle_version),
        platform_budget=settings.harness_runtime_budget(),
    )
    drifted = [
        name
        for name, frozen, recomputed in (
            ("artifact_sha256", preparation.artifact_sha256, version.artifact_sha256 or ""),
            ("input_fingerprint", preparation.input_fingerprint, facts.input_fingerprint()),
            ("resource_fingerprint", preparation.resource_fingerprint, facts.scope.fingerprint()),
            (
                "supervision_contract_sha256",
                preparation.supervision_contract_sha256,
                facts.budget.fingerprint(),
            ),
            (
                "data_classification_sha256",
                preparation.data_classification_sha256,
                facts.classification.sha256(),
            ),
        )
        if frozen != recomputed
    ]
    if drifted:
        preparation.status = ExecutionPreparationStatus.EXPIRED
        await session.flush()
        raise VersionConflict(
            "准备单冻结的事实已经变化（" + "、".join(drifted) + "），已有审批一并失效，"
            "请重新 Prepare"
        )
