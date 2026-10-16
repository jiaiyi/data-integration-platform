"""Commit：服务端重建事实并签发单次能力（T-35 / FR-55）。

证据与不变量：

- **客户端伪造风险等级无效**：Commit 的入参里根本没有风险等级，等级由服务端
  重新判定；准备单上的等级与当下判定不一致就拒绝提交（审批批的是旧等级）。
- **一条事务里做完四件事**：消费能力 + 建 Run + 写 Outbox + 准备单转 COMMITTED。
  事务回滚时四者一起消失，因此不可能出现「远端有作业、本地没有运行记录」。
- **Commit 本身零外部副作用**：Broker 在这里的「执行器」只是把命令排进待处理队列，
  测试断言它的返回值里没有远端引用，且 `execution_runs`/`harness_command_outbox`
  之外没有任何变化。
"""

import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from data_integration_platform.application.harness.approval import decide
from data_integration_platform.application.harness.commit import commit
from data_integration_platform.application.harness.preparation import prepare
from data_integration_platform.application.versions import PipelineVersionService
from data_integration_platform.config import Settings
from data_integration_platform.domain.enums import (
    ApprovalDecisionValue,
    ExecutionStatus,
    ExecutionTrigger,
    PipelineVersionStatus,
    ProjectRole,
)
from data_integration_platform.domain.errors import (
    DomainError,
    PermissionDenied,
    VersionConflict,
)
from data_integration_platform.domain.harness import (
    ExecutionPreparationStatus,
    HarnessApprovalValue,
    HarnessRole,
    RiskLevel,
)
from data_integration_platform.infrastructure.ed25519_signing import (
    Ed25519KeyRingVerifier,
    Ed25519Signer,
    generate_keypair,
    parse_public_keys,
)
from data_integration_platform.infrastructure.models import (
    ExecutionPreparation,
    ExecutionRun,
    HarnessCapabilityGrant,
    HarnessCommandOutbox,
    HarnessEvidenceEvent,
    ProjectMembership,
    ProjectRoleGrant,
    User,
)
from tests.conftest import ProjectContext
from tests.test_pipeline_versions import candidate, ensure_execution_endpoints

KEY_ID = "harness-local-1"


def settings(**overrides: object) -> Settings:
    values: dict[str, object] = {"_env_file": None, "environment": "local"}
    values.update(overrides)
    return Settings(**values)  # type: ignore[arg-type]


@pytest.fixture(scope="module")
def keys() -> tuple[Ed25519Signer, Ed25519KeyRingVerifier]:
    private, public = generate_keypair()
    return (
        Ed25519Signer(key_id=KEY_ID, private_key=private),
        Ed25519KeyRingVerifier(parse_public_keys(f"{KEY_ID}:{public}")),
    )


async def make_approver(session: AsyncSession, project: ProjectContext) -> User:
    approver = User(subject=f"approver-{uuid.uuid4().hex[:8]}", display_name="审批人")
    session.add(approver)
    await session.flush()
    session.add(
        ProjectMembership(
            project_id=project.project_id, user_id=approver.id, role=ProjectRole.APPROVER
        )
    )
    session.add(
        ProjectRoleGrant(
            project_id=project.project_id,
            user_id=approver.id,
            role=HarnessRole.DATA_APPROVER,
        )
    )
    await session.flush()
    return approver


async def ready_preparation(
    session: AsyncSession, project: ProjectContext, tmp_path: Path
) -> ExecutionPreparation:
    """一路推到 READY：创建版本 → 冻结 → 审批版本 → Prepare → 审批准备单。"""
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
    version_approver = await make_approver(session, project)
    await versions.decide_approval(
        session,
        approval_id=request.id,
        actor_id=version_approver.id,
        decision=ApprovalDecisionValue.APPROVE,
        reason="核对无误",
    )
    preparation = await prepare(
        session,
        version_id=version.id,
        actor_id=project.engineer_id,
        idempotency_key=f"prep-{uuid.uuid4().hex[:8]}",
        settings=settings(),
    )
    await decide(
        session,
        preparation_id=preparation.id,
        actor_id=version_approver.id,
        role=HarnessRole.DATA_APPROVER,
        decision=HarnessApprovalValue.APPROVE,
        reason="数据维度通过",
        settings=settings(),
    )
    return preparation


# ------------------------------------------------------------------ 正向路径
async def test_commit_creates_run_outbox_and_consumes_the_capability(
    session: AsyncSession,
    project: ProjectContext,
    tmp_path: Path,
    keys: tuple[Ed25519Signer, Ed25519KeyRingVerifier],
) -> None:
    preparation = await ready_preparation(session, project, tmp_path)

    run = await commit(
        session,
        preparation_id=preparation.id,
        actor_id=project.engineer_id,
        settings=settings(),
        signer=keys[0],
        verifier=keys[1],
    )

    assert run.status is ExecutionStatus.QUEUED
    assert run.trigger is ExecutionTrigger.MANUAL
    assert preparation.status is ExecutionPreparationStatus.COMMITTED
    assert preparation.committed_by == project.engineer_id


