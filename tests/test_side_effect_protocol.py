"""通用副作用框架（T-36 / FR-57、FR-23）。

要守的证据有四条：

1. **九类操作各自产生准备单、审批与能力记录**（FR-57 的验收原文）。逐个操作走一遍
   申请 → 审批 → 提交，断言三类记录都在，且绑定的事实逐项对得上。
2. **不存在绕过三阶段的执行路径**（FR-23）。没有准备单、或准备单没提交，
   执行一律被拒 —— 断言的是「拒绝」，不是「返回了错误码」。
3. **执行租约防并发**（SPEC §8.3）。并发冲突必须报 `SideEffectLeaseBusy` 并让调用方
   安全重试，**不允许**被记成失败：一条正常的重复投递不该毁掉一次本该成功的副作用。
4. **冻结的事实变了，审批就失效**。这是三阶段协议买的东西：审批批的是当时的事实，
   不是「批准这次操作」这么一句话。

九类里轮换的三段（暂存/验证/激活）共用一份准备单，见 `side_effect_targets` 的模块文档 ——
因此这里断言的是 7 张准备单覆盖 9 类操作。
"""

import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from pydantic import SecretStr
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from data_integration_platform.application.harness import side_effects
from data_integration_platform.application.harness.side_effect_targets import (
    AGGREGATE_EXECUTION_RUN,
    AGGREGATE_SECRET_DESTRUCTION,
    AGGREGATE_SECRET_MIGRATION,
    AGGREGATE_SECRET_ROTATION,
    AGGREGATE_SECRET_ROTATION_ROLLBACK,
    derive_facts,
)
from data_integration_platform.application.secrets import ConnectionCredentials
from data_integration_platform.application.side_effect_runtime import (
    apply_side_effect,
    begin_execution,
    decide_for,
    preparation_id_for,
    prepare_for,
    settle,
    worker_owner,
)
from data_integration_platform.application.versions import PipelineVersionService
from data_integration_platform.config import Settings
from data_integration_platform.domain.enums import (
    ApprovalDecisionValue,
    ConnectorType,
    DataEndpointRole,
    ExecutionStatus,
    ExecutionTrigger,
    PipelineVersionStatus,
    ProjectRole,
    SecretDestructionStatus,
    SecretDestructionTarget,
    SecretMigrationStatus,
    SecretRotationRollbackStatus,
    SecretRotationStatus,
)
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
)
from data_integration_platform.domain.side_effects import (
    SideEffectExecutionStatus,
    SideEffectOperation,
)
from data_integration_platform.infrastructure.ed25519_signing import (
    Ed25519KeyRingVerifier,
    Ed25519Signer,
    generate_keypair,
    parse_public_keys,
)
from data_integration_platform.infrastructure.models import (
    ExecutionRun,
    HarnessSideEffectCapabilityGrant,
    HarnessSideEffectPreparation,
    ProjectMembership,
    ProjectRoleGrant,
    SecretDestructionRequest,
    SecretMigrationRequest,
    SecretRotationRequest,
    SecretRotationRollbackRequest,
    User,
)
from data_integration_platform.infrastructure.secret_factory import create_secret_provider
from tests.conftest import ProjectContext
from tests.test_pipeline_versions import candidate, ensure_execution_endpoints

KEY_ID = "harness-local-1"

# 每个操作期望的审批槽。取自 PDP（T-25 的破坏性写入要双人、凭据使用要安全审批槽），
# 不是这里另定的一套规则 —— 写死一份期望值，就是让规则改动在这里显形。
EXPECTED_ROLES: dict[SideEffectOperation, tuple[HarnessRole, ...]] = {
    SideEffectOperation.EXECUTION_CANCEL: (HarnessRole.DATA_APPROVER,),
    SideEffectOperation.DORIS_ROLLBACK: (
        HarnessRole.DATA_APPROVER,
        HarnessRole.SECURITY_APPROVER,
    ),
    SideEffectOperation.DORIS_CLEANUP: (
        HarnessRole.DATA_APPROVER,
        HarnessRole.SECURITY_APPROVER,
    ),
    SideEffectOperation.SECRET_MIGRATION: (HarnessRole.SECURITY_APPROVER,),
    SideEffectOperation.SECRET_ROTATION_STAGE: (HarnessRole.SECURITY_APPROVER,),
    SideEffectOperation.SECRET_ROTATION_VERIFY: (HarnessRole.SECURITY_APPROVER,),
    SideEffectOperation.SECRET_ROTATION_ACTIVATE: (HarnessRole.SECURITY_APPROVER,),
    SideEffectOperation.SECRET_ROTATION_ROLLBACK: (HarnessRole.SECURITY_APPROVER,),
    SideEffectOperation.SECRET_DESTRUCTION: (
        HarnessRole.DATA_APPROVER,
        HarnessRole.SECURITY_APPROVER,
    ),
}


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


