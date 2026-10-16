"""准备单的具名审批（T-34 / FR-53、FR-54）。

证据是「内容变化使旧审批失效」—— 分两种变化各验一次：
**准备单过期**与**冻结事实变了**（这里用改了平台预算来模拟）。两种都要求
准备单被作废，而不是带着已经不相干的旧审批继续往前走。

其余守的是职责分离三件：申请人自批（SOD-01）、一人占多槽（SOD-02）、
以及槽位只能有一个决定。
"""

import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from data_integration_platform.application.harness.approval import decide
from data_integration_platform.application.harness.preparation import prepare
from data_integration_platform.application.versions import PipelineVersionService
from data_integration_platform.config import Settings
from data_integration_platform.domain.enums import (
    ApprovalDecisionValue,
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
    ExecutionPreparationStatus,
    HarnessApprovalValue,
    HarnessRole,
)
from data_integration_platform.infrastructure.models import (
    ExecutionPreparation,
    HarnessApprovalDecision,
    PipelineVersion,
    ProjectMembership,
    ProjectRoleGrant,
    User,
)
from tests.conftest import ProjectContext
from tests.test_pipeline_versions import candidate, ensure_execution_endpoints

ENGINEER = HarnessRole.DATA_ENGINEER


def settings(**overrides: object) -> Settings:
    values: dict[str, object] = {"_env_file": None, "environment": "local"}
    values.update(overrides)
    return Settings(**values)  # type: ignore[arg-type]


async def make_user(
    session: AsyncSession,
    project: ProjectContext,
    *,
    role: ProjectRole,
    harness_roles: tuple[HarnessRole, ...] = (),
) -> User:
    user = User(subject=f"actor-{uuid.uuid4().hex[:8]}", display_name="审批人")
    session.add(user)
    await session.flush()
    session.add(ProjectMembership(project_id=project.project_id, user_id=user.id, role=role))
    for harness_role in harness_roles:
        session.add(
            ProjectRoleGrant(project_id=project.project_id, user_id=user.id, role=harness_role)
        )
    await session.flush()
    return user


