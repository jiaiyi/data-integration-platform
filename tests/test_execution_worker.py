"""执行 Worker：验签、物化与提交（T-37 / FR-44、FR-45）。

核心证据是**未验签不执行**，而且证据形式很具体：令牌不成立时，
**假引擎与假 Secret Provider 的调用次数都是 0**。只断言「返回了失败」不够 ——
「先读了凭据才发现令牌不对」正是这条不变量要挡的事。

另外守住：提交给引擎的是**冻结在盘上的那份 HOCON**（不是内存里的候选）、
物化后的明文**不进审计**（只记占位符名字）、引擎不可用时靠 Outbox 重投而不是直接判死。
"""

import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from pydantic import SecretStr
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from data_integration_platform.application.harness.approval import decide
from data_integration_platform.application.harness.commit import commit
from data_integration_platform.application.harness.engine import EngineJobState
from data_integration_platform.application.harness.preparation import prepare
from data_integration_platform.application.secrets import ConnectionCredentials, SecretProviderPort
from data_integration_platform.application.versions import PipelineVersionService
from data_integration_platform.config import Settings
from data_integration_platform.domain.enums import (
    ApprovalDecisionValue,
    ConnectionStatus,
    ConnectorType,
    DataEndpointRole,
    DataProfileStatus,
    ExecutionStatus,
    PipelineVersionStatus,
    ProjectRole,
)
from data_integration_platform.domain.errors import DomainError
from data_integration_platform.domain.harness import (
    HarnessApprovalValue,
    HarnessRole,
)
from data_integration_platform.execution_tasks import _submit
from data_integration_platform.infrastructure.ed25519_signing import (
    Ed25519KeyRingVerifier,
    Ed25519Signer,
    generate_keypair,
    parse_public_keys,
)
from data_integration_platform.infrastructure.models import (
    DataConnection,
    DataProfile,
    ExecutionRun,
    HarnessCapabilityGrant,
    HarnessCommandOutbox,
    ProjectMembership,
    ProjectRoleGrant,
    User,
)
from data_integration_platform.infrastructure.secret_factory import create_secret_provider
from tests.conftest import ProjectContext
from tests.test_pipeline_versions import candidate

KEY_ID = "harness-local-1"
MYSQL_PASSWORD = "s3cret-mysql-password"
NOW = datetime(2026, 10, 16, 2, 0, tzinfo=UTC)
# 占位符名字按 `<角色>_<连接器>_<字段>` 的约定；旧夹具里的 `${MYSQL_PASSWORD}` 缺角色前缀，
# 在源端与目标端都是 MySQL 时就分不清用的是哪一个，所以执行阶段不认它。
FROZEN_HOCON = candidate().hocon.replace("${MYSQL_PASSWORD}", "${SOURCE_MYSQL_PASSWORD}")
CREDENTIALS = ConnectionCredentials(username="dip_reader", password=SecretStr(MYSQL_PASSWORD))


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


class FakeEngine:
    """记账的假引擎；「没有绕过路径」靠它的计数来证明。"""

    def __init__(self, *, fail_with: Exception | None = None) -> None:
        self.calls: list[tuple[str, str]] = []
        self.fail_with = fail_with

    async def submit(self, *, config: str, job_name: str) -> str:
        self.calls.append((config, job_name))
        if self.fail_with is not None:
            raise self.fail_with
        return "engine-job-1"

    async def status(self, *, job_id: str) -> EngineJobState | None:
        return None

    async def cancel(self, *, job_id: str) -> None:
        return None


class SpySecrets:
    """记账的假凭据存储；未验签时它的 `load` 一次都不该被调到。"""

    provider_name = "spy"

    def __init__(self, credentials: ConnectionCredentials | None) -> None:
        self.credentials = credentials
        self.loads: list[str | None] = []

    async def store(self, **kwargs: object) -> object:  # pragma: no cover - 用例不需要
        raise NotImplementedError

    async def load(self, secret_ref: str) -> ConnectionCredentials:
        self.loads.append(secret_ref)
        # 不在这里断言「本该有凭据」：这个假实现同时服务两类用例 ——
        # 一类要验「根本没读」，另一类要验「读了并对」。断言留给调用方。
        return self.credentials or ConnectionCredentials()

    async def destroy(self, secret_ref: str) -> None:  # pragma: no cover
        return None