async def test_capability_is_consumed_at_commit_so_it_cannot_be_replayed(
    session: AsyncSession,
    project: ProjectContext,
    tmp_path: Path,
    keys: tuple[Ed25519Signer, Ed25519KeyRingVerifier],
) -> None:
    preparation = await ready_preparation(session, project, tmp_path)

    run = await commit(
        session,
        preparation_id=preparation.id,
        actor_id=project.engineer_id,
        settings=settings(),
        signer=keys[0],
        verifier=keys[1],
    )

    grant = await session.scalar(
        select(HarnessCapabilityGrant).where(
            HarnessCapabilityGrant.preparation_id == preparation.id
        )
    )
    assert grant is not None
    assert grant.consumed_at is not None
    assert grant.execution_run_id == run.id
    assert grant.claims["artifact_sha256"] == preparation.artifact_sha256


async def test_outbox_command_carries_no_credential(
    session: AsyncSession,
    project: ProjectContext,
    tmp_path: Path,
    keys: tuple[Ed25519Signer, Ed25519KeyRingVerifier],
) -> None:
    """能力令牌**不进队列**：Worker 按 run_id 从库里取声明体与签名来验。"""
    preparation = await ready_preparation(session, project, tmp_path)

    run = await commit(
        session,
        preparation_id=preparation.id,
        actor_id=project.engineer_id,
        settings=settings(),
        signer=keys[0],
        verifier=keys[1],
    )

    row = await session.scalar(
        select(HarnessCommandOutbox).where(HarnessCommandOutbox.preparation_id == preparation.id)
    )
    assert row is not None
    assert row.command_payload["run_id"] == str(run.id)
    assert row.command_payload["artifact_sha256"] == preparation.artifact_sha256
    assert "signature" not in row.command_payload
    assert "claims" not in row.command_payload


async def test_commit_writes_an_evidence_event(
    session: AsyncSession,
    project: ProjectContext,
    tmp_path: Path,
    keys: tuple[Ed25519Signer, Ed25519KeyRingVerifier],
) -> None:
    """账本要真的被用起来：提交是一条带签名的事件，事后可校验完整性。"""
    preparation = await ready_preparation(session, project, tmp_path)

    run = await commit(
        session,
        preparation_id=preparation.id,
        actor_id=project.engineer_id,
        settings=settings(),
        signer=keys[0],
        verifier=keys[1],
    )

    events = (
        await session.scalars(select(HarnessEvidenceEvent).order_by(HarnessEvidenceEvent.sequence))
    ).all()
    assert len(events) == 1
    assert events[0].event_type == "EXECUTION_COMMITTED"
    assert events[0].payload["run_id"] == str(run.id)
    assert events[0].signature_key_id == KEY_ID


async def test_run_idempotency_key_is_derived_from_the_preparation(
    session: AsyncSession,
    project: ProjectContext,
    tmp_path: Path,
    keys: tuple[Ed25519Signer, Ed25519KeyRingVerifier],
) -> None:
    preparation = await ready_preparation(session, project, tmp_path)

    run = await commit(
        session,
        preparation_id=preparation.id,
        actor_id=project.engineer_id,
        settings=settings(),
        signer=keys[0],
        verifier=keys[1],
    )

    assert run.idempotency_key == f"{preparation.idempotency_key}:commit"


# ------------------------------------------------------------------ 拒绝路径
async def test_commit_requires_a_ready_preparation(
    session: AsyncSession,
    project: ProjectContext,
    tmp_path: Path,
    keys: tuple[Ed25519Signer, Ed25519KeyRingVerifier],
) -> None:
    """审批没齐就提交：拒绝，而且一条运行记录都不该产生。"""
    await ensure_execution_endpoints(session, project)
    versions = PipelineVersionService(root=tmp_path / "artifacts")
    version = await versions.create(
        session,
        project_id=project.project_id,
        actor_id=project.engineer_id,
        name=f"notready-{uuid.uuid4().hex[:6]}",
        requirement="还没审批",
    )
    version.status = PipelineVersionStatus.GENERATED
    await session.flush()
    await versions.freeze(
        session, version_id=version.id, actor_id=project.engineer_id, candidate=candidate()
    )
    request = await versions.submit_for_approval(
        session, version_id=version.id, actor_id=project.engineer_id
    )
    approver = await make_approver(session, project)
    await versions.decide_approval(
        session,
        approval_id=request.id,
        actor_id=approver.id,
        decision=ApprovalDecisionValue.APPROVE,
        reason="通过",
    )
    preparation = await prepare(
        session,
        version_id=version.id,
        actor_id=project.engineer_id,
        idempotency_key=f"prep-{uuid.uuid4().hex[:8]}",
        settings=settings(),
    )

    with pytest.raises(DomainError, match="只有 READY 才能提交"):
        await commit(
            session,
            preparation_id=preparation.id,
            actor_id=project.engineer_id,
            settings=settings(),
            signer=keys[0],
            verifier=keys[1],
        )

    runs = (await session.scalars(select(ExecutionRun))).all()
    assert list(runs) == []