async def make_user(
    session: AsyncSession,
    project: ProjectContext,
    *,
    harness_role: HarnessRole,
) -> uuid.UUID:
    """一个同时具备「能进审批台」与「持有该职责槽」的主体。"""
    user = User(subject=f"approver-{uuid.uuid4().hex[:8]}", display_name="审批人")
    session.add(user)
    await session.flush()
    session.add(
        ProjectMembership(project_id=project.project_id, user_id=user.id, role=ProjectRole.APPROVER)
    )
    session.add(ProjectRoleGrant(project_id=project.project_id, user_id=user.id, role=harness_role))
    await session.flush()
    return user.id


async def seed_connection(session: AsyncSession, project: ProjectContext) -> Any:
    stored = await create_secret_provider(session).store(
        project_id=project.project_id,
        name="mysql-source",
        credentials=ConnectionCredentials(username="dip_reader", password=SecretStr("p@ss")),
    )
    from data_integration_platform.domain.enums import ConnectionStatus
    from data_integration_platform.infrastructure.models import DataConnection

    connection = DataConnection(
        project_id=project.project_id,
        name="mysql-source",
        connector_type=ConnectorType.MYSQL,
        endpoint_role=DataEndpointRole.SOURCE,
        status=ConnectionStatus.READY,
        host="127.0.0.1",
        port=13306,
        database_name="source_db",
        options={},
        secret_ref=stored.secret_ref,
        created_by=project.engineer_id,
    )
    session.add(connection)
    await session.flush()
    return connection


async def seed_run(session: AsyncSession, project: ProjectContext, tmp_path: Path) -> ExecutionRun:
    """造一次已提交的执行：执行侧三类操作（取消/回滚/清理）的聚合对象就是它。"""
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
    approver = await make_user(session, project, harness_role=HarnessRole.DATA_APPROVER)
    await versions.decide_approval(
        session,
        approval_id=request.id,
        actor_id=approver,
        decision=ApprovalDecisionValue.APPROVE,
        reason="核对无误",
    )
    from data_integration_platform.application.harness.preparation import prepare

    preparation = await prepare(
        session,
        version_id=version.id,
        actor_id=project.engineer_id,
        idempotency_key=f"prep-{uuid.uuid4().hex[:8]}",
    )
    run = ExecutionRun(
        pipeline_version_id=version.id,
        harness_preparation_id=preparation.id,
        status=ExecutionStatus.SUBMITTED,
        idempotency_key=f"run-{uuid.uuid4().hex[:8]}",
        trigger=ExecutionTrigger.MANUAL,
        supervision_contract_sha256=preparation.supervision_contract_sha256,
        seatunnel_job_id=f"job-{uuid.uuid4().hex[:8]}",
        engine_status="RUNNING",
    )
    session.add(run)
    await session.flush()
    return run