async def _connection_with_secret(
    session: AsyncSession,
    project: ProjectContext,
    *,
    name: str,
    connector: ConnectorType,
    role: DataEndpointRole,
    table_name: str,
    schema_name: str,
) -> DataConnection:
    connection = DataConnection(
        project_id=project.project_id,
        name=name,
        connector_type=connector,
        endpoint_role=role,
        status=ConnectionStatus.READY,
        host="127.0.0.1",
        port=13306,
        database_name=schema_name,
        options={},
        created_by=project.engineer_id,
    )
    session.add(connection)
    await session.flush()
    stored = await create_secret_provider(session).store(
        project_id=project.project_id,
        name=name,
        credentials=ConnectionCredentials(
            username="dip_reader", password=SecretStr(MYSQL_PASSWORD)
        ),
    )
    connection.secret_ref = stored.secret_ref
    session.add(
        DataProfile(
            connection_id=connection.id,
            schema_name=schema_name,
            table_name=table_name,
            endpoint_role=role,
            status=DataProfileStatus.READY,
            columns=[{"name": "id", "type": "bigint"}],
            primary_keys=["id"],
            expires_at=NOW + timedelta(days=30),
        )
    )
    await session.flush()
    return connection


async def _approver(session: AsyncSession, project: ProjectContext) -> User:
    user = User(subject=f"approver-{uuid.uuid4().hex[:8]}", display_name="审批人")
    session.add(user)
    await session.flush()
    session.add(
        ProjectMembership(project_id=project.project_id, user_id=user.id, role=ProjectRole.APPROVER)
    )
    session.add(
        ProjectRoleGrant(
            project_id=project.project_id, user_id=user.id, role=HarnessRole.DATA_APPROVER
        )
    )
    await session.flush()
    return user


async def committed_run(
    session: AsyncSession,
    project: ProjectContext,
    tmp_path: Path,
    keys: tuple[Ed25519Signer, Ed25519KeyRingVerifier],
) -> tuple[HarnessCommandOutbox, ExecutionRun]:
    """一路推到 Commit：建连接与 Profile → 版本 → 审批 → Prepare → 审批 → Commit。"""
    await _connection_with_secret(
        session,
        project,
        name=f"mysql-{uuid.uuid4().hex[:6]}",
        connector=ConnectorType.MYSQL,
        role=DataEndpointRole.SOURCE,
        schema_name="source_db",
        table_name="orders",
    )
    await _connection_with_secret(
        session,
        project,
        name=f"doris-{uuid.uuid4().hex[:6]}",
        connector=ConnectorType.DORIS,
        role=DataEndpointRole.SINK,
        schema_name="dw",
        table_name="orders_wide",
    )
    # 用**默认**制品根：Worker 读冻结制品走的就是默认根，两边不一致会读不到文件。
    versions = PipelineVersionService()
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
        session,
        version_id=version.id,
        actor_id=project.engineer_id,
        candidate=candidate(hocon=FROZEN_HOCON),
    )
    request = await versions.submit_for_approval(
        session, version_id=version.id, actor_id=project.engineer_id
    )
    approver = await _approver(session, project)
    await versions.decide_approval(
        session,
        approval_id=request.id,
        actor_id=approver.id,
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
        actor_id=approver.id,
        role=HarnessRole.DATA_APPROVER,
        decision=HarnessApprovalValue.APPROVE,
        reason="通过",
        settings=settings(),
    )
    run = await commit(
        session,
        preparation_id=preparation.id,
        actor_id=project.engineer_id,
        settings=settings(),
        signer=keys[0],
        verifier=keys[1],
    )
    command = await session.scalar(
        select(HarnessCommandOutbox).where(HarnessCommandOutbox.preparation_id == preparation.id)
    )
    assert command is not None
    return command, run


def spy_factory(spy: SpySecrets):  # type: ignore[no-untyped-def]
    def factory(session: AsyncSession) -> SecretProviderPort:
        _ = session
        return spy

    return factory


# ------------------------------------------------------------------ 正向路径
async def test_verified_command_materializes_and_submits(
    session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    project: ProjectContext,
    tmp_path: Path,
    keys: tuple[Ed25519Signer, Ed25519KeyRingVerifier],
) -> None:
    command, run = await committed_run(session, project, tmp_path, keys)
    await session.commit()
    engine = FakeEngine()
    spy = SpySecrets(
        ConnectionCredentials(username="dip_reader", password=SecretStr(MYSQL_PASSWORD))
    )

    outcome = await _submit(
        command.id,
        session_factory=session_factory,
        engine=engine,
        verifier=keys[1],
        secrets_factory=spy_factory(spy),
    )

    assert outcome.status is ExecutionStatus.SUBMITTED
    assert outcome.engine_job_id == "engine-job-1"
    assert len(engine.calls) == 1
    submitted_config, _ = engine.calls[0]
    # 提交的是**冻结在盘上的那份 HOCON**，且占位符已经换成了真实凭据。
    assert MYSQL_PASSWORD in submitted_config
    assert "${" not in submitted_config
    await session.refresh(run)
    assert run.status is ExecutionStatus.SUBMITTED
    assert run.seatunnel_job_id == "engine-job-1"


