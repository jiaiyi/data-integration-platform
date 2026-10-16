"""通用副作用框架（T-36 / FR-57、FR-23）。

九种副作用操作（`domain/side_effects.py::SideEffectOperation`）共用**一套**入口：

```
prepare（申请） → decide（具名审批） → commit（签发并消费能力） → 执行（租约 + 验签）
```

存在的理由是 FR-57 的验收口径：**不得存在绕过这条入口的直连执行路径**。
在此之前，六类凭据操作走的是 M2 各自那条「申请→审批→执行」的路 —— 业务语义对，
但它们不产生准备单、不产生能力记录，于是「所有副作用都受同一个内核管」只是一句话。

## 与执行链路（T-33~T-35）的关系

不是同一套表，但是**同一套规矩**。执行准备单绑的是「管道版本 + 制品字节」，
副作用准备单绑的是「聚合对象 + 冻结的事实摘要」—— 一个凭据轮换没有制品，
硬塞进 `execution_preparations` 会让那边多出一堆语义为空的列。所以表分开、流程对齐：

| 阶段 | 执行链路 | 副作用链路（本模块） |
|---|---|---|
| 申请 | `prepare()` | `prepare()` |
| 审批 | `approval.decide()` | `decide()` |
| 提交 | `commit()` | `commit()` |
| 执行前把关 | Worker 验签 | `claim_execution()` 取租约 + `verify_grant()` 验签 |
| 并发保护 | Outbox 租约 | `execution_lease_*` 执行租约（`SideEffectLeaseBusy`） |

**能力令牌的五项绑定在副作用语境下的含义**（同一个 `CapabilityClaims` 结构，
不为了这里另造一套令牌）：`artifact_sha256` 取事实摘要（副作用没有制品，
被冻结的「要动什么的字节」就是它的制品）；`supervision_contract_sha256` 取影响面摘要
（副作用没有运行预算，等价的约束是「影响面不许在执行期变化」）；
`data_classification_sha256` 取数据分级摘要，其余两项本就同名同义。

## 一条不放松的边界

`fact_fingerprint` 在 Prepare 时冻结，在**审批前、Commit 时、执行前**各重算一次并逐项比对。
所以「审批之后把要销毁的凭据换成另一把」在结构上不可能 —— 这正是三阶段协议要买的东西。
事实的推导**只有一条路径**（各操作自己的 `side_effect_facts`），三处各推一遍的话，
任何一处漏了某个输入都会让指纹漂移，而漂移看起来像「事实被改过」，排查方向全错。
"""

import uuid
from collections.abc import Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from typing import Any, cast

from sqlalchemy import or_, select, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from data_integration_platform.application.access import (
    require_harness_roles,
    require_project_roles,
)
from data_integration_platform.application.audit import record_audit_event
from data_integration_platform.application.harness import ledger
from data_integration_platform.application.harness.capability import (
    CapabilityExpectation,
    SignedCapability,
    issue_capability,
    token_from_grant,
    verify_capability,
)
from data_integration_platform.application.harness.pdp import PolicyBundle, build_policy_bundle
from data_integration_platform.application.harness.signing import SignerPort, VerifierPort
from data_integration_platform.application.harness.tools import ToolRegistry, build_tool_registry
from data_integration_platform.application.secret_governance import as_utc
from data_integration_platform.config import Settings, get_settings
from data_integration_platform.domain.enums import AuditEventType, ProjectRole
from data_integration_platform.domain.errors import (
    DomainError,
    ResourceNotFound,
    SideEffectLeaseBusy,
    VersionConflict,
)
from data_integration_platform.domain.harness import (
    ExecutionPreparationStatus,
    HarnessApprovalValue,
    HarnessRole,
    PolicyDecision,
    ToolIntent,
    ToolIntentOutcome,
    canonical_sha256,
    require_distinct_slot_holders,
    require_no_self_approval,
)
from data_integration_platform.domain.side_effects import (
    SideEffectExecutionStatus,
    SideEffectOperation,
)
from data_integration_platform.infrastructure.ed25519_signing import (
    create_capability_signer,
    create_capability_verifier,
)
from data_integration_platform.infrastructure.models import (
    HarnessPolicyDecision,
    HarnessSideEffectApproval,
    HarnessSideEffectCapabilityGrant,
    HarnessSideEffectPreparation,
)

# 能进审批台的项目成员角色（与执行链路的准备单一致）。
APPROVER_ROLES = frozenset({ProjectRole.ADMIN, ProjectRole.APPROVER})
WRITABLE = frozenset({ProjectRole.ADMIN, ProjectRole.DATA_ENGINEER})
READABLE = frozenset(
    {ProjectRole.ADMIN, ProjectRole.DATA_ENGINEER, ProjectRole.APPROVER, ProjectRole.VIEWER}
)