async def test_commit_refuses_when_the_risk_level_changed(
    session: AsyncSession,
    project: ProjectContext,
    tmp_path: Path,
    keys: tuple[Ed25519Signer, Ed25519KeyRingVerifier],
) -> None:
    """**客户端伪造风险等级无效**：等级由服务端重新判定，对不上就拒绝提交。

    这里模拟的是「准备单上的等级与当下判定不一致」—— 审批批的是旧等级，
    不该拿来放行按新等级才能做的事。
    """
    preparation = await ready_preparation(session, project, tmp_path)

    preparation.risk_level = RiskLevel.P3
    await session.flush()

    with pytest.raises(DomainError, match="风险等级与准备单不一致"):
        await commit(
            session,
            preparation_id=preparation.id,
            actor_id=project.engineer_id,
            settings=settings(),
            signer=keys[0],
            verifier=keys[1],
        )


async def test_commit_refuses_an_expired_preparation(
    session: AsyncSession,
    project: ProjectContext,
    tmp_path: Path,
    keys: tuple[Ed25519Signer, Ed25519KeyRingVerifier],
) -> None:
    preparation = await ready_preparation(session, project, tmp_path)

    with pytest.raises(DomainError, match="已过期"):
        await commit(
            session,
            preparation_id=preparation.id,
            actor_id=project.engineer_id,
            settings=settings(),
            signer=keys[0],
            verifier=keys[1],
            now=datetime.now(UTC) + timedelta(hours=25),
        )

    assert preparation.status is ExecutionPreparationStatus.EXPIRED


async def test_commit_refuses_drifted_frozen_facts(
    session: AsyncSession,
    project: ProjectContext,
    tmp_path: Path,
    keys: tuple[Ed25519Signer, Ed25519KeyRingVerifier],
) -> None:
    preparation = await ready_preparation(session, project, tmp_path)

    with pytest.raises(VersionConflict, match="已有审批一并失效"):
        await commit(
            session,
            preparation_id=preparation.id,
            actor_id=project.engineer_id,
            settings=settings(harness_runtime_max_records=77),
            signer=keys[0],
            verifier=keys[1],
        )


async def test_rollback_leaves_no_run_and_no_command(
    session: AsyncSession,
    project: ProjectContext,
    tmp_path: Path,
    keys: tuple[Ed25519Signer, Ed25519KeyRingVerifier],
) -> None:
    """一条事务：回滚时运行记录与待投递命令一起消失，不会留下「有命令没事实」的半截状态。"""
    preparation = await ready_preparation(session, project, tmp_path)
    preparation_id = preparation.id

    await session.commit()
    await commit(
        session,
        preparation_id=preparation_id,
        actor_id=project.engineer_id,
        settings=settings(),
        signer=keys[0],
        verifier=keys[1],
    )
    await session.rollback()

    assert list((await session.scalars(select(ExecutionRun))).all()) == []
    assert list((await session.scalars(select(HarnessCommandOutbox))).all()) == []


async def test_only_a_writable_role_can_commit(
    session: AsyncSession,
    project: ProjectContext,
    tmp_path: Path,
    keys: tuple[Ed25519Signer, Ed25519KeyRingVerifier],
) -> None:
    preparation = await ready_preparation(session, project, tmp_path)
    viewer = User(subject=f"viewer-{uuid.uuid4().hex[:8]}", display_name="只读")
    session.add(viewer)
    await session.flush()
    session.add(
        ProjectMembership(project_id=project.project_id, user_id=viewer.id, role=ProjectRole.VIEWER)
    )
    await session.flush()

    with pytest.raises(PermissionDenied):
        await commit(
            session,
            preparation_id=preparation.id,
            actor_id=viewer.id,
            settings=settings(),
            signer=keys[0],
            verifier=keys[1],
        )


async def test_committing_twice_is_refused(
    session: AsyncSession,
    project: ProjectContext,
    tmp_path: Path,
    keys: tuple[Ed25519Signer, Ed25519KeyRingVerifier],
) -> None:
    preparation = await ready_preparation(session, project, tmp_path)
    await commit(
        session,
        preparation_id=preparation.id,
        actor_id=project.engineer_id,
        settings=settings(),
        signer=keys[0],
        verifier=keys[1],
    )

    with pytest.raises(DomainError, match="不能再使用"):
        await commit(
            session,
            preparation_id=preparation.id,
            actor_id=project.engineer_id,
            settings=settings(),
            signer=keys[0],
            verifier=keys[1],
        )