async def test_secrets_are_only_read_after_verification(
    session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    project: ProjectContext,
    tmp_path: Path,
    keys: tuple[Ed25519Signer, Ed25519KeyRingVerifier],
) -> None:
    """读 Secret 发生在验签**之后**：正向路径里两次读取（源端与目标端）。"""
    command, _ = await committed_run(session, project, tmp_path, keys)
    await session.commit()
    spy = SpySecrets(ConnectionCredentials(username="u", password=SecretStr("p")))

    await _submit(
        command.id,
        session_factory=session_factory,
        engine=FakeEngine(),
        verifier=keys[1],
        secrets_factory=spy_factory(spy),
    )

    assert len(spy.loads) == 2


async def test_dispatched_command_is_terminal(
    session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    project: ProjectContext,
    tmp_path: Path,
    keys: tuple[Ed25519Signer, Ed25519KeyRingVerifier],
) -> None:
    command, _ = await committed_run(session, project, tmp_path, keys)
    await session.commit()

    await _submit(
        command.id,
        session_factory=session_factory,
        engine=FakeEngine(),
        verifier=keys[1],
        secrets_factory=spy_factory(SpySecrets(CREDENTIALS)),
    )

    await session.refresh(command)
    assert command.status.value == "DISPATCHED"
    assert command.dispatched_at is not None


# ------------------------------------------------------------------ 未验签不执行
async def test_tampered_capability_never_reads_secrets_nor_touches_the_engine(
    session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    project: ProjectContext,
    tmp_path: Path,
    keys: tuple[Ed25519Signer, Ed25519KeyRingVerifier],
) -> None:
    """**核心证据**：令牌被改过时，假引擎与假 Secret Provider 的调用次数都是 0。"""
    command, run = await committed_run(session, project, tmp_path, keys)
    grant = await session.scalar(
        select(HarnessCapabilityGrant).where(HarnessCapabilityGrant.execution_run_id == run.id)
    )
    assert grant is not None
    grant.claims_signature_value = "被改过的签名"
    await session.commit()
    engine = FakeEngine()
    spy = SpySecrets(ConnectionCredentials(username="u", password=SecretStr("p")))

    outcome = await _submit(
        command.id,
        session_factory=session_factory,
        engine=engine,
        verifier=keys[1],
        secrets_factory=spy_factory(spy),
    )

    assert outcome.status is ExecutionStatus.FAILED
    assert engine.calls == []
    assert spy.loads == []
    await session.refresh(run)
    assert run.status is ExecutionStatus.FAILED
    assert run.failure_stage.value == "SUBMISSION"


async def test_expired_capability_never_reaches_the_engine(
    session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    project: ProjectContext,
    tmp_path: Path,
    keys: tuple[Ed25519Signer, Ed25519KeyRingVerifier],
) -> None:
    command, _ = await committed_run(session, project, tmp_path, keys)
    await session.commit()
    engine = FakeEngine()
    spy = SpySecrets(None)

    outcome = await _submit(
        command.id,
        session_factory=session_factory,
        engine=engine,
        verifier=keys[1],
        secrets_factory=spy_factory(spy),
        now=datetime.now(UTC) + timedelta(days=1),
    )

    assert outcome.status is ExecutionStatus.FAILED
    assert "已过期" in (outcome.reason or "")
    assert engine.calls == []
    assert spy.loads == []


async def test_command_without_a_grant_never_reaches_the_engine(
    session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    project: ProjectContext,
    tmp_path: Path,
    keys: tuple[Ed25519Signer, Ed25519KeyRingVerifier],
) -> None:
    command, _ = await committed_run(session, project, tmp_path, keys)
    await session.commit()
    engine = FakeEngine()
    spy = SpySecrets(None)

    # 把命令挪到另一条不存在的运行上，制造「有命令、没令牌」。
    command.command_payload = {**command.command_payload, "run_id": str(uuid.uuid4())}
    from data_integration_platform.domain.harness import canonical_sha256

    command.payload_sha256 = canonical_sha256(command.command_payload)
    await session.commit()

    outcome = await _submit(
        command.id,
        session_factory=session_factory,
        engine=engine,
        verifier=keys[1],
        secrets_factory=spy_factory(spy),
    )

    assert outcome.status is ExecutionStatus.FAILED
    assert engine.calls == []
    assert spy.loads == []


async def test_tampered_payload_digest_never_reaches_the_engine(
    session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    project: ProjectContext,
    tmp_path: Path,
    keys: tuple[Ed25519Signer, Ed25519KeyRingVerifier],
) -> None:
    """命令在库里被改过：摘要对不上就不投递，也不读凭据。"""
    command, _ = await committed_run(session, project, tmp_path, keys)
    command.command_payload = {**command.command_payload, "job_name": "被改过的作业"}
    await session.commit()
    engine = FakeEngine()
    spy = SpySecrets(None)

    outcome = await _submit(
        command.id,
        session_factory=session_factory,
        engine=engine,
        verifier=keys[1],
        secrets_factory=spy_factory(spy),
    )

    assert outcome.status is ExecutionStatus.FAILED
    assert "载荷与登记摘要不一致" in (outcome.reason or "")
    assert engine.calls == []
    assert spy.loads == []