# 终态：不接受任何后续阶段。
_TERMINAL = (
    ExecutionPreparationStatus.COMMITTED,
    ExecutionPreparationStatus.REJECTED,
    ExecutionPreparationStatus.DENIED,
    ExecutionPreparationStatus.EXPIRED,
)


@dataclass(frozen=True, slots=True)
class SideEffectFacts:
    """服务端为一次副作用推导出的全部事实；**没有一个字段来自请求体**。

    每个操作各自只有一个推导函数（见 `side_effect_targets.py`），
    Prepare、审批前校验、Commit、执行前校验都读它 —— 一条推导路径。
    """

    operation: SideEffectOperation
    project_id: uuid.UUID
    aggregate_type: str
    aggregate_id: str
    intent: ToolIntent
    fact_summary: dict[str, Any]
    impact_report: dict[str, Any]
    rollback_plan: dict[str, Any]

    def fact_fingerprint(self) -> str:
        """冻结事实的摘要：动谁、动什么、动到什么程度。"""
        return canonical_sha256(
            {
                "operation": self.operation.value,
                "aggregate_type": self.aggregate_type,
                "aggregate_id": self.aggregate_id,
                "facts": self.fact_summary,
            }
        )

    def input_fingerprint(self) -> str:
        """输入摘要：换了工具、换了作用范围、或事实本身变了，都会让摘要不同。"""
        return canonical_sha256(
            {
                "tool": {"name": self.intent.tool_name, "version": self.intent.tool_version},
                "resource_fingerprint": self.resource_fingerprint(),
                "facts": self.fact_summary,
                "data_sensitivity": self.intent.data_sensitivity.value,
            }
        )

    def resource_fingerprint(self) -> str:
        return self.intent.scope.fingerprint()

    def artifact_sha256(self) -> str:
        """副作用没有制品；被冻结的申请事实就是它的「制品字节」。"""
        return self.fact_fingerprint()

    def supervision_contract_sha256(self) -> str:
        """副作用没有运行预算；等价约束是「影响面不得在执行期变化」。"""
        return canonical_sha256(self.impact_report)

    def data_classification_sha256(self) -> str:
        return canonical_sha256({"data_sensitivity": self.intent.data_sensitivity.value})


def bindings_of_facts(facts: SideEffectFacts) -> dict[str, str]:
    return {
        "artifact_sha256": facts.artifact_sha256(),
        "input_fingerprint": facts.input_fingerprint(),
        "resource_fingerprint": facts.resource_fingerprint(),
        "supervision_contract_sha256": facts.supervision_contract_sha256(),
        "data_classification_sha256": facts.data_classification_sha256(),
    }


def bindings_of_preparation(preparation: HarnessSideEffectPreparation) -> dict[str, str]:
    """从**落库的列**重算同一组绑定摘要；执行方与审批方各算一遍，必须逐项相等。

    与 `bindings_of_facts` 是同一套口径的两端：一端算将要写的，一端算已经写的。
    两处都只读同一个来源（准备单冻结的那几列），所以不会各自漂移。
    """
    return {
        "artifact_sha256": preparation.fact_fingerprint,
        "input_fingerprint": preparation.input_fingerprint,
        "resource_fingerprint": preparation.resource_fingerprint,
        "supervision_contract_sha256": canonical_sha256(preparation.impact_report),
        "data_classification_sha256": canonical_sha256(
            {"data_sensitivity": preparation.fact_summary.get("data_sensitivity")}
        ),
    }


