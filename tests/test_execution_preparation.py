"""执行准备单与版本审批（T-33 / FR-51、FR-52、FR-55 的前半条）。

两条最要紧的：

- **Prepare 零外部副作用**：整个推导过程里没有任何 socket 连接 —— 不是「我们小心地
  没连」，而是测试把 `socket.connect` 换成会炸的实现，真连了就红。
- **事实全部来自服务端**：指纹是从冻结的 plan 与策略推导出来的，请求体里除幂等键外
  什么都没传，因此客户端无从伪造风险等级、工具版本或资源范围。
"""

import socket
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from data_integration_platform.application.generation import EtlPlan
from data_integration_platform.application.harness.pdp import build_policy_bundle
from data_integration_platform.application.harness.preparation import (
    EXECUTION_TOOL,
    PUBLISH_STRATEGY,
    derive_facts,
    prepare,
    require_not_expired,
)
from data_integration_platform.application.harness.tools import build_tool_registry
from data_integration_platform.application.versions import PipelineVersionService
from data_integration_platform.config import Settings
from data_integration_platform.domain.enums import (
    ApprovalDecisionValue,
    ApprovalStatus,
    PipelineVersionStatus,
    ProjectRole,
)
from data_integration_platform.domain.errors import (
    DomainError,
    PermissionDenied,
    ResourceNotFound,
    VersionConflict,
)
from data_integration_platform.domain.harness import (
    DataSensitivity,
    RiskLevel,
    ToolIntentOutcome,
)
from data_integration_platform.infrastructure.models import (
    ApprovalRequest,
    ExecutionPreparation,
    HarnessPolicyDecision,
    PipelineVersion,
    ProjectMembership,
    User,
)
from tests.conftest import ProjectContext
from tests.test_deterministic_gate import baseline_plan
from tests.test_pipeline_versions import candidate, ensure_execution_endpoints

ARTIFACT = "a" * 64


@pytest.fixture
def settings() -> Settings:
    return Settings(_env_file=None, environment="local", seatunnel_version="2.3.13")


def masked_version(**plan_overrides: object) -> PipelineVersion:
    """一份带 `email → email_masked` 映射的版本：分类的血缘传播要靠它才看得见。"""
    plan = baseline_plan(**plan_overrides)
    return PipelineVersion(
        pipeline_id=uuid.uuid4(),
        version_number=1,
        status=PipelineVersionStatus.APPROVED,
        requirement="脱敏后同步",
        plan=plan,
        artifact_sha256=ARTIFACT,
        created_by=uuid.uuid4(),
    )


def facts_for(version: PipelineVersion, *, settings: Settings, environment: str = "local"):
    return derive_facts(
        version=version,
        project_id=uuid.UUID(int=1),
        environment=environment,
        registry=build_tool_registry(seatunnel_version=settings.seatunnel_version),
        bundle=build_policy_bundle(version=settings.harness_policy_bundle_version),
        platform_budget=settings.harness_runtime_budget(),
    )


async def approved_version(
    session: AsyncSession,
    project: ProjectContext,
    tmp_path: Path,
    *,
    approve: bool = True,
) -> PipelineVersion:
    """造一个走到 APPROVED 的版本；`approve=False` 时停在 WAITING_APPROVAL。"""
    await ensure_execution_endpoints(session, project)
    versions = PipelineVersionService(root=tmp_path / "artifacts")
    version = await versions.create(
        session,
        project_id=project.project_id,
        actor_id=project.engineer_id,
        name=f"orders-sync-{uuid.uuid4().hex[:6]}",
        requirement="把订单同步到宽表",
    )
    version.status = PipelineVersionStatus.GENERATED
    await session.flush()
    await versions.freeze(
        session, version_id=version.id, actor_id=project.engineer_id, candidate=candidate()
    )
    request = await versions.submit_for_approval(
        session, version_id=version.id, actor_id=project.engineer_id
    )
    if not approve:
        return version
    approver = User(subject=f"approver-{uuid.uuid4().hex[:6]}", display_name="审批人")
    session.add(approver)
    await session.flush()
    session.add(
        ProjectMembership(
            project_id=project.project_id, user_id=approver.id, role=ProjectRole.APPROVER
        )
    )
    await session.flush()
    await versions.decide_approval(
        session,
        approval_id=request.id,
        actor_id=approver.id,
        decision=ApprovalDecisionValue.APPROVE,
        reason="字段映射与质量规则核对无误",
    )
    return version


