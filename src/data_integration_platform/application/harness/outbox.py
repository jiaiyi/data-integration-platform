"""事务 Outbox（T-30 / FR-47）。

要挡住的是**最坏的那种失败**：远端作业已经起来了，本地却没有任何记录说明它是谁、
凭什么起来的。做法是把「本地事实」与「待投递命令」写进**同一个事务**，
两者要么都在、要么都不在；投递是之后从 Outbox 里捞出来做的。

因此顺序是固定的：`enqueue` 必须与执行记录在同一个 `session.begin()` 里调用，
投递则发生在事务提交**之后**。事务回滚了就不会有待投递命令，
远端也就不可能凭空多出一个作业。

扫描用**租约**而不是「取出来就删」：Worker 拿到一批命令后崩掉，租约到期后
同一批命令会重新可领 —— 命令不丢，也不会两个 Worker 同时投同一批。

领取是**逐行的条件更新**，不是「先查再改」：

```sql
UPDATE harness_command_outbox
   SET status='DISPATCHING', lease_owner=:owner, lease_expires_at=:deadline
 WHERE id=:id AND (status='PENDING' OR (status='DISPATCHING' AND lease_expires_at < :now))
```

两个 Worker 可能选出同一批候选，但每一行只有一次更新会成功（行锁由数据库给）。
不用 `FOR UPDATE SKIP LOCKED` 是因为它在 SQLite 上不可用，而本项目两套库都要跑。
"""

import uuid
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta
from typing import Any, cast

from sqlalchemy import and_, or_, select, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.ext.asyncio import AsyncSession

from data_integration_platform.config import get_settings
from data_integration_platform.domain.errors import DomainError, ResourceNotFound
from data_integration_platform.domain.harness import HarnessOutboxStatus, canonical_sha256
from data_integration_platform.infrastructure.models import HarnessCommandOutbox

DEFAULT_CLAIM_LIMIT = 32


async def enqueue(
    session: AsyncSession,
    *,
    preparation_id: uuid.UUID,
    command: Mapping[str, Any],
) -> HarnessCommandOutbox:
    """登记一条待投递命令。

    **调用方必须与执行记录处于同一事务**：这条命令代表的是「本地已经记下了这次执行」，
    单独提交它就等于把「待投递」与「为什么投递」拆成了两步。
    """
    payload = dict(command)
    row = HarnessCommandOutbox(
        preparation_id=preparation_id,
        command_payload=payload,
        payload_sha256=canonical_sha256(payload),
        status=HarnessOutboxStatus.PENDING,
    )
    session.add(row)
    await session.flush()
    return row


async def claim(
    session: AsyncSession,
    *,
    owner: str,
    now: datetime | None = None,
    lease_seconds: int | None = None,
    limit: int = DEFAULT_CLAIM_LIMIT,
) -> Sequence[HarnessCommandOutbox]:
    """领取一批可投递的命令并加上租约；没有可领的返回空序列。"""
    settings = get_settings()
    moment = now if now is not None else datetime.now(UTC)
    deadline = moment + timedelta(
        seconds=lease_seconds
        if lease_seconds is not None
        else settings.harness_outbox_lease_seconds
    )

    candidates = await session.scalars(
        select(HarnessCommandOutbox.id)
        .where(_claimable(moment))
        .order_by(HarnessCommandOutbox.created_at)
        .limit(limit)
    )
    claimed: list[uuid.UUID] = []
    for row_id in candidates.all():
        result = await session.execute(
            update(HarnessCommandOutbox)
            .where(HarnessCommandOutbox.id == row_id, _claimable(moment))
            .values(
                status=HarnessOutboxStatus.DISPATCHING,
                lease_owner=owner,
                lease_expires_at=deadline,
                attempts=HarnessCommandOutbox.attempts + 1,
            )
            .execution_options(synchronize_session=False)
        )
        if cast("CursorResult[Any]", result).rowcount == 1:
            claimed.append(row_id)
    if not claimed:
        return ()
    rows = await session.scalars(
        select(HarnessCommandOutbox)
        .where(HarnessCommandOutbox.id.in_(claimed))
        .order_by(HarnessCommandOutbox.created_at)
        # 上面的 UPDATE 用的是 SQL 侧自增表达式（`attempts = attempts + 1`），
        # 会话里那份对象还是旧值；不强制刷新的话调用方读到的 `attempts`
        # 永远停在第一次领到时的数字，重试上限也就永远触发不了。
        .execution_options(populate_existing=True)
    )
    return rows.all()


async def mark_dispatched(
    session: AsyncSession, *, outbox_id: uuid.UUID, now: datetime | None = None
) -> HarnessCommandOutbox:
    """投递成功；终态，不再可领。"""
    row = await _require(session, outbox_id)
    row.status = HarnessOutboxStatus.DISPATCHED
    row.dispatched_at = now if now is not None else datetime.now(UTC)
    row.lease_owner = None
    row.lease_expires_at = None
    row.last_error = None
    await session.flush()
    return row


async def mark_failed(
    session: AsyncSession,
    *,
    outbox_id: uuid.UUID,
    error: str,
) -> HarnessCommandOutbox:
    """投递失败：没到次数上限就回到可重投，到了就记 FAILED 不再重试。

    回到 `PENDING` 而不是原地等租约到期，是为了让下一次扫描立刻能捞到它；
    重试次数由 `attempts` 兜住，不会无限重投。
    """
    row = await _require(session, outbox_id)
    max_attempts = get_settings().harness_outbox_max_attempts
    row.status = (
        HarnessOutboxStatus.PENDING if row.attempts < max_attempts else HarnessOutboxStatus.FAILED
    )
    row.last_error = error
    row.lease_owner = None
    row.lease_expires_at = None
    await session.flush()
    return row


def _claimable(moment: datetime) -> Any:
    """可领条件：从未投递过，或者上一次投递的租约已经过期。"""
    return or_(
        HarnessCommandOutbox.status == HarnessOutboxStatus.PENDING,
        and_(
            HarnessCommandOutbox.status == HarnessOutboxStatus.DISPATCHING,
            HarnessCommandOutbox.lease_expires_at < moment,
        ),
    )


async def _require(session: AsyncSession, outbox_id: uuid.UUID) -> HarnessCommandOutbox:
    row = await session.get(HarnessCommandOutbox, outbox_id)
    if row is None:
        raise ResourceNotFound("待投递命令不存在")
    return row


def require_payload_intact(row: HarnessCommandOutbox) -> None:
    """投递前核对载荷摘要：命令在库里被改过就不要再投出去。"""
    if canonical_sha256(row.command_payload) != row.payload_sha256:
        raise DomainError(f"待投递命令 {row.id} 的载荷与登记摘要不一致，拒绝投递")