# ------------------------------------------------------------------ 申请
async def prepare(
    session: AsyncSession,
    *,
    facts: SideEffectFacts,
    actor_id: uuid.UUID,
    idempotency_key: str,
    settings: Settings | None = None,
    registry: ToolRegistry | None = None,
    bundle: PolicyBundle | None = None,
    now: datetime | None = None,
    trace_id: str | None = None,
) -> HarnessSideEffectPreparation:
    """生成（或按幂等键取回）一份副作用准备单；**零外部副作用**。

    幂等键命中同一份事实就返回原准备单：重试不该产出第二张待审批的申请单。
    命中但事实不同则拒绝 —— 那说明同一个键被拿去申请了两件不同的事。
    """
    resolved = settings if settings is not None else get_settings()
    moment = now if now is not None else datetime.now(UTC)
    await require_project_roles(
        session, project_id=facts.project_id, actor_id=actor_id, allowed=WRITABLE, write=True
    )
    existing = await _by_idempotency_key(session, idempotency_key)
    if existing is not None:
        if existing.fact_fingerprint != facts.fact_fingerprint():
            raise VersionConflict("该幂等键已用于另一件事实不同的副作用申请")
        return existing

    resolved_registry = (
        registry
        if registry is not None
        else build_tool_registry(seatunnel_version=resolved.seatunnel_version)
    )
    resolved_bundle = (
        bundle
        if bundle is not None
        else build_policy_bundle(version=resolved.harness_policy_bundle_version)
    )
    spec = resolved_registry.resolve(name=facts.intent.tool_name, version=facts.intent.tool_version)
    decision = resolved_bundle.evaluate(facts.intent, spec=spec)
    if decision.outcome not in (ToolIntentOutcome.ALLOW, ToolIntentOutcome.REQUIRE_APPROVAL):
        # 策略不放行的动作不该生成准备单：走到审批那里只是浪费人的时间。
        raise DomainError(
            f"策略判定为 {decision.outcome.value}，不生成副作用准备单：{decision.reason}"
        )

    _record_decision(session, facts=facts, decision=decision)
    await session.flush()
    bindings = bindings_of_facts(facts)
    needs_approval = bool(decision.required_roles) or decision.required_approvals > 0
    preparation = HarnessSideEffectPreparation(
        operation=facts.operation,
        project_id=facts.project_id,
        aggregate_type=facts.aggregate_type,
        aggregate_id=facts.aggregate_id,
        # 不需要审批的动作（例如只读的连通性验证）直接置 READY：
        # 挂在 WAITING_APPROVAL 上会变成一张没有槽位可填、谁也推不动的单子。
        status=(
            ExecutionPreparationStatus.WAITING_APPROVAL
            if needs_approval
            else ExecutionPreparationStatus.READY
        ),
        risk_level=decision.risk_level,
        policy_bundle_version=decision.policy_bundle_version,
        environment=facts.intent.environment,
        tool_name=facts.intent.tool_name,
        tool_version=facts.intent.tool_version,
        fact_fingerprint=facts.fact_fingerprint(),
        # 分级落进 fact_summary，使 `bindings_of_preparation` 能从库里重算出同一摘要。
        fact_summary={
            **facts.fact_summary,
            "data_sensitivity": facts.intent.data_sensitivity.value,
        },
        input_fingerprint=bindings["input_fingerprint"],
        resource_fingerprint=bindings["resource_fingerprint"],
        impact_report=dict(facts.impact_report),
        rollback_plan=dict(facts.rollback_plan),
        required_roles=[role.value for role in decision.required_roles],
        required_approvals=max(decision.required_approvals, 0),
        idempotency_key=idempotency_key,
        expires_at=moment + timedelta(seconds=resolved.harness_preparation_ttl_seconds),
        created_by=actor_id,
    )
    session.add(preparation)
    await session.flush()

    record_audit_event(
        session,
        event_type=AuditEventType.SIDE_EFFECT_PREPARED,
        target_type="harness_side_effect_preparation",
        project_id=facts.project_id,
        actor_id=actor_id,
        target_id=str(preparation.id),
        change_summary={
            "operation": facts.operation.value,
            "aggregate_type": facts.aggregate_type,
            "aggregate_id": facts.aggregate_id,
            "risk_level": decision.risk_level.value,
            "required_roles": preparation.required_roles,
            "required_approvals": preparation.required_approvals,
            "fact_fingerprint": preparation.fact_fingerprint,
        },
        trace_id=trace_id,
    )
    return preparation


# ------------------------------------------------------------------ 审批
async def decide(
    session: AsyncSession,
    *,
    preparation_id: uuid.UUID,
    actor_id: uuid.UUID,
    facts: SideEffectFacts,
    role: HarnessRole | None,
    decision: HarnessApprovalValue,
    reason: str,
    settings: Settings | None = None,
    now: datetime | None = None,
    trace_id: str | None = None,
) -> HarnessSideEffectPreparation:
    """记录一条具名审批；齐备后准备单进入 `READY`。

    `role` 省略时取准备单上**唯一**那个槽位。要求两个槽位却不说清是哪一个，
    说明调用方没读懂这张单子，这里直接拒绝而不是替它猜。
    """
    preparation = await _require(session, preparation_id)
    moment = now if now is not None else datetime.now(UTC)
    await _require_usable(session, preparation, facts=facts, now=moment)
    if preparation.status is not ExecutionPreparationStatus.WAITING_APPROVAL:
        raise DomainError(f"准备单当前状态为 {preparation.status.value}，不接受审批")

    resolved_role = role if role is not None else _single_slot(preparation)
    if resolved_role.value not in preparation.required_roles:
        raise DomainError(
            f"该准备单没有 {resolved_role.value} 这个审批槽，需要的槽位是 "
            f"{'、'.join(preparation.required_roles) or '（无）'}"
        )
    await require_project_roles(
        session,
        project_id=preparation.project_id,
        actor_id=actor_id,
        allowed=APPROVER_ROLES,
        write=True,
    )
    # 「有审批角色的成员」与「持有该职责槽的主体」是两件事，两者都要成立。
    await require_harness_roles(
        session, project_id=preparation.project_id, actor_id=actor_id, allowed={resolved_role}
    )
    require_no_self_approval(applicant_id=str(preparation.created_by), approver_id=str(actor_id))

    existing = await approvals_of(session, preparation_id=preparation.id)
    # SOD-02：同一个人不得在同一张准备单上占两个槽。
    require_distinct_slot_holders(
        role_holders={HarnessRole(item.role): str(item.approver_id) for item in existing}
        | {resolved_role: str(actor_id)}
    )

    record = HarnessSideEffectApproval(
        preparation_id=preparation.id,
        approver_id=actor_id,
        role=resolved_role,
        decision=decision,
        reason=reason,
        decided_at=moment,
    )
    session.add(record)
    try:
        await session.flush()
    except IntegrityError as exc:
        raise VersionConflict(f"该准备单的 {resolved_role.value} 槽位已经有决定了") from exc

    if decision is HarnessApprovalValue.REJECT:
        preparation.status = ExecutionPreparationStatus.REJECTED
    else:
        approved_roles = {
            item.role
            for item in [*existing, record]
            if item.decision is HarnessApprovalValue.APPROVE
        }
        if (
            set(preparation.required_roles) <= {item.value for item in approved_roles}
            and len(approved_roles) >= preparation.required_approvals
        ):
            preparation.status = ExecutionPreparationStatus.READY
    await session.flush()

    record_audit_event(
        session,
        event_type=AuditEventType.SIDE_EFFECT_APPROVED,
        target_type="harness_side_effect_preparation",
        project_id=preparation.project_id,
        actor_id=actor_id,
        target_id=str(preparation.id),
        change_summary={
            "operation": preparation.operation.value,
            "role": resolved_role.value,
            "decision": decision.value,
            "reason": reason[:2000],
            "status": preparation.status.value,
        },
        trace_id=trace_id,
    )
    return preparation