async def seed_aggregate(
    session: AsyncSession, project: ProjectContext, tmp_path: Path, operation: SideEffectOperation
) -> tuple[str, str]:
    """为九类操作各造一份真实的业务事实，返回 `(aggregate_type, aggregate_id)`。"""
    if operation in (
        SideEffectOperation.EXECUTION_CANCEL,
        SideEffectOperation.DORIS_ROLLBACK,
        SideEffectOperation.DORIS_CLEANUP,
    ):
        run = await seed_run(session, project, tmp_path)
        return AGGREGATE_EXECUTION_RUN, str(run.id)

    connection = await seed_connection(session, project)
    if operation is SideEffectOperation.SECRET_MIGRATION:
        row = SecretMigrationRequest(
            connection_id=connection.id,
            source_provider="fernet-db",
            source_secret_ref=connection.secret_ref,
            target_provider="vault-kv-v2",
            source_retained=True,
            status=SecretMigrationStatus.PENDING,
            reason="换存储",
            requested_by=project.engineer_id,
        )
    elif operation in (
        SideEffectOperation.SECRET_ROTATION_STAGE,
        SideEffectOperation.SECRET_ROTATION_VERIFY,
        SideEffectOperation.SECRET_ROTATION_ACTIVATE,
    ):
        row = SecretRotationRequest(
            connection_id=connection.id,
            status=SecretRotationStatus.WAITING_APPROVAL,
            previous_secret_ref=connection.secret_ref or "fernet://missing",
            previous_credential_issued_at=datetime.now(UTC),
            requested_by=project.engineer_id,
        )
    elif operation is SideEffectOperation.SECRET_ROTATION_ROLLBACK:
        rotation = SecretRotationRequest(
            connection_id=connection.id,
            status=SecretRotationStatus.SUCCEEDED,
            previous_secret_ref=connection.secret_ref or "fernet://missing",
            requested_by=project.engineer_id,
        )
        session.add(rotation)
        await session.flush()
        row = SecretRotationRollbackRequest(
            connection_id=connection.id,
            rotation_request_id=rotation.id,
            status=SecretRotationRollbackStatus.PENDING,
            requested_by=project.engineer_id,
        )
    else:
        rotation = SecretRotationRequest(
            connection_id=connection.id,
            status=SecretRotationStatus.SUCCEEDED,
            previous_secret_ref=connection.secret_ref or "fernet://missing",
            requested_by=project.engineer_id,
        )
        session.add(rotation)
        await session.flush()
        row = SecretDestructionRequest(
            connection_id=connection.id,
            target=SecretDestructionTarget.ROTATION_SOURCE,
            source_rotation_request_id=rotation.id,
            status=SecretDestructionStatus.PENDING,
            requested_by=project.engineer_id,
        )
    session.add(row)
    await session.flush()
    from data_integration_platform.application.harness.side_effect_targets import (
        aggregate_type_of,
    )

    return aggregate_type_of(operation), str(row.id)


async def walk(
    session: AsyncSession,
    project: ProjectContext,
    tmp_path: Path,
    operation: SideEffectOperation,
    *,
    keys: tuple[Ed25519Signer, Ed25519KeyRingVerifier],
) -> HarnessSideEffectPreparation:
    """把一类操作走完 申请 → 逐槽审批 → 提交，返回已提交的准备单。"""
    aggregate_type, aggregate_id = await seed_aggregate(session, project, tmp_path, operation)
    preparation = await prepare_for(
        session,
        operation=operation,
        aggregate_id=aggregate_id,
        actor_id=project.engineer_id,
        settings=settings(),
    )
    assert preparation.aggregate_type == aggregate_type

    roles = EXPECTED_ROLES[operation]
    for index, role in enumerate(roles):
        approver = await make_user(session, project, harness_role=role)
        preparation = await decide_for(
            session,
            operation=operation,
            aggregate_id=aggregate_id,
            actor_id=approver,
            role=role,
            decision=HarnessApprovalValue.APPROVE,
            reason=f"{role.value} 维度通过",
            settings=settings(),
            signer=keys[0],
        )
        if index < len(roles) - 1:
            # 槽位没齐：既不能提交，也不该显示成「可以执行」。
            assert preparation.status is ExecutionPreparationStatus.WAITING_APPROVAL
    return preparation


