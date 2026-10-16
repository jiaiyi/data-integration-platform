"""能力令牌的登记与防重放（T-29 / FR-46）。

证据是**第二次使用必失败**（G-09）。除此之外守住三件：
- 令牌没登记过就消费不了（「没登记」不等于「可以先用着」）；
- 主体对不上同样失败（令牌被偷走后换个主体来用，不该走过去）；
- 摘要重复登记在数据层就不成立（唯一约束）。
"""

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from data_integration_platform.application.harness.capability import (
    SignedCapability,
    issue_capability,
)
from data_integration_platform.application.harness.grants import consume, record
from data_integration_platform.application.secret_governance import as_utc
from data_integration_platform.domain.errors import (
    DomainError,
    ResourceNotFound,
    VersionConflict,
)
from data_integration_platform.domain.harness import RiskLevel
from data_integration_platform.infrastructure.ed25519_signing import Ed25519Signer, generate_keypair
from data_integration_platform.infrastructure.models import (
    ExecutionPreparation,
    PipelineVersion,
    Project,
)
from tests.conftest import ProjectContext

NOW = datetime(2026, 10, 16, 2, 0, tzinfo=UTC)
TTL = 300
SUBJECT = "22222222-2222-2222-2222-222222222222"
OTHER_SUBJECT = "33333333-3333-3333-3333-333333333333"


@pytest.fixture(scope="module")
def signer() -> Ed25519Signer:
    private, _ = generate_keypair()
    return Ed25519Signer(key_id="harness-local-1", private_key=private)


def token(signer: Ed25519Signer, **overrides: object) -> SignedCapability:
    values: dict[str, object] = {
        "preparation_id": "66666666-6666-6666-6666-666666666666",
        "tool_name": "seatunnel.submit_job",
        "tool_version": "2.3.13",
        "subject_id": SUBJECT,
        "environment": "local",
        "artifact_sha256": "a" * 64,
        "input_fingerprint": "b" * 64,
        "resource_fingerprint": "c" * 64,
        "supervision_contract_sha256": "d" * 64,
        "data_classification_sha256": "e" * 64,
        "policy_bundle_version": "harness-v1-20261016",
        "risk_level": RiskLevel.P0,
        "ttl_seconds": TTL,
        "signer": signer,
        "now": NOW,
    }
    values.update(overrides)
    return issue_capability(**values)  # type: ignore[arg-type]


@pytest.fixture
async def preparation(session: AsyncSession, project: ProjectContext) -> ExecutionPreparation:
    """一张准备单 —— 能力令牌是挂在它上面的（外键非空）。"""
    pipeline = Project(name=f"管道-{uuid.uuid4().hex[:6]}")
    session.add(pipeline)
    await session.flush()
    version = PipelineVersion(
        pipeline_id=pipeline.id,
        version_number=1,
        requirement="把订单同步到宽表",
        created_by=project.engineer_id,
    )
    session.add(version)
    await session.flush()
    row = ExecutionPreparation(
        pipeline_version_id=version.id,
        risk_level=RiskLevel.P0,
        policy_bundle_version="harness-v1-20261016",
        environment="local",
        tool_name="seatunnel.submit_job",
        tool_version="2.3.13",
        artifact_sha256="a" * 64,
        input_fingerprint="b" * 64,
        resource_fingerprint="c" * 64,
        supervision_contract_sha256="d" * 64,
        data_classification_sha256="e" * 64,
        idempotency_key=f"prep-{uuid.uuid4().hex[:12]}",
        expires_at=NOW + timedelta(days=1),
        created_by=project.engineer_id,
    )
    session.add(row)
    await session.flush()
    return row


# ------------------------------------------------------------------ 核心证据
async def test_second_use_of_a_token_fails(
    session: AsyncSession, signer: Ed25519Signer, preparation: ExecutionPreparation
) -> None:
    """G-09：单次令牌用第二次必须失败。"""
    issued = token(signer)
    await record(session, preparation_id=preparation.id, token=issued)
    run_id = uuid.uuid4()

    first = await consume(
        session,
        token_sha256=issued.token_sha256(),
        subject_id=SUBJECT,
        execution_run_id=run_id,
        now=NOW,
    )
    assert first.consumed_at is not None
    assert first.execution_run_id == run_id

    with pytest.raises(VersionConflict, match="已被使用过"):
        await consume(
            session,
            token_sha256=issued.token_sha256(),
            subject_id=SUBJECT,
            execution_run_id=uuid.uuid4(),
            now=NOW,
        )