async def abandon(
    session: AsyncSession,
    *,
    preparation: HarnessSideEffectPreparation,
    reason: str,
    trace_id: str | None = None,
) -> HarnessSideEffectPreparation:
    """把一张准备单作废（业务前置失败、申请人撤回等）。

    落 `REJECTED` 而不是删掉：**申请发生过就是事实**。删掉它会让「谁在什么时候
    申请过什么」从证据面上消失，而准备单正是这条事实的载体。
    """
    if preparation.status is ExecutionPreparationStatus.COMMITTED:
        raise DomainError("已经提交的准备单不能作废")
    preparation.status = ExecutionPreparationStatus.REJECTED
    preparation.failure_reason = reason[:2000]
    await session.flush()
    record_audit_event(
        session,
        event_type=AuditEventType.SIDE_EFFECT_FAILED,
        target_type="harness_side_effect_preparation",
        project_id=preparation.project_id,
        target_id=str(preparation.id),
        change_summary={
            "operation": preparation.operation.value,
            "aggregate_id": preparation.aggregate_id,
            "reason": reason[:2000],
        },
        trace_id=trace_id,
    )
    return preparation


# ------------------------------------------------------------------ 提交
async def commit(
    session: AsyncSession,
    *,
    preparation_id: uuid.UUID,
    actor_id: uuid.UUID,
    facts: SideEffectFacts,
    settings: Settings | None = None,
    signer: SignerPort | None = None,
    now: datetime | None = None,
    trace_id: str | None = None,
) -> HarnessSideEffectPreparation:
    """提交：重算事实 → 校验审批 → 签发并**原子消费**单次能力 → 置 `COMMITTED`。

    与执行链路一样，单次能力在提交这一步就被消费掉：第二次提交在令牌层面不可能，
    而执行侧的防重放由**执行租约**单独负责（`claim_execution`）——
    两件事分开，是因为「提交一次」与「副作用只发生一次」不是同一个保证。
    """
    resolved = settings if settings is not None else get_settings()
    moment = now if now is not None else datetime.now(UTC)
    preparation = await _require(session, preparation_id)
    await _require_usable(session, preparation, facts=facts, now=moment)
    if preparation.status is not ExecutionPreparationStatus.READY:
        raise DomainError(f"准备单当前状态为 {preparation.status.value}，只有 READY 才能提交")

    bound_intent = _subject_bound(facts.intent, subject_id=str(actor_id))
    resolved_registry = build_tool_registry(seatunnel_version=resolved.seatunnel_version)
    bundle = build_policy_bundle(version=resolved.harness_policy_bundle_version)
    spec = resolved_registry.resolve(name=bound_intent.tool_name, version=bound_intent.tool_version)
    decision = bundle.evaluate(bound_intent, spec=spec)
    # 比的是**准备单上冻结的等级**与当下重新判定的结果。拿两个都来自当下推导的值互比
    # 永远相等，等于没检。
    if decision.risk_level is not preparation.risk_level:
        raise DomainError(
            f"风险等级与准备单不一致（准备单 {preparation.risk_level.value}，"
            f"当下判定 {decision.risk_level.value}），审批批的是旧等级，请重新申请"
        )

    bindings = bindings_of_facts(facts)
    token = issue_capability(
        preparation_id=str(preparation.id),
        tool_name=bound_intent.tool_name,
        tool_version=bound_intent.tool_version,
        subject_id=str(actor_id),
        environment=preparation.environment,
        policy_bundle_version=decision.policy_bundle_version,
        risk_level=decision.risk_level,
        ttl_seconds=resolved.harness_capability_ttl_seconds,
        signer=signer if signer is not None else create_capability_signer(),
        now=moment,
        **bindings,
    )
    await _record_grant(session, preparation_id=preparation.id, token=token)
    await consume_grant(
        session,
        token_sha256=token.token_sha256(),
        subject_id=str(actor_id),
        now=moment,
    )

    preparation.status = ExecutionPreparationStatus.COMMITTED
    preparation.committed_by = actor_id
    preparation.committed_at = moment
    await session.flush()

    await ledger.append(
        session,
        aggregate_type=preparation.aggregate_type,
        aggregate_id=preparation.aggregate_id,
        event_type="SIDE_EFFECT_COMMITTED",
        risk_level=decision.risk_level,
        payload={
            "preparation_id": str(preparation.id),
            "operation": preparation.operation.value,
            "tool": f"{bound_intent.tool_name}@{bound_intent.tool_version}",
            "token_id": token.claims.token_id,
            "risk_level": decision.risk_level.value,
            "matched_rules": list(decision.matched_rules),
            "fact_fingerprint": preparation.fact_fingerprint,
        },
        signer=create_capability_signer() if signer is None else signer,
        trace_id=trace_id,
    )
    record_audit_event(
        session,
        event_type=AuditEventType.SIDE_EFFECT_COMMITTED,
        target_type="harness_side_effect_preparation",
        project_id=preparation.project_id,
        actor_id=actor_id,
        target_id=str(preparation.id),
        change_summary={
            "operation": preparation.operation.value,
            "aggregate_id": preparation.aggregate_id,
            "token_id": token.claims.token_id,
            "risk_level": decision.risk_level.value,
            "fact_fingerprint": preparation.fact_fingerprint,
        },
        trace_id=trace_id,
    )
    return preparation