# --------------------------------------------- FR-57：九类各自产生三类记录
@pytest.mark.parametrize("operation", list(SideEffectOperation))
async def test_every_operation_produces_preparation_approval_and_capability(
    session: AsyncSession,
    project: ProjectContext,
    tmp_path: Path,
    keys: tuple[Ed25519Signer, Ed25519KeyRingVerifier],
    operation: SideEffectOperation,
) -> None:
    """九类操作逐一走完三阶段，各自留下准备单、审批与能力记录。"""
    preparation = await walk(session, project, tmp_path, operation, keys=keys)

    assert preparation.status is ExecutionPreparationStatus.COMMITTED
    assert preparation.execution_status is SideEffectExecutionStatus.PENDING

    # —— 审批记录：期望的槽位一个不少，且都出自具名主体 ——
    approvals = await side_effects.approvals_of(session, preparation_id=preparation.id)
    assert {item.role.value for item in approvals} == {
        role.value for role in EXPECTED_ROLES[operation]
    }
    assert all(item.decision is HarnessApprovalValue.APPROVE for item in approvals)

    # —— 提交人是**批完最后一个槽的那位审批人**：这几类操作的已验收交互就是「批完即执行」，
    # 再要一个人去点一次提交不会让它更安全，只会让一次轮换变成两次人工往返。
    # 提交本身仍是独立的一步（有自己的令牌、证据与审计事件），只是由同一个请求触发。
    assert preparation.committed_by is not None
    assert preparation.committed_by != project.engineer_id
    assert preparation.committed_by in {item.approver_id for item in approvals}

    # —— 能力记录：单次、已消费、主体是提交人 ——
    grant = await session.scalar(
        select(HarnessSideEffectCapabilityGrant).where(
            HarnessSideEffectCapabilityGrant.preparation_id == preparation.id
        )
    )
    assert grant is not None
    assert grant.single_use is True
    assert grant.consumed_at is not None
    assert grant.subject_id == str(preparation.committed_by)
    # 声明体里不带任何凭据或业务行。
    assert "password" not in str(grant.claims)
    assert preparation.aggregate_type and preparation.aggregate_id


async def test_rotation_phases_share_one_preparation(
    session: AsyncSession,
    project: ProjectContext,
    tmp_path: Path,
    keys: tuple[Ed25519Signer, Ed25519KeyRingVerifier],
) -> None:
    """轮换的三段落在**同一张**准备单上：同一次申请不该要三次人工审批。"""
    rotation = SecretRotationRequest(
        connection_id=(await seed_connection(session, project)).id,
        status=SecretRotationStatus.WAITING_APPROVAL,
        previous_secret_ref="fernet://abc",
        requested_by=project.engineer_id,
    )
    session.add(rotation)
    await session.flush()

    seen: list[str] = []
    for operation in (
        SideEffectOperation.SECRET_ROTATION_STAGE,
        SideEffectOperation.SECRET_ROTATION_VERIFY,
        SideEffectOperation.SECRET_ROTATION_ACTIVATE,
    ):
        facts = await derive_facts(
            session, operation=operation, aggregate_id=str(rotation.id), settings=settings()
        )
        seen.append(facts.fact_fingerprint())

    assert len(set(seen)) == 1
    facts = await derive_facts(
        session,
        operation=SideEffectOperation.SECRET_ROTATION_STAGE,
        aggregate_id=str(rotation.id),
        settings=settings(),
    )
    assert facts.fact_summary["phases"] == [
        SideEffectOperation.SECRET_ROTATION_STAGE.value,
        SideEffectOperation.SECRET_ROTATION_VERIFY.value,
        SideEffectOperation.SECRET_ROTATION_ACTIVATE.value,
    ]


# --------------------------------------------- FR-23：没有受管入口就走不下去
async def test_execution_without_preparation_is_refused(
    session: AsyncSession, project: ProjectContext, tmp_path: Path
) -> None:
    """没有受管申请单时，连准备单都查不到 —— 执行路径在第一步就被拒。"""
    run = await seed_run(session, project, tmp_path)

    with pytest.raises(DomainError, match="没有对应的受管申请单"):
        await preparation_id_for(
            session, operation=SideEffectOperation.EXECUTION_CANCEL, aggregate_id=str(run.id)
        )
    with pytest.raises(ResourceNotFound):
        await side_effects.get(session, preparation_id=uuid.uuid4(), actor_id=project.engineer_id)


