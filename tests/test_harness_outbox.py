"""事务 Outbox（T-30 / FR-47）。

证据是**中断后不产生无本地事实的远端作业**，可检验的形式是：
本地事实与待投递命令在同一事务里，回滚时两者一起消失（远端作业因为压根没被投递过，
所以不可能存在）。另外守住租约语义：同一批命令不会同时被两个 Worker 领走，
Worker 崩掉后租约到期能重新领，重试到上限才判失败。
"""

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from data_integration_platform.application.harness.outbox import (
    claim,
    enqueue,
    mark_dispatched,
    mark_failed,
    require_payload_intact,
)
from data_integration_platform.domain.errors import DomainError, ResourceNotFound
from data_integration_platform.domain.harness import (
    HarnessOutboxStatus,
    canonical_sha256,
)
from data_integration_platform.infrastructure.models import (
    ExecutionPreparation,
    HarnessCommandOutbox,
    PipelineVersion,
    Project,
)
from tests.conftest import ProjectContext

NOW = datetime(2026, 10, 16, 2, 0, tzinfo=UTC)
LEASE = 60
MAX_ATTEMPTS = 5


@pytest.fixture
async def preparation(session: AsyncSession, project: ProjectContext) -> ExecutionPreparation:
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
        risk_level="P0",
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


COMMAND = {"tool": "seatunnel.submit_job", "job_name": "orders-sync", "attempt": 1}


# ------------------------------------------------------------------ 原子性
async def test_command_and_local_fact_land_in_one_transaction(
    session: AsyncSession, preparation: ExecutionPreparation
) -> None:
    """同一事务里的两条写入一起可见。"""
    await enqueue(session, preparation_id=preparation.id, command=COMMAND)
    preparation.committed_by = uuid.uuid4()
    await session.commit()

    rows = await claim(session, owner="worker-1", now=NOW, lease_seconds=LEASE)
    assert len(rows) == 1
    assert rows[0].command_payload == COMMAND


async def test_rollback_leaves_neither_fact_nor_command(
    session: AsyncSession, preparation: ExecutionPreparation
) -> None:
    """**核心证据**：事务回滚时本地事实与待投递命令一起消失。

    命令没留下，就不可能有人把它投出去 —— 「无本地事实的远端作业」因此在结构上不存在。
    """
    # 先把执行记录提交掉，让它成为一条真实的本地事实。
    # 主键先取出来：提交/回滚会让会话里的对象过期，之后再读属性会触发一次
    # 同步惰性加载，在 async 会话下直接抛 MissingGreenlet。
    preparation_id = preparation.id
    await session.commit()

    await enqueue(session, preparation_id=preparation_id, command=COMMAND)
    await session.rollback()

    assert await claim(session, owner="worker-1", now=NOW, lease_seconds=LEASE) == ()
    still_there = await session.scalar(
        select(ExecutionPreparation.id).where(ExecutionPreparation.id == preparation_id)
    )
    assert still_there == preparation_id


async def test_payload_digest_is_recorded(
    session: AsyncSession, preparation: ExecutionPreparation
) -> None:
    row = await enqueue(session, preparation_id=preparation.id, command=COMMAND)

    assert row.payload_sha256 == canonical_sha256(COMMAND)
    require_payload_intact(row)


async def test_tampered_payload_is_refused(
    session: AsyncSession, preparation: ExecutionPreparation
) -> None:
    """命令在库里被改过就不要再投出去：摘要就是为这一刻留的。"""
    row = await enqueue(session, preparation_id=preparation.id, command=COMMAND)
    row.command_payload = {**COMMAND, "job_name": "被改过的作业"}

    with pytest.raises(DomainError, match="载荷与登记摘要不一致"):
        require_payload_intact(row)


# ------------------------------------------------------------------ 租约
async def test_claimed_command_is_not_claimable_again_while_the_lease_holds(
    session: AsyncSession, preparation: ExecutionPreparation
) -> None:
    """两个 Worker 不会领到同一条命令。"""
    await enqueue(session, preparation_id=preparation.id, command=COMMAND)

    first = await claim(session, owner="worker-1", now=NOW, lease_seconds=LEASE)
    second = await claim(session, owner="worker-2", now=NOW, lease_seconds=LEASE)

    assert len(first) == 1
    assert first[0].status is HarnessOutboxStatus.DISPATCHING
    assert first[0].lease_owner == "worker-1"
    assert second == ()