# ------------------------------------------------------------------ 执行租约与验签
async def claim_execution(
    session: AsyncSession,
    *,
    preparation_id: uuid.UUID,
    owner: str,
    settings: Settings | None = None,
    now: datetime | None = None,
) -> HarnessSideEffectPreparation:
    """取执行租约 —— 副作用「只发生一次」的那道闸。

    一条带条件的原子 UPDATE：只有 `COMMITTED + 未执行 + （没有租约或租约已过期）`
    的行会被抢到，行数为 0 就说明别人正在做，抛 `SideEffectLeaseBusy` 让调用方
    安全延迟重试。不用「先查再写」：两个 Worker 会同时查到「没人做」。
    """
    resolved = settings if settings is not None else get_settings()
    moment = now if now is not None else datetime.now(UTC)
    deadline = moment + timedelta(seconds=resolved.harness_outbox_lease_seconds)
    result = await session.execute(
        update(HarnessSideEffectPreparation)
        .where(
            HarnessSideEffectPreparation.id == preparation_id,
            HarnessSideEffectPreparation.status == ExecutionPreparationStatus.COMMITTED,
            HarnessSideEffectPreparation.execution_status == SideEffectExecutionStatus.PENDING,
            or_(
                HarnessSideEffectPreparation.execution_lease_expires_at.is_(None),
                HarnessSideEffectPreparation.execution_lease_expires_at < moment,
            ),
        )
        .values(
            execution_lease_owner=owner,
            execution_lease_expires_at=deadline,
            lock_version=HarnessSideEffectPreparation.lock_version + 1,
        )
        .execution_options(synchronize_session=False)
    )
    if cast("CursorResult[Any]", result).rowcount != 1:
        await _explain_busy(session, preparation_id=preparation_id, owner=owner)
    return await _require(session, preparation_id, refresh=True)