async def test_consume_records_who_and_which_run(
    session: AsyncSession, signer: Ed25519Signer, preparation: ExecutionPreparation
) -> None:
    """消费要留下「谁在什么时候、为哪次执行用掉的」。"""
    issued = token(signer)
    await record(session, preparation_id=preparation.id, token=issued)
    run_id = uuid.uuid4()

    grant = await consume(
        session,
        token_sha256=issued.token_sha256(),
        subject_id=SUBJECT,
        execution_run_id=run_id,
        now=NOW,
    )

    assert grant.consumed_by == uuid.UUID(SUBJECT)
    assert as_utc(grant.consumed_at) == NOW
    assert grant.execution_run_id == run_id


# ------------------------------------------------------------------ 其余拒绝
async def test_unregistered_token_cannot_be_consumed(session: AsyncSession) -> None:
    """没登记过的令牌消费不了：「没登记」不等于「可以先用着」。"""
    with pytest.raises(ResourceNotFound, match="不存在或未被登记"):
        await consume(
            session,
            token_sha256="f" * 64,
            subject_id=SUBJECT,
            execution_run_id=uuid.uuid4(),
        )


async def test_token_cannot_be_used_by_another_subject(
    session: AsyncSession, signer: Ed25519Signer, preparation: ExecutionPreparation
) -> None:
    """令牌被偷走后换个主体来用：同样走不过去，且额度不被消耗（原主还能用）。"""
    issued = token(signer)
    await record(session, preparation_id=preparation.id, token=issued)

    with pytest.raises(DomainError, match="主体与本次执行不一致"):
        await consume(
            session,
            token_sha256=issued.token_sha256(),
            subject_id=OTHER_SUBJECT,
            execution_run_id=uuid.uuid4(),
        )

    # 失败的那次没有把它消费掉。
    grant = await consume(
        session,
        token_sha256=issued.token_sha256(),
        subject_id=SUBJECT,
        execution_run_id=uuid.uuid4(),
        now=NOW,
    )
    assert as_utc(grant.consumed_at) == NOW


async def test_duplicate_registration_is_rejected(
    session: AsyncSession, signer: Ed25519Signer, preparation: ExecutionPreparation
) -> None:
    """同一张令牌登记两次在数据层就不成立（`token_sha256` 唯一）。"""
    issued = token(signer)
    await record(session, preparation_id=preparation.id, token=issued)

    with pytest.raises(VersionConflict, match="已登记过"):
        await record(session, preparation_id=preparation.id, token=issued)


async def test_malformed_subject_is_rejected(
    session: AsyncSession, signer: Ed25519Signer, preparation: ExecutionPreparation
) -> None:
    issued = token(signer)
    await record(session, preparation_id=preparation.id, token=issued)

    with pytest.raises(DomainError, match="不是合法的 UUID"):
        await consume(
            session,
            token_sha256=issued.token_sha256(),
            subject_id="not-a-uuid",
            execution_run_id=uuid.uuid4(),
        )


# ------------------------------------------------------------------ 登记内容
async def test_registered_grant_carries_the_signed_claims(
    session: AsyncSession, signer: Ed25519Signer, preparation: ExecutionPreparation
) -> None:
    """登记行要能独立重建出令牌（声明体 + 签名 + 算法 + key_id 都在），
    否则事后核对「这张令牌当时签的是什么」就得回头翻日志。"""
    issued = token(signer)

    grant = await record(session, preparation_id=preparation.id, token=issued)

    assert grant.token_sha256 == issued.token_sha256()
    assert grant.claims == issued.claims.canonical_payload()
    assert grant.claims_signature_value == issued.signature
    assert grant.claims_signature_algorithm == issued.algorithm
    assert grant.key_id == issued.key_id
    assert grant.single_use is True
    assert grant.expires_at == NOW + timedelta(seconds=TTL)
    assert grant.token_id == uuid.UUID(issued.claims.token_id)


async def test_two_distinct_tokens_are_both_consumable(
    session: AsyncSession, signer: Ed25519Signer, preparation: ExecutionPreparation
) -> None:
    """两张不同的令牌互不影响 —— 防重放不该变成「一次只能有一个执行」。"""
    first, second = token(signer), token(signer)
    await record(session, preparation_id=preparation.id, token=first)
    await record(session, preparation_id=preparation.id, token=second)

    assert first.token_sha256() != second.token_sha256()
    for issued in (first, second):
        grant = await consume(
            session,
            token_sha256=issued.token_sha256(),
            subject_id=SUBJECT,
            execution_run_id=uuid.uuid4(),
            now=NOW,
        )
        assert as_utc(grant.consumed_at) == NOW