async def test_expired_lease_makes_the_command_claimable_again(
    session: AsyncSession, preparation: ExecutionPreparation
) -> None:
    """领走命令的 Worker 崩了：租约到期后同一条命令重新可领，而不是永远卡在 DISPATCHING。"""
    await enqueue(session, preparation_id=preparation.id, command=COMMAND)
    await claim(session, owner="worker-1", now=NOW, lease_seconds=LEASE)

    reclaimed = await claim(
        session,
        owner="worker-2",
        now=NOW + timedelta(seconds=LEASE + 1),
        lease_seconds=LEASE,
    )

    assert len(reclaimed) == 1
    assert reclaimed[0].lease_owner == "worker-2"
    # 重领算一次投递尝试：次数就是靠这里累积的，否则重试没有上限。
    assert reclaimed[0].attempts == 2


async def test_attempts_accumulate_across_leases(
    session: AsyncSession, preparation: ExecutionPreparation
) -> None:
    await enqueue(session, preparation_id=preparation.id, command=COMMAND)
    moment = NOW
    for expected in (1, 2, 3):
        rows = await claim(session, owner=f"worker-{expected}", now=moment, lease_seconds=LEASE)
        assert rows[0].attempts == expected
        moment += timedelta(seconds=LEASE + 1)


# ------------------------------------------------------------------ 终态与重试
async def test_dispatched_command_is_terminal(
    session: AsyncSession, preparation: ExecutionPreparation
) -> None:
    await enqueue(session, preparation_id=preparation.id, command=COMMAND)
    row = (await claim(session, owner="worker-1", now=NOW, lease_seconds=LEASE))[0]

    dispatched = await mark_dispatched(session, outbox_id=row.id, now=NOW)

    assert dispatched.status is HarnessOutboxStatus.DISPATCHED
    assert dispatched.dispatched_at == NOW
    assert dispatched.lease_owner is None
    assert await claim(session, owner="worker-2", now=NOW + timedelta(days=1)) == ()


async def test_failed_command_returns_to_pending_for_retry(
    session: AsyncSession, preparation: ExecutionPreparation
) -> None:
    await enqueue(session, preparation_id=preparation.id, command=COMMAND)
    row = (await claim(session, owner="worker-1", now=NOW, lease_seconds=LEASE))[0]

    failed = await mark_failed(session, outbox_id=row.id, error="引擎不可达")

    assert failed.status is HarnessOutboxStatus.PENDING
    assert failed.last_error == "引擎不可达"
    assert failed.lease_owner is None
    # 回到 PENDING 之后下一次扫描立刻能捞到，不用等租约到期。
    assert len(await claim(session, owner="worker-2", now=NOW)) == 1


async def test_command_fails_permanently_after_the_attempt_ceiling(
    session: AsyncSession, preparation: ExecutionPreparation
) -> None:
    """重试有上限：一直失败就一直重投，等于把坏命令变成常驻噪声。"""
    await enqueue(session, preparation_id=preparation.id, command=COMMAND)
    moment = NOW
    row = None
    for _ in range(MAX_ATTEMPTS):
        rows = await claim(session, owner="worker-1", now=moment, lease_seconds=LEASE)
        if not rows:
            break
        row = rows[0]
        await mark_failed(session, outbox_id=row.id, error="引擎不可达")
        moment += timedelta(seconds=1)

    assert row is not None
    assert row.status is HarnessOutboxStatus.FAILED
    assert await claim(session, owner="worker-2", now=moment + timedelta(days=1)) == ()


async def test_unknown_command_is_rejected(session: AsyncSession) -> None:
    with pytest.raises(ResourceNotFound):
        await mark_dispatched(session, outbox_id=uuid.uuid4())


async def test_claim_limit_is_respected(
    session: AsyncSession, preparation: ExecutionPreparation
) -> None:
    for index in range(3):
        await enqueue(session, preparation_id=preparation.id, command={**COMMAND, "attempt": index})

    rows = await claim(session, owner="worker-1", now=NOW, lease_seconds=LEASE, limit=2)

    assert len(rows) == 2
    remaining = await claim(session, owner="worker-1", now=NOW, lease_seconds=LEASE, limit=2)
    assert len(remaining) == 1


async def test_empty_outbox_yields_nothing(session: AsyncSession) -> None:
    assert await claim(session, owner="worker-1", now=NOW) == ()


async def test_outbox_row_is_reachable_by_preparation(
    session: AsyncSession, preparation: ExecutionPreparation
) -> None:
    row = await enqueue(session, preparation_id=preparation.id, command=COMMAND)

    assert row.preparation_id == preparation.id
    assert isinstance(row, HarnessCommandOutbox)