async def finish_execution(
    session: AsyncSession,
    *,
    preparation_id: uuid.UUID,
    owner: str,
    succeeded: bool,
    failure_reason: str | None = None,
    trace_id: str | None = None,
) -> HarnessSideEffectPreparation:
    """落执行终态并释放租约；**只有租约持有人**能落。"""
    result = await session.execute(
        update(HarnessSideEffectPreparation)
        .where(
            HarnessSideEffectPreparation.id == preparation_id,
            HarnessSideEffectPreparation.execution_lease_owner == owner,
        )
        .values(
            execution_status=(
                SideEffectExecutionStatus.APPLIED if succeeded else SideEffectExecutionStatus.FAILED
            ),
            execution_lease_owner=None,
            execution_lease_expires_at=None,
            failure_reason=None if succeeded else (failure_reason or "")[:2000],
            lock_version=HarnessSideEffectPreparation.lock_version + 1,
        )
        .execution_options(synchronize_session=False)
    )
    if cast("CursorResult[Any]", result).rowcount != 1:
        raise DomainError(f"准备单 {preparation_id} 的执行租约不属于 {owner}，拒绝落终态")
    preparation = await _require(session, preparation_id, refresh=True)
    record_audit_event(
        session,
        event_type=(
            AuditEventType.SIDE_EFFECT_APPLIED if succeeded else AuditEventType.SIDE_EFFECT_FAILED
        ),
        target_type="harness_side_effect_preparation",
        project_id=preparation.project_id,
        target_id=str(preparation.id),
        change_summary={
            "operation": preparation.operation.value,
            "aggregate_id": preparation.aggregate_id,
            "execution_status": preparation.execution_status.value,
            "failure_reason": preparation.failure_reason,
        },
        trace_id=trace_id,
    )
    return preparation


async def verify_grant(
    session: AsyncSession,
    *,
    preparation: HarnessSideEffectPreparation,
    verifier: VerifierPort | None = None,
    now: datetime | None = None,
) -> SignedCapability:
    """执行前的验签：确认这条副作用确实由持钥方授权过（FR-45）。

    期望值全部取自**准备单冻结的事实**，不来自命令载荷 —— 载荷在队列里可以被改，
    准备单不行。`DomainError` 是这里的统一出口：`CapabilityMismatch` 刻意不继承
    `DomainError`（它是安全边界上的拒绝），不显式接住就会穿透成一个没有结论的异常。
    """
    grant = await session.scalar(
        select(HarnessSideEffectCapabilityGrant)
        .where(HarnessSideEffectCapabilityGrant.preparation_id == preparation.id)
        .order_by(HarnessSideEffectCapabilityGrant.issued_at.desc())
    )
    if grant is None:
        raise DomainError("这张副作用准备单没有对应的能力令牌，拒绝执行")
    token = token_from_grant(
        claims_payload=grant.claims,
        algorithm=grant.claims_signature_algorithm,
        key_id=grant.key_id,
        signature=grant.claims_signature_value,
    )
    try:
        verify_capability(
            token,
            expectation=_expectation(preparation),
            verifier=verifier if verifier is not None else create_capability_verifier(),
            now=now,
        )
    except Exception as exc:  # SignatureError / CapabilityMismatch 都属于「不放行」
        raise DomainError(f"能力令牌校验未通过，拒绝执行：{exc}") from exc
    return token


async def require_not_drifted(
    session: AsyncSession,
    *,
    preparation: HarnessSideEffectPreparation,
    facts: SideEffectFacts,
) -> None:
    """执行前查「事实还成立吗」。不成立就**不执行**，也不假装成功。

    只查漂移，不查 TTL：这时候该管的是令牌的有效期，不是准备单的审批窗口。
    """
    if preparation.status is not ExecutionPreparationStatus.COMMITTED:
        raise DomainError(f"准备单当前状态为 {preparation.status.value}，未提交的副作用不得执行")
    await _require_facts_unchanged(session, preparation, facts=facts)


# ------------------------------------------------------------------ 查询
async def get(
    session: AsyncSession, *, preparation_id: uuid.UUID, actor_id: uuid.UUID
) -> HarnessSideEffectPreparation:
    preparation = await _require(session, preparation_id)
    await require_project_roles(
        session, project_id=preparation.project_id, actor_id=actor_id, allowed=READABLE
    )
    return preparation


async def list_for_project(
    session: AsyncSession,
    *,
    project_id: uuid.UUID,
    actor_id: uuid.UUID,
    operation: SideEffectOperation | None = None,
    status: ExecutionPreparationStatus | None = None,
) -> Sequence[HarnessSideEffectPreparation]:
    await require_project_roles(session, project_id=project_id, actor_id=actor_id, allowed=READABLE)
    statement = select(HarnessSideEffectPreparation).where(
        HarnessSideEffectPreparation.project_id == project_id
    )
    if operation is not None:
        statement = statement.where(HarnessSideEffectPreparation.operation == operation)
    if status is not None:
        statement = statement.where(HarnessSideEffectPreparation.status == status)
    rows = await session.scalars(statement.order_by(HarnessSideEffectPreparation.created_at.desc()))
    return rows.all()


async def approvals_of(
    session: AsyncSession, *, preparation_id: uuid.UUID
) -> Sequence[HarnessSideEffectApproval]:
    rows = await session.scalars(
        select(HarnessSideEffectApproval)
        .where(HarnessSideEffectApproval.preparation_id == preparation_id)
        .order_by(HarnessSideEffectApproval.decided_at)
    )
    return rows.all()