# ------------------------------------------------------------------ 版本审批
async def test_approval_moves_the_version_to_approved(
    session: AsyncSession, project: ProjectContext, tmp_path: Path
) -> None:
    version = await approved_version(session, project, tmp_path)

    assert version.status is PipelineVersionStatus.APPROVED


async def test_applicant_cannot_approve_their_own_submission(
    session: AsyncSession, project: ProjectContext, tmp_path: Path
) -> None:
    """SOD-01：四眼原则的红线。"""
    await ensure_execution_endpoints(session, project)
    versions = PipelineVersionService(root=tmp_path / "artifacts")
    version = await versions.create(
        session,
        project_id=project.project_id,
        actor_id=project.engineer_id,
        name=f"self-{uuid.uuid4().hex[:6]}",
        requirement="自己批自己",
    )
    version.status = PipelineVersionStatus.GENERATED
    await session.flush()
    await versions.freeze(
        session, version_id=version.id, actor_id=project.engineer_id, candidate=candidate()
    )
    request = await versions.submit_for_approval(
        session, version_id=version.id, actor_id=project.engineer_id
    )
    # 让申请人**同时**具备审批资格：否则先撞上的是「无权访问该项目资源」，
    # 而这条用例要验的恰恰是「有资格也不许批自己」。
    # 成员表在 (project_id, user_id) 上有唯一约束，所以是改这一行而不是加一行。
    membership = await session.scalar(
        select(ProjectMembership).where(
            ProjectMembership.project_id == project.project_id,
            ProjectMembership.user_id == project.engineer_id,
        )
    )
    assert membership is not None
    membership.role = ProjectRole.APPROVER
    await session.flush()

    with pytest.raises(PermissionDenied, match="SOD-01"):
        await versions.decide_approval(
            session,
            approval_id=request.id,
            actor_id=project.engineer_id,
            decision=ApprovalDecisionValue.APPROVE,
            reason="我自己批的",
        )


async def test_changed_artifact_invalidates_a_pending_approval(
    session: AsyncSession, project: ProjectContext, tmp_path: Path
) -> None:
    """SOD-04：审批绑的是制品摘要，内容一变这份审批就不该再生效。"""
    await ensure_execution_endpoints(session, project)
    versions = PipelineVersionService(root=tmp_path / "artifacts")
    version = await versions.create(
        session,
        project_id=project.project_id,
        actor_id=project.engineer_id,
        name=f"stale-{uuid.uuid4().hex[:6]}",
        requirement="审批期间内容被改",
    )
    version.status = PipelineVersionStatus.GENERATED
    await session.flush()
    await versions.freeze(
        session, version_id=version.id, actor_id=project.engineer_id, candidate=candidate()
    )
    request = await versions.submit_for_approval(
        session, version_id=version.id, actor_id=project.engineer_id
    )
    approver = User(subject=f"approver-{uuid.uuid4().hex[:6]}", display_name="审批人")
    session.add(approver)
    await session.flush()
    session.add(
        ProjectMembership(
            project_id=project.project_id, user_id=approver.id, role=ProjectRole.APPROVER
        )
    )
    await session.flush()
    # 制品摘要在审批期间被换掉。
    version.artifact_sha256 = "f" * 64
    await session.flush()

    with pytest.raises(VersionConflict, match="内容在审批期间被改过"):
        await versions.decide_approval(
            session,
            approval_id=request.id,
            actor_id=approver.id,
            decision=ApprovalDecisionValue.APPROVE,
            reason="通过",
        )


async def test_rejection_terminates_the_version(
    session: AsyncSession, project: ProjectContext, tmp_path: Path
) -> None:
    await ensure_execution_endpoints(session, project)
    versions = PipelineVersionService(root=tmp_path / "artifacts")
    version = await versions.create(
        session,
        project_id=project.project_id,
        actor_id=project.engineer_id,
        name=f"reject-{uuid.uuid4().hex[:6]}",
        requirement="会被驳回的版本",
    )
    version.status = PipelineVersionStatus.GENERATED
    await session.flush()
    await versions.freeze(
        session, version_id=version.id, actor_id=project.engineer_id, candidate=candidate()
    )
    request = await versions.submit_for_approval(
        session, version_id=version.id, actor_id=project.engineer_id
    )
    approver = User(subject=f"approver-{uuid.uuid4().hex[:6]}", display_name="审批人")
    session.add(approver)
    await session.flush()
    session.add(
        ProjectMembership(
            project_id=project.project_id, user_id=approver.id, role=ProjectRole.APPROVER
        )
    )
    await session.flush()

    decided = await versions.decide_approval(
        session,
        approval_id=request.id,
        actor_id=approver.id,
        decision=ApprovalDecisionValue.REJECT,
        reason="金额字段没做非空校验",
    )

    assert decided.status is ApprovalStatus.REJECTED
    assert version.status is PipelineVersionStatus.REJECTED