async def approved_version(
    session: AsyncSession, project: ProjectContext, tmp_path: Path
) -> PipelineVersion:
    await ensure_execution_endpoints(session, project)
    versions = PipelineVersionService(root=tmp_path / "artifacts")
    version = await versions.create(
        session,
        project_id=project.project_id,
        actor_id=project.engineer_id,
        name=f"orders-{uuid.uuid4().hex[:6]}",
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
    approver = await make_user(session, project, role=ProjectRole.APPROVER)
    await versions.decide_approval(
        session,
        approval_id=request.id,
        actor_id=approver.id,
        decision=ApprovalDecisionValue.APPROVE,
        reason="核对无误",
    )
    return version


async def prepared(
    session: AsyncSession,
    project: ProjectContext,
    tmp_path: Path,
    *,
    key: str = "prep-0001",
) -> ExecutionPreparation:
    version = await approved_version(session, project, tmp_path)
    return await prepare(
        session,
        version_id=version.id,
        actor_id=project.engineer_id,
        idempotency_key=key,
        settings=settings(),
    )


async def approval_ready(
    session: AsyncSession, project: ProjectContext, tmp_path: Path
) -> tuple[ExecutionPreparation, dict[HarnessRole, User]]:
    """准备单 + 一个持有 DATA_APPROVER 槽的审批人。"""
    preparation = await prepared(session, project, tmp_path, key=f"prep-{uuid.uuid4().hex[:8]}")
    approver = await make_user(
        session, project, role=ProjectRole.APPROVER, harness_roles=(HarnessRole.DATA_APPROVER,)
    )
    return preparation, {HarnessRole.DATA_APPROVER: approver}


# ------------------------------------------------------------------ 正常路径
async def test_approval_fills_the_slot_and_readies_the_preparation(
    session: AsyncSession, project: ProjectContext, tmp_path: Path
) -> None:
    preparation, approvers = await approval_ready(session, project, tmp_path)

    decided = await decide(
        session,
        preparation_id=preparation.id,
        actor_id=approvers[HarnessRole.DATA_APPROVER].id,
        role=HarnessRole.DATA_APPROVER,
        decision=HarnessApprovalValue.APPROVE,
        reason="字段映射与质量规则核对无误",
        settings=settings(),
    )

    assert decided.status is ExecutionPreparationStatus.READY


async def test_approval_records_a_named_decision(
    session: AsyncSession, project: ProjectContext, tmp_path: Path
) -> None:
    """具名：谁、以什么槽位、在什么时候、因为什么批的，都要留下来。"""
    preparation, approvers = await approval_ready(session, project, tmp_path)
    approver = approvers[HarnessRole.DATA_APPROVER]

    await decide(
        session,
        preparation_id=preparation.id,
        actor_id=approver.id,
        role=HarnessRole.DATA_APPROVER,
        decision=HarnessApprovalValue.APPROVE,
        reason="金额字段已做非空校验",
        settings=settings(),
    )

    record = await session.scalar(
        select(HarnessApprovalDecision).where(
            HarnessApprovalDecision.preparation_id == preparation.id
        )
    )
    assert record is not None
    assert record.approver_id == approver.id
    assert record.role is HarnessRole.DATA_APPROVER
    assert record.reason == "金额字段已做非空校验"


async def test_rejection_terminates_the_preparation(
    session: AsyncSession, project: ProjectContext, tmp_path: Path
) -> None:
    preparation, approvers = await approval_ready(session, project, tmp_path)

    decided = await decide(
        session,
        preparation_id=preparation.id,
        actor_id=approvers[HarnessRole.DATA_APPROVER].id,
        role=HarnessRole.DATA_APPROVER,
        decision=HarnessApprovalValue.REJECT,
        reason="脱敏字段没覆盖到邮箱",
        settings=settings(),
    )

    assert decided.status is ExecutionPreparationStatus.REJECTED


# ------------------------------------------------------------------ 内容变化使旧审批失效
async def test_expired_preparation_is_voided(
    session: AsyncSession, project: ProjectContext, tmp_path: Path
) -> None:
    """FR-52：24 小时之后审批不再有效，且准备单会被作废而不是留着半死不活。"""
    preparation, approvers = await approval_ready(session, project, tmp_path)

    with pytest.raises(DomainError, match="已过期"):
        await decide(
            session,
            preparation_id=preparation.id,
            actor_id=approvers[HarnessRole.DATA_APPROVER].id,
            role=HarnessRole.DATA_APPROVER,
            decision=HarnessApprovalValue.APPROVE,
            reason="来晚了",
            settings=settings(),
            now=datetime.now(UTC) + timedelta(hours=25),
        )

    assert preparation.status is ExecutionPreparationStatus.EXPIRED


async def test_changed_frozen_facts_void_existing_approvals(
    session: AsyncSession, project: ProjectContext, tmp_path: Path
) -> None:
    """**内容变化使旧审批失效**：平台预算改了，准备工作已经是按另一套上限算的。

    这时已有的审批批的是旧事实，不该继续有效；准备单作废，要求重新 Prepare。
    """
    preparation, approvers = await approval_ready(session, project, tmp_path)

    with pytest.raises(VersionConflict, match="已有审批一并失效"):
        await decide(
            session,
            preparation_id=preparation.id,
            actor_id=approvers[HarnessRole.DATA_APPROVER].id,
            role=HarnessRole.DATA_APPROVER,
            decision=HarnessApprovalValue.APPROVE,
            reason="预算变了之后才来批",
            settings=settings(harness_runtime_max_records=123),
        )

    assert preparation.status is ExecutionPreparationStatus.EXPIRED


async def test_first_approval_survives_a_second_approver_with_the_same_settings(
    session: AsyncSession, project: ProjectContext, tmp_path: Path
) -> None:
    """事实没变时，第二个审批人仍能正常补上另一个槽。"""
    version = await approved_version(session, project, tmp_path)
    preparation = await prepare(
        session,
        version_id=version.id,
        actor_id=project.engineer_id,
        idempotency_key=f"prep-{uuid.uuid4().hex[:8]}",
        settings=settings(),
    )
    # 换成需要两个槽的破坏性场景：直接改required_roles 来模拟双人审批。
    preparation.required_roles = [
        HarnessRole.DATA_APPROVER.value,
        HarnessRole.SECURITY_APPROVER.value,
    ]
    preparation.required_approvals = 2
    await session.flush()
    data = await make_user(
        session, project, role=ProjectRole.APPROVER, harness_roles=(HarnessRole.DATA_APPROVER,)
    )
    security = await make_user(
        session, project, role=ProjectRole.APPROVER, harness_roles=(HarnessRole.SECURITY_APPROVER,)
    )

    await decide(
        session,
        preparation_id=preparation.id,
        actor_id=data.id,
        role=HarnessRole.DATA_APPROVER,
        decision=HarnessApprovalValue.APPROVE,
        reason="数据维度通过",
        settings=settings(),
    )
    assert preparation.status is ExecutionPreparationStatus.WAITING_APPROVAL

    await decide(
        session,
        preparation_id=preparation.id,
        actor_id=security.id,
        role=HarnessRole.SECURITY_APPROVER,
        decision=HarnessApprovalValue.APPROVE,
        reason="安全维度通过",
        settings=settings(),
    )

    assert preparation.status is ExecutionPreparationStatus.READY


# ------------------------------------------------------------------ 职责分离
async def test_applicant_cannot_approve(
    session: AsyncSession, project: ProjectContext, tmp_path: Path
) -> None:
    """SOD-01：准备单的申请人不得审批自己的准备单。"""
    preparation = await prepared(session, project, tmp_path, key="prep-sod01")
    membership = await session.scalar(
        select(ProjectMembership).where(
            ProjectMembership.project_id == project.project_id,
            ProjectMembership.user_id == project.engineer_id,
        )
    )
    assert membership is not None
    membership.role = ProjectRole.APPROVER
    session.add(
        ProjectRoleGrant(
            project_id=project.project_id,
            user_id=project.engineer_id,
            role=HarnessRole.DATA_APPROVER,
        )
    )
    await session.flush()

    with pytest.raises(PermissionDenied, match="SOD-01"):
        await decide(
            session,
            preparation_id=preparation.id,
            actor_id=project.engineer_id,
            role=HarnessRole.DATA_APPROVER,
            decision=HarnessApprovalValue.APPROVE,
            reason="我自己批",
            settings=settings(),
        )


async def test_one_person_cannot_hold_two_slots(
    session: AsyncSession, project: ProjectContext, tmp_path: Path
) -> None:
    """SOD-02：同一张准备单上，一个人不能占两个槽 —— 那样双人审批就成了摆设。"""
    preparation, _ = await approval_ready(session, project, tmp_path)
    preparation.required_roles = [
        HarnessRole.DATA_APPROVER.value,
        HarnessRole.SECURITY_APPROVER.value,
    ]
    preparation.required_approvals = 2
    await session.flush()
    both = await make_user(
        session,
        project,
        role=ProjectRole.APPROVER,
        harness_roles=(HarnessRole.DATA_APPROVER, HarnessRole.SECURITY_APPROVER),
    )

    await decide(
        session,
        preparation_id=preparation.id,
        actor_id=both.id,
        role=HarnessRole.DATA_APPROVER,
        decision=HarnessApprovalValue.APPROVE,
        reason="数据维度",
        settings=settings(),
    )
    with pytest.raises(PermissionDenied, match="SOD-02"):
        await decide(
            session,
            preparation_id=preparation.id,
            actor_id=both.id,
            role=HarnessRole.SECURITY_APPROVER,
            decision=HarnessApprovalValue.APPROVE,
            reason="安全维度（还是我）",
            settings=settings(),
        )


async def test_a_slot_can_only_be_decided_once(
    session: AsyncSession, project: ProjectContext, tmp_path: Path
) -> None:
    preparation, approvers = await approval_ready(session, project, tmp_path)
    # 要求两个槽，这样第一次批准不会立刻把准备单推到 READY ——
    # 否则第二次撞上的是「状态已变」，验不到槽位唯一性这一条。
    preparation.required_roles = [
        HarnessRole.DATA_APPROVER.value,
        HarnessRole.SECURITY_APPROVER.value,
    ]
    preparation.required_approvals = 2
    await session.flush()
    await decide(
        session,
        preparation_id=preparation.id,
        actor_id=approvers[HarnessRole.DATA_APPROVER].id,
        role=HarnessRole.DATA_APPROVER,
        decision=HarnessApprovalValue.APPROVE,
        reason="通过",
        settings=settings(),
    )
    other = await make_user(
        session, project, role=ProjectRole.APPROVER, harness_roles=(HarnessRole.DATA_APPROVER,)
    )

    with pytest.raises(VersionConflict, match="已经有决定了"):
        await decide(
            session,
            preparation_id=preparation.id,
            actor_id=other.id,
            role=HarnessRole.DATA_APPROVER,
            decision=HarnessApprovalValue.APPROVE,
            reason="我也批一遍",
            settings=settings(),
        )


async def test_approver_must_actually_hold_the_slot(
    session: AsyncSession, project: ProjectContext, tmp_path: Path
) -> None:
    """「有审批角色的成员」与「持有该职责槽的主体」是两件事，不能互相顶替。"""
    preparation = await prepared(session, project, tmp_path, key="prep-slot")
    member_only = await make_user(session, project, role=ProjectRole.APPROVER)

    with pytest.raises(PermissionDenied):
        await decide(
            session,
            preparation_id=preparation.id,
            actor_id=member_only.id,
            role=HarnessRole.DATA_APPROVER,
            decision=HarnessApprovalValue.APPROVE,
            reason="我有审批角色",
            settings=settings(),
        )


async def test_approving_a_slot_that_is_not_required_is_refused(
    session: AsyncSession, project: ProjectContext, tmp_path: Path
) -> None:
    preparation = await prepared(session, project, tmp_path, key="prep-notreq")
    security = await make_user(
        session, project, role=ProjectRole.APPROVER, harness_roles=(HarnessRole.SECURITY_APPROVER,)
    )

    with pytest.raises(DomainError, match="没有 SECURITY_APPROVER 这个审批槽"):
        await decide(
            session,
            preparation_id=preparation.id,
            actor_id=security.id,
            role=HarnessRole.SECURITY_APPROVER,
            decision=HarnessApprovalValue.APPROVE,
            reason="我批了不该我批的槽",
            settings=settings(),
        )


async def test_unknown_preparation_is_a_404(session: AsyncSession, project: ProjectContext) -> None:
    with pytest.raises(ResourceNotFound):
        await decide(
            session,
            preparation_id=uuid.uuid4(),
            actor_id=project.engineer_id,
            role=HarnessRole.DATA_APPROVER,
            decision=HarnessApprovalValue.APPROVE,
            reason="不存在",
            settings=settings(),
        )