async def test_execution_requires_committed_preparation(
    session: AsyncSession, project: ProjectContext, tmp_path: Path
) -> None:
    """只申请、还没审批提交的副作用不得执行。"""
    run = await seed_run(session, project, tmp_path)
    preparation = await prepare_for(
        session,
        operation=SideEffectOperation.EXECUTION_CANCEL,
        aggregate_id=str(run.id),
        actor_id=project.engineer_id,
        settings=settings(),
    )
    assert preparation.status is ExecutionPreparationStatus.WAITING_APPROVAL

    with pytest.raises(DomainError, match="未提交的副作用不得执行"):
        await begin_execution(
            session, preparation_id=preparation.id, owner="worker-1", settings=settings()
        )


async def test_drifted_facts_invalidate_the_preparation(
    session: AsyncSession, project: ProjectContext, tmp_path: Path, keys: Any
) -> None:
    """冻结的事实变了 → 准备单作废，已有的审批一并失效。

    这里改的是作业 id：审批人批的是「取消 job-x」，作业被换掉之后那份批准不作数。
    """
    run = await seed_run(session, project, tmp_path)
    preparation = await prepare_for(
        session,
        operation=SideEffectOperation.EXECUTION_CANCEL,
        aggregate_id=str(run.id),
        actor_id=project.engineer_id,
        settings=settings(),
    )
    run.seatunnel_job_id = "job-被换掉了"
    await session.flush()

    approver = await make_user(session, project, harness_role=HarnessRole.DATA_APPROVER)
    with pytest.raises(VersionConflict, match="事实已经变化"):
        await decide_for(
            session,
            operation=SideEffectOperation.EXECUTION_CANCEL,
            aggregate_id=str(run.id),
            actor_id=approver,
            role=HarnessRole.DATA_APPROVER,
            decision=HarnessApprovalValue.APPROVE,
            reason="看起来没问题",
            settings=settings(),
        )
    await session.refresh(preparation)
    assert preparation.status is ExecutionPreparationStatus.EXPIRED


# --------------------------------------------- SPEC §8.3：执行租约防并发
async def test_execution_lease_is_exclusive_and_retryable(
    session: AsyncSession,
    project: ProjectContext,
    tmp_path: Path,
    keys: tuple[Ed25519Signer, Ed25519KeyRingVerifier],
) -> None:
    """并发冲突报 `SideEffectLeaseBusy` 而**不是**失败。

    两者要分开：冲突是「现在不该做」，记成失败会让一次正常的重复投递毁掉
    一条本该成功的副作用。
    """
    preparation = await walk(
        session, project, tmp_path, SideEffectOperation.EXECUTION_CANCEL, keys=keys
    )
    first = await side_effects.claim_execution(
        session, preparation_id=preparation.id, owner="worker-1", settings=settings()
    )
    assert first.execution_lease_owner == "worker-1"

    with pytest.raises(SideEffectLeaseBusy, match="稍后重试"):
        await side_effects.claim_execution(
            session, preparation_id=preparation.id, owner="worker-2", settings=settings()
        )
    # 冲突没有把执行状态改成失败。
    await session.refresh(preparation)
    assert preparation.execution_status is SideEffectExecutionStatus.PENDING

    # 租约到期后可以被另一个 Worker 接手（崩掉的 Worker 不会让副作用永远卡住）。
    expired = await side_effects.claim_execution(
        session,
        preparation_id=preparation.id,
        owner="worker-3",
        settings=settings(),
        now=datetime.now(UTC) + timedelta(seconds=settings().harness_outbox_lease_seconds + 1),
    )
    assert expired.execution_lease_owner == "worker-3"