async def test_approved_request_is_bound_to_the_version_digest(
    session: AsyncSession, project: ProjectContext, tmp_path: Path
) -> None:
    """审批单上留的摘要是版本当时那份制品的摘要 —— 审批绑内容而不是绑「点过按钮」。"""
    version = await approved_version(session, project, tmp_path)

    request = await session.scalar(
        select(ApprovalRequest).where(ApprovalRequest.version_id == version.id)
    )

    assert request is not None
    assert request.status is ApprovalStatus.APPROVED
    assert request.artifact_sha256 == version.artifact_sha256
    assert request.decided_by is not None and request.decided_by != request.requested_by


# ------------------------------------------------------------------ Prepare 推导
def test_facts_are_derived_from_the_frozen_plan(settings: Settings) -> None:
    version = PipelineVersion(
        pipeline_id=uuid.uuid4(),
        version_number=1,
        status=PipelineVersionStatus.APPROVED,
        requirement="把订单同步到宽表",
        plan=candidate().plan.model_dump(mode="json"),
        artifact_sha256=ARTIFACT,
        created_by=uuid.uuid4(),
    )

    facts = facts_for(version, settings=settings)

    assert facts.spec_tool_name == EXECUTION_TOOL
    assert facts.spec_tool_version == "2.3.13"
    assert facts.decision.outcome is ToolIntentOutcome.REQUIRE_APPROVAL
    assert facts.decision.risk_level is RiskLevel.P2
    assert facts.rollback_plan["strategy"] == PUBLISH_STRATEGY
    assert facts.rollback_plan["shadow_table"].endswith("__shadow")


def test_classification_propagates_through_the_plan_mappings(settings: Settings) -> None:
    """`email_masked` 认不出名字，但它来自 `email` —— 血缘传播在这里真的用上了。"""
    facts = facts_for(masked_version(), settings=settings)

    assert facts.classification.classification_of("email") is DataSensitivity.CONFIDENTIAL
    assert facts.classification.classification_of("email_masked") is DataSensitivity.CONFIDENTIAL


def test_unclassified_columns_make_the_whole_schema_unknown(settings: Settings) -> None:
    """**整表分级是保守的**：只要还有没分类的列，整表就记 UNKNOWN。

    连一半列是什么都不知道，却说「这张表只是机密级」是假保证。缺哪几列由
    `unresolved` 单独给出，信息没丢，只是没混进分级里。
    """
    facts = facts_for(masked_version(), settings=settings)

    assert facts.classification.overall is DataSensitivity.UNKNOWN
    assert facts.intent.data_sensitivity is DataSensitivity.UNKNOWN
    assert "id" in facts.classification.unresolved
    assert "email" not in facts.classification.unresolved


def test_fully_classified_schema_reports_its_strictest_level(settings: Settings) -> None:
    """反过来也要成立：全部列都能判时，整表就等于最严的那一列。"""
    plan = baseline_plan(
        mappings=[
            {"source_column": "amount", "target_column": "amount"},
            {"source_column": "email", "target_column": "email_masked", "transforms": ["mask"]},
        ],
        transforms=[{"name": "mask", "kind": "MASK", "target_column": "email_masked"}],
        dedup_keys=["amount"],
    )
    version = PipelineVersion(
        pipeline_id=uuid.uuid4(),
        version_number=1,
        status=PipelineVersionStatus.APPROVED,
        requirement="全部分类得出",
        plan=plan,
        artifact_sha256=ARTIFACT,
        created_by=uuid.uuid4(),
    )

    facts = facts_for(version, settings=settings)

    assert facts.classification.unresolved == ()
    assert facts.classification.overall is DataSensitivity.CONFIDENTIAL


def test_plan_budget_can_only_tighten_the_platform_ceiling(settings: Settings) -> None:
    from data_integration_platform.application.generation import RuntimeBudgetSpec

    plan = candidate().plan.model_copy(update={"budget": RuntimeBudgetSpec(max_records=100)})
    version = PipelineVersion(
        pipeline_id=uuid.uuid4(),
        version_number=1,
        status=PipelineVersionStatus.APPROVED,
        requirement="收紧预算",
        plan=plan.model_dump(mode="json"),
        artifact_sha256=ARTIFACT,
        created_by=uuid.uuid4(),
    )

    facts = facts_for(version, settings=settings)

    assert facts.budget.max_records == 100
    assert facts.budget.max_bytes == settings.harness_runtime_max_bytes