# ------------------------------------------------------------------ 物化
async def test_unknown_placeholder_is_refused_before_submitting(
    session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    project: ProjectContext,
    tmp_path: Path,
    keys: tuple[Ed25519Signer, Ed25519KeyRingVerifier],
) -> None:
    """模型编了一个平台没登记的名字：拒绝提交，而不是把字面量当口令送去连库。"""
    command, run = await committed_run(session, project, tmp_path, keys)
    await session.commit()
    engine = FakeEngine()
    spy = SpySecrets(ConnectionCredentials(username="u", password=SecretStr("p")))

    # 用一份含未知占位符的制品替换掉冻结内容（模拟生成阶段漏检）。
    original = PipelineVersionService()
    artifacts = await original.artifacts_of(session, version_id=run.pipeline_version_id)
    hocon = artifacts["HOCON"]
    from data_integration_platform.infrastructure.probe_support import run_blocking

    await run_blocking(
        (original.root() / hocon.storage_ref).write_text,
        FROZEN_HOCON.replace("${SOURCE_MYSQL_PASSWORD}", "${模型自己编的名字}"),
        encoding="utf-8",
    )
    await session.commit()

    outcome = await _submit(
        command.id,
        session_factory=session_factory,
        engine=engine,
        verifier=keys[1],
        secrets_factory=spy_factory(spy),
    )

    assert outcome.status is ExecutionStatus.FAILED
    assert "未登记的凭据占位符" in (outcome.reason or "")
    assert engine.calls == []


async def test_materialized_credentials_never_enter_the_audit_log(
    session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    project: ProjectContext,
    tmp_path: Path,
    keys: tuple[Ed25519Signer, Ed25519KeyRingVerifier],
) -> None:
    """审计只记「用了哪些占位符名字」，明文口令不落任何一张表。"""
    command, _ = await committed_run(session, project, tmp_path, keys)
    await session.commit()

    await _submit(
        command.id,
        session_factory=session_factory,
        engine=FakeEngine(),
        verifier=keys[1],
        secrets_factory=spy_factory(
            SpySecrets(
                ConnectionCredentials(username="dip_reader", password=SecretStr(MYSQL_PASSWORD))
            )
        ),
    )

    from data_integration_platform.infrastructure.models import AuditEvent

    summaries = await session.scalars(
        select(AuditEvent.change_summary).where(AuditEvent.event_type == "EXECUTION_SUBMITTED")
    )
    recorded = str(summaries.all())
    assert MYSQL_PASSWORD not in recorded
    assert "MYSQL_PASSWORD" in recorded  # 名字可以留痕，值不行


# ------------------------------------------------------------------ 引擎不可用
async def test_unavailable_engine_returns_the_command_to_the_queue(
    session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    project: ProjectContext,
    tmp_path: Path,
    keys: tuple[Ed25519Signer, Ed25519KeyRingVerifier],
) -> None:
    """引擎不可用是**可重投**的：命令回到队列，运行停在 QUEUED，而不是当场判死。"""
    from data_integration_platform.domain.errors import ExternalServiceUnavailable

    command, run = await committed_run(session, project, tmp_path, keys)
    await session.commit()
    engine = FakeEngine(fail_with=ExternalServiceUnavailable("引擎请求失败（POST /submit-job）"))

    outcome = await _submit(
        command.id,
        session_factory=session_factory,
        engine=engine,
        verifier=keys[1],
        secrets_factory=spy_factory(SpySecrets(CREDENTIALS)),
    )

    assert outcome.retryable is True
    assert outcome.status is ExecutionStatus.QUEUED
    await session.refresh(command)
    assert command.status.value == "PENDING"
    await session.refresh(run)
    assert run.status is ExecutionStatus.QUEUED


async def test_engine_rejection_is_terminal(
    session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    project: ProjectContext,
    tmp_path: Path,
    keys: tuple[Ed25519Signer, Ed25519KeyRingVerifier],
) -> None:
    """引擎明确拒绝（配置不合法）：重投也没用，记为终态。"""
    command, run = await committed_run(session, project, tmp_path, keys)
    await session.commit()
    engine = FakeEngine(fail_with=DomainError("配置不合法"))

    outcome = await _submit(
        command.id,
        session_factory=session_factory,
        engine=engine,
        verifier=keys[1],
        secrets_factory=spy_factory(SpySecrets(CREDENTIALS)),
    )

    assert outcome.status is ExecutionStatus.FAILED
    assert outcome.retryable is False
    await session.refresh(run)
    assert run.failure_category.value == "CONFIGURATION"