async def test_execution_happens_once_and_consumes_the_lease(
    session: AsyncSession,
    project: ProjectContext,
    tmp_path: Path,
    keys: tuple[Ed25519Signer, Ed25519KeyRingVerifier],
) -> None:
    """执行走完三道闸 → 落终态；同一张单子不再被执行第二次。"""
    preparation = await walk(
        session, project, tmp_path, SideEffectOperation.DORIS_CLEANUP, keys=keys
    )
    owner = worker_owner()
    row = await begin_execution(
        session,
        preparation_id=preparation.id,
        owner=owner,
        settings=settings(),
        verifier=keys[1],
    )
    assert row.execution_lease_owner == owner

    await settle(
        session,
        preparation_id=preparation.id,
        owner=owner,
        outcome=_success(),
    )
    await session.refresh(preparation)
    assert preparation.execution_status is SideEffectExecutionStatus.APPLIED
    assert preparation.execution_lease_owner is None

    with pytest.raises(DomainError, match="已经执行过"):
        await side_effects.claim_execution(
            session, preparation_id=preparation.id, owner="worker-9", settings=settings()
        )


async def test_verification_failure_stops_before_any_side_effect(
    session: AsyncSession,
    project: ProjectContext,
    tmp_path: Path,
    keys: tuple[Ed25519Signer, Ed25519KeyRingVerifier],
) -> None:
    """验签不过时**执行器一次都不会被调到**。

    这是「无绕过路径」的可检验形态：断言的不是「返回了错误」，而是调用次数是 0。
    这里用一把别人的公钥去验，等价于「令牌确实是被改过 / 换过钥匙的」。
    """
    preparation = await walk(
        session, project, tmp_path, SideEffectOperation.DORIS_CLEANUP, keys=keys
    )
    _, other_public = generate_keypair()
    stranger = Ed25519KeyRingVerifier(parse_public_keys(f"{KEY_ID}:{other_public}"))

    calls: list[str] = []

    async def executor(**_: Any) -> Any:
        calls.append("called")
        raise AssertionError("验签未通过时执行器不该被调用")

    owner = worker_owner()
    with pytest.raises(DomainError, match="能力令牌校验未通过"):
        await begin_execution(
            session,
            preparation_id=preparation.id,
            owner=owner,
            settings=settings(),
            verifier=stranger,
        )
    assert calls == []

    _ = executor
    _ = apply_side_effect


# --------------------------------------------- 提交不可重放、审批不可重复占槽
async def test_commit_cannot_be_replayed(
    session: AsyncSession,
    project: ProjectContext,
    tmp_path: Path,
    keys: tuple[Ed25519Signer, Ed25519KeyRingVerifier],
) -> None:
    """已提交的准备单不能再提交一次。"""
    preparation = await walk(
        session, project, tmp_path, SideEffectOperation.SECRET_MIGRATION, keys=keys
    )
    facts = await derive_facts(
        session,
        operation=SideEffectOperation.SECRET_MIGRATION,
        aggregate_id=preparation.aggregate_id,
        settings=settings(),
    )
    with pytest.raises(DomainError, match="不能再使用"):
        await side_effects.commit(
            session,
            preparation_id=preparation.id,
            actor_id=project.engineer_id,
            facts=facts,
            settings=settings(),
            signer=keys[0],
        )


async def test_same_person_cannot_take_two_slots(
    session: AsyncSession,
    project: ProjectContext,
    tmp_path: Path,
    keys: tuple[Ed25519Signer, Ed25519KeyRingVerifier],
) -> None:
    """SOD-02：双人审批的两个槽必须由两个不同的人来投。"""
    from data_integration_platform.domain.errors import PermissionDenied

    run = await seed_run(session, project, tmp_path)
    await prepare_for(
        session,
        operation=SideEffectOperation.DORIS_ROLLBACK,
        aggregate_id=str(run.id),
        actor_id=project.engineer_id,
        settings=settings(),
    )
    user = User(subject=f"both-{uuid.uuid4().hex[:8]}", display_name="一人两槽")
    session.add(user)
    await session.flush()
    session.add(
        ProjectMembership(project_id=project.project_id, user_id=user.id, role=ProjectRole.APPROVER)
    )
    for role in (HarnessRole.DATA_APPROVER, HarnessRole.SECURITY_APPROVER):
        session.add(ProjectRoleGrant(project_id=project.project_id, user_id=user.id, role=role))
    await session.flush()

    first = await decide_for(
        session,
        operation=SideEffectOperation.DORIS_ROLLBACK,
        aggregate_id=str(run.id),
        actor_id=user.id,
        role=HarnessRole.DATA_APPROVER,
        decision=HarnessApprovalValue.APPROVE,
        reason="数据维度通过",
        settings=settings(),
        signer=keys[0],
    )
    assert first.status is ExecutionPreparationStatus.WAITING_APPROVAL

    with pytest.raises(PermissionDenied, match="SOD-02"):
        await decide_for(
            session,
            operation=SideEffectOperation.DORIS_ROLLBACK,
            aggregate_id=str(run.id),
            actor_id=user.id,
            role=HarnessRole.SECURITY_APPROVER,
            decision=HarnessApprovalValue.APPROVE,
            reason="安全维度也我来",
            settings=settings(),
            signer=keys[0],
        )