async def find_for_aggregate(
    session: AsyncSession, *, aggregate_type: str, aggregate_id: str
) -> HarnessSideEffectPreparation | None:
    """按业务事实反查准备单：九类操作共用一张表，聚合对象是它们的定位键。"""
    return await session.scalar(
        select(HarnessSideEffectPreparation).where(
            HarnessSideEffectPreparation.aggregate_type == aggregate_type,
            HarnessSideEffectPreparation.aggregate_id == aggregate_id,
        )
    )


# ------------------------------------------------------------------ 内部
async def _require_usable(
    session: AsyncSession,
    preparation: HarnessSideEffectPreparation,
    *,
    facts: SideEffectFacts,
    now: datetime,
) -> None:
    """确认这张准备单**还能用、还代表当下的事实**；不代表就作废并拒绝。

    过期与「事实变了」都落到 `EXPIRED`：对调用方的含义一样 —— 这份准备单不能再用了。
    已记下的审批一并失效，因为它们批的是旧事实。
    """
    if preparation.status in _TERMINAL:
        raise DomainError(f"准备单已经是 {preparation.status.value}，不能再使用")
    if as_utc(preparation.expires_at) <= now:
        preparation.status = ExecutionPreparationStatus.EXPIRED
        await session.flush()
        raise DomainError("副作用准备单已过期，请重新申请")
    await _require_facts_unchanged(session, preparation, facts=facts)


async def _require_facts_unchanged(
    session: AsyncSession,
    preparation: HarnessSideEffectPreparation,
    *,
    facts: SideEffectFacts,
) -> None:
    """重算事实并与准备单冻结的那份逐项比对。

    与 `_require_usable` 分开是因为执行侧要走的路不同：一张**已提交**的准备单当然
    「不能再审批」，但它正该被执行 —— 把两者并成一个判断，会让所有已提交的副作用
    在执行时被自己的终态挡在门外（写这段时踩过一次，回归用例见
    `test_side_effect_protocol.py::test_execution_happens_once_and_consumes_the_lease`）。
    执行侧也不查准备单的 TTL：那个窗口管的是「审批对象还在不在」，
    真到了执行那一步，可执行性由**能力令牌自己的有效期**说了算（`verify_grant`）。
    """
    drifted = [
        name
        for name, frozen, recomputed in (
            ("fact_fingerprint", preparation.fact_fingerprint, facts.fact_fingerprint()),
            ("input_fingerprint", preparation.input_fingerprint, facts.input_fingerprint()),
            (
                "resource_fingerprint",
                preparation.resource_fingerprint,
                facts.resource_fingerprint(),
            ),
            (
                "data_sensitivity",
                preparation.fact_summary.get("data_sensitivity"),
                facts.intent.data_sensitivity.value,
            ),
        )
        if frozen != recomputed
    ]
    if drifted:
        preparation.status = ExecutionPreparationStatus.EXPIRED
        await session.flush()
        raise VersionConflict(
            "准备单冻结的事实已经变化（" + "、".join(drifted) + "），已有审批一并失效，请重新申请"
        )


def _expectation(preparation: HarnessSideEffectPreparation) -> CapabilityExpectation:
    bindings = bindings_of_preparation(preparation)
    return CapabilityExpectation(
        tool_name=preparation.tool_name,
        tool_version=preparation.tool_version,
        subject_id=str(preparation.committed_by or ""),
        environment=preparation.environment,
        preparation_id=str(preparation.id),
        **bindings,
    )


def _subject_bound(intent: ToolIntent, *, subject_id: str) -> ToolIntent:
    """把主体补进意图：Prepare 阶段还不知道是谁来提交。"""
    return replace(intent, subject_id=subject_id)


def _single_slot(preparation: HarnessSideEffectPreparation) -> HarnessRole:
    roles = preparation.required_roles
    if len(roles) != 1:
        raise DomainError(
            f"该准备单需要 {len(roles)} 个审批槽（{'、'.join(roles) or '无'}），"
            "审批时必须指明是哪一个"
        )
    return HarnessRole(roles[0])


def _record_decision(
    session: AsyncSession, *, facts: SideEffectFacts, decision: PolicyDecision
) -> HarnessPolicyDecision:
    """PDP 决策落成事实，作为准备单的可追溯依据（FR-49 的落地点）。"""
    row = HarnessPolicyDecision(
        tool_name=facts.intent.tool_name,
        tool_version=facts.intent.tool_version,
        outcome=decision.outcome,
        risk_level=decision.risk_level,
        policy_bundle_version=decision.policy_bundle_version,
        request_fingerprint=facts.intent.fingerprint(),
        resource_fingerprint=facts.resource_fingerprint(),
        matched_rules=list(decision.matched_rules),
        required_roles=[role.value for role in decision.required_roles],
        required_approvals=decision.required_approvals,
        subject_id=facts.intent.subject_id,
        environment=facts.intent.environment,
        reason=decision.reason,
    )
    session.add(row)
    return row