def test_impact_report_does_not_guess_whether_the_target_exists(settings: Settings) -> None:
    """目标表存不存在要连库才知道，而 Prepare 不碰外部系统 —— 不猜。"""
    report = facts_for(masked_version(), settings=settings).impact_report

    assert "exists" not in report["target"]
    assert report["target"]["schema_name"] == "dw"
    assert report["publish_strategy"] == PUBLISH_STRATEGY
    assert report["data_classification"]["overall"] == DataSensitivity.UNKNOWN.value
    # 未解析清单要在影响范围里可见：审批人据此知道还有哪些列没分类。
    assert "id" in report["data_classification"]["unresolved"]


def test_unknown_environment_is_refused_at_prepare(settings: Settings) -> None:
    """环境名拼错时不该产出准备单：那张准备单走到审批那里只是浪费人的时间。"""
    version = PipelineVersion(
        pipeline_id=uuid.uuid4(),
        version_number=1,
        status=PipelineVersionStatus.APPROVED,
        requirement="环境拼错了",
        plan=candidate().plan.model_dump(mode="json"),
        artifact_sha256=ARTIFACT,
        created_by=uuid.uuid4(),
    )

    with pytest.raises(DomainError, match="不生成准备单"):
        facts_for(version, settings=settings, environment="prod-ish")


def test_fingerprints_are_stable_for_the_same_inputs(settings: Settings) -> None:
    """同一份冻结内容重复推导必须得到同一组指纹，否则 Commit 的重算永远对不上。"""
    plan = candidate().plan.model_dump(mode="json")
    version = PipelineVersion(
        pipeline_id=uuid.uuid4(),
        version_number=1,
        status=PipelineVersionStatus.APPROVED,
        requirement="复现",
        plan=plan,
        artifact_sha256=ARTIFACT,
        created_by=uuid.uuid4(),
    )

    first = facts_for(version, settings=settings)
    second = facts_for(version, settings=settings)

    assert first.input_fingerprint() == second.input_fingerprint()
    assert first.scope.fingerprint() == second.scope.fingerprint()