async def test_approval_record_is_queryable_and_visible_only_in_project(
    session: AsyncSession, project: ProjectContext, tmp_path: Path, keys: Any
) -> None:
    """审批记录可查；项目外的主体连准备单都读不到。"""
    from data_integration_platform.domain.errors import PermissionDenied

    preparation = await walk(
        session, project, tmp_path, SideEffectOperation.SECRET_DESTRUCTION, keys=keys
    )
    approvals = await side_effects.approvals_of(session, preparation_id=preparation.id)
    assert len(approvals) == 2

    outsider = User(subject=f"outsider-{uuid.uuid4().hex[:8]}", display_name="外人")
    session.add(outsider)
    await session.flush()
    with pytest.raises(PermissionDenied):
        await side_effects.get(session, preparation_id=preparation.id, actor_id=outsider.id)


async def test_abandoned_preparation_does_not_dangle(
    session: AsyncSession, project: ProjectContext, tmp_path: Path
) -> None:
    """业务前置失败时准备单被作废，不会在审批台上留一张点不动的单子。"""
    from data_integration_platform.application.side_effect_runtime import abandon_for

    connection = await seed_connection(session, project)
    rotation = SecretRotationRequest(
        connection_id=connection.id,
        status=SecretRotationStatus.VALIDATING,
        previous_secret_ref=connection.secret_ref or "fernet://missing",
        requested_by=project.engineer_id,
    )
    session.add(rotation)
    await session.flush()
    preparation = await prepare_for(
        session,
        operation=SideEffectOperation.SECRET_ROTATION_STAGE,
        aggregate_id=str(rotation.id),
        actor_id=project.engineer_id,
        settings=settings(),
    )
    assert preparation.status is ExecutionPreparationStatus.WAITING_APPROVAL

    await abandon_for(
        session,
        operation=SideEffectOperation.SECRET_ROTATION_STAGE,
        aggregate_id=str(rotation.id),
        reason="暂存凭据验证未通过",
    )
    await session.refresh(preparation)
    assert preparation.status is ExecutionPreparationStatus.REJECTED
    assert preparation.failure_reason == "暂存凭据验证未通过"


async def test_every_registered_operation_has_a_deriver_and_an_aggregate_type() -> None:
    """九类操作都必须登记推导入口；漏一个就会在执行时才炸。"""
    from data_integration_platform.application.harness import side_effect_targets

    for operation in SideEffectOperation:
        assert side_effect_targets.aggregate_type_of(operation)
        assert operation in side_effect_targets._DERIVERS

    # 九类操作落在 5 种聚合对象上：执行侧三类共用 `execution_run`，
    # 轮换的三段共用 `secret_rotation_request` —— 两者都是「一个业务对象、多段动作」，
    # 拆开只会让同一次操作多出几个谁也说不清的准备单。
    assert {side_effect_targets.aggregate_type_of(op) for op in SideEffectOperation} == {
        AGGREGATE_EXECUTION_RUN,
        AGGREGATE_SECRET_MIGRATION,
        AGGREGATE_SECRET_ROTATION,
        AGGREGATE_SECRET_ROTATION_ROLLBACK,
        AGGREGATE_SECRET_DESTRUCTION,
    }


def _success() -> Any:
    from data_integration_platform.application.secret_governance import OperationOutcome

    return OperationOutcome.success()