async def _record_grant(
    session: AsyncSession, *, preparation_id: uuid.UUID, token: SignedCapability
) -> HarnessSideEffectCapabilityGrant:
    grant = HarnessSideEffectCapabilityGrant(
        token_id=uuid.UUID(token.claims.token_id),
        preparation_id=preparation_id,
        token_sha256=token.token_sha256(),
        key_id=token.key_id,
        claims=token.claims.canonical_payload(),
        claims_signature_algorithm=token.algorithm,
        claims_signature_value=token.signature,
        subject_id=token.claims.subject_id,
        environment=token.claims.environment,
        issued_at=token.claims.issued_at,
        expires_at=token.claims.expires_at,
        single_use=token.claims.single_use,
    )
    session.add(grant)
    try:
        await session.flush()
    except IntegrityError as exc:
        raise VersionConflict("该能力令牌已登记过，不能重复登记") from exc
    return grant


async def consume_grant(
    session: AsyncSession,
    *,
    token_sha256: str,
    subject_id: str,
    now: datetime | None = None,
) -> HarnessSideEffectCapabilityGrant:
    """原子消费一张副作用能力令牌；第二次使用必失败（防重放）。

    与执行链路的 `grants.consume` 是同一套写法，只是表不同（本表没有 `execution_run_id`：
    副作用的执行对象是它自己的聚合对象，不是一次运行）。
    """
    result = await session.execute(
        update(HarnessSideEffectCapabilityGrant)
        .where(
            HarnessSideEffectCapabilityGrant.token_sha256 == token_sha256,
            HarnessSideEffectCapabilityGrant.consumed_at.is_(None),
            HarnessSideEffectCapabilityGrant.subject_id == subject_id,
        )
        .values(
            consumed_at=now if now is not None else datetime.now(UTC),
            consumed_by=uuid.UUID(subject_id),
        )
        .execution_options(synchronize_session=False)
    )
    existing = await session.scalar(
        select(HarnessSideEffectCapabilityGrant).where(
            HarnessSideEffectCapabilityGrant.token_sha256 == token_sha256
        )
    )
    if cast("CursorResult[Any]", result).rowcount == 1:
        assert existing is not None
        return existing
    # 到这里已经确定不放行；下面只是把原因说清楚，不影响安全性。
    if existing is None:
        raise ResourceNotFound("能力令牌不存在或未被登记")
    if existing.subject_id != subject_id:
        raise DomainError("能力令牌的主体与本次提交不一致")
    raise VersionConflict("该能力令牌已被使用过（单次令牌不允许重放）")


async def _explain_busy(session: AsyncSession, *, preparation_id: uuid.UUID, owner: str) -> None:
    """把「没抢到租约」的原因说清楚，然后一律抛 `SideEffectLeaseBusy`。"""
    preparation = await session.get(HarnessSideEffectPreparation, preparation_id)
    if preparation is None:
        raise ResourceNotFound("副作用准备单不存在")
    if preparation.status is not ExecutionPreparationStatus.COMMITTED:
        raise DomainError(f"准备单当前状态为 {preparation.status.value}，未提交的副作用不得执行")
    if preparation.execution_status is not SideEffectExecutionStatus.PENDING:
        raise DomainError(
            f"这次副作用已经执行过（{preparation.execution_status.value}），不再重复执行"
        )
    raise SideEffectLeaseBusy(
        f"准备单 {preparation_id} 的执行租约由 {preparation.execution_lease_owner!r} 持有，"
        f"{owner!r} 稍后重试"
    )


async def _require(
    session: AsyncSession, preparation_id: uuid.UUID, *, refresh: bool = False
) -> HarnessSideEffectPreparation:
    preparation = await session.get(
        HarnessSideEffectPreparation,
        preparation_id,
        # 上面的 UPDATE 走 SQL 侧表达式（`lock_version + 1`），会话里那份对象还是旧值；
        # 不强制刷新的话调用方读到的一直是更新前的数字。
        populate_existing=refresh,
    )
    if preparation is None:
        raise ResourceNotFound("副作用准备单不存在")
    return preparation


async def _by_idempotency_key(
    session: AsyncSession, key: str
) -> HarnessSideEffectPreparation | None:
    return await session.scalar(
        select(HarnessSideEffectPreparation).where(
            HarnessSideEffectPreparation.idempotency_key == key
        )
    )


def side_effect_idempotency_key(
    *, operation: SideEffectOperation, aggregate_type: str, aggregate_id: str
) -> str:
    """由操作与聚合对象推导的幂等键。

    刻意不是随机值：同一个聚合对象上的同一类操作**只应该有一张申请单**。
    随机键会让重放的申请各造一张，而它们动的是同一样东西。
    """
    return f"side-effect:{operation.value}:{aggregate_type}:{aggregate_id}"