# ------------------------------------------------------------------ Prepare 落库
async def test_prepare_lands_a_preparation_without_any_socket(
    session: AsyncSession,
    project: ProjectContext,
    tmp_path: Path,
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """**零外部副作用**：把 socket 连接换成会炸的实现，Prepare 全程不该碰它。"""
    version = await approved_version(session, project, tmp_path)

    def explode(*args: object, **kwargs: object) -> None:
        raise AssertionError("Prepare 不该发起任何外部连接")

    monkeypatch.setattr(socket.socket, "connect", explode)
    monkeypatch.setattr(socket, "create_connection", explode)

    preparation = await prepare(
        session,
        version_id=version.id,
        actor_id=project.engineer_id,
        idempotency_key="prep-key-0001",
        settings=settings,
    )

    assert preparation.status.value == "WAITING_APPROVAL"
    assert preparation.risk_level is RiskLevel.P2
    assert preparation.required_approvals == 1
    assert preparation.required_roles == ["DATA_APPROVER"]
    assert preparation.expires_at > preparation.created_at.replace(tzinfo=UTC)


async def test_prepare_records_the_policy_decision(
    session: AsyncSession,
    project: ProjectContext,
    tmp_path: Path,
    settings: Settings,
) -> None:
    """决策要可追溯：命中了哪条规则、按哪版策略判的，都得留在库里。"""
    version = await approved_version(session, project, tmp_path)

    preparation = await prepare(
        session,
        version_id=version.id,
        actor_id=project.engineer_id,
        idempotency_key="prep-key-0002",
        settings=settings,
    )

    decision = await session.get(HarnessPolicyDecision, preparation.policy_decision_id)
    assert decision is not None
    assert decision.matched_rules[0] == "PLAIN_WRITE"
    assert decision.policy_bundle_version == settings.harness_policy_bundle_version
    assert decision.resource_fingerprint == preparation.resource_fingerprint


async def test_prepare_requires_an_approved_version(
    session: AsyncSession,
    project: ProjectContext,
    tmp_path: Path,
    settings: Settings,
) -> None:
    """还没被人批准过的内容不该进到执行准备里。"""
    version = await approved_version(session, project, tmp_path, approve=False)

    with pytest.raises(DomainError, match="只有已批准的版本"):
        await prepare(
            session,
            version_id=version.id,
            actor_id=project.engineer_id,
            idempotency_key="prep-key-0003",
            settings=settings,
        )


async def test_prepare_is_idempotent_by_key(
    session: AsyncSession,
    project: ProjectContext,
    tmp_path: Path,
    settings: Settings,
) -> None:
    """重试同一个幂等键要拿回同一份准备单，而不是造出第二份待审批的。"""
    version = await approved_version(session, project, tmp_path)

    first = await prepare(
        session,
        version_id=version.id,
        actor_id=project.engineer_id,
        idempotency_key="prep-key-0004",
        settings=settings,
    )
    second = await prepare(
        session,
        version_id=version.id,
        actor_id=project.engineer_id,
        idempotency_key="prep-key-0004",
        settings=settings,
    )

    assert first.id == second.id
    rows = await session.scalars(
        select(ExecutionPreparation).where(ExecutionPreparation.pipeline_version_id == version.id)
    )
    assert len(rows.all()) == 1


async def test_prepare_unknown_version_is_a_404(
    session: AsyncSession, project: ProjectContext, settings: Settings
) -> None:
    with pytest.raises(ResourceNotFound):
        await prepare(
            session,
            version_id=uuid.uuid4(),
            actor_id=project.engineer_id,
            idempotency_key="prep-key-0005",
            settings=settings,
        )


async def test_viewer_cannot_prepare(
    session: AsyncSession,
    project: ProjectContext,
    tmp_path: Path,
    settings: Settings,
) -> None:
    version = await approved_version(session, project, tmp_path, approve=False)
    version.status = PipelineVersionStatus.APPROVED
    viewer = User(subject=f"viewer-{uuid.uuid4().hex[:6]}", display_name="只读")
    session.add(viewer)
    await session.flush()
    session.add(
        ProjectMembership(project_id=project.project_id, user_id=viewer.id, role=ProjectRole.VIEWER)
    )
    await session.flush()

    with pytest.raises(PermissionDenied):
        await prepare(
            session,
            version_id=version.id,
            actor_id=viewer.id,
            idempotency_key="prep-key-0006",
            settings=settings,
        )


async def test_expired_preparation_is_refused(
    session: AsyncSession,
    project: ProjectContext,
    tmp_path: Path,
    settings: Settings,
) -> None:
    """FR-52：24 小时之后审批与提交都失效。"""
    version = await approved_version(session, project, tmp_path)
    preparation = await prepare(
        session,
        version_id=version.id,
        actor_id=project.engineer_id,
        idempotency_key="prep-key-0007",
        settings=settings,
        now=datetime(2026, 10, 16, 2, 0, tzinfo=UTC),
    )
    await session.flush()

    require_not_expired(preparation, now=datetime(2026, 10, 16, 20, 0, tzinfo=UTC))
    with pytest.raises(DomainError, match="已过期"):
        require_not_expired(preparation, now=datetime(2026, 10, 17, 4, 0, tzinfo=UTC))


def test_plan_contract_still_parses() -> None:
    """推导依赖 `plan` 能反序列化回 `EtlPlan`：冻结写入的是 `model_dump(mode="json")`。"""
    plan = candidate().plan

    assert EtlPlan.model_validate(plan.model_dump(mode="json")) == plan


def test_preparation_ttl_defaults_to_24_hours(settings: Settings) -> None:
    assert timedelta(seconds=settings.harness_preparation_ttl_seconds) == timedelta(hours=24)


async def test_pipeline_must_exist(
    session: AsyncSession, project: ProjectContext, settings: Settings
) -> None:
    """版本存在但管道被删：报 404 而不是 None 解引用。"""
    version = PipelineVersion(
        pipeline_id=uuid.uuid4(),
        version_number=1,
        status=PipelineVersionStatus.APPROVED,
        requirement="孤儿版本",
        plan=candidate().plan.model_dump(mode="json"),
        artifact_sha256=ARTIFACT,
        created_by=project.engineer_id,
    )
    session.add(version)
    await session.flush()

    with pytest.raises(ResourceNotFound, match="管道不存在"):
        await prepare(
            session,
            version_id=version.id,
            actor_id=project.engineer_id,
            idempotency_key="prep-key-0008",
            settings=settings,
        )
