"""能力令牌的登记与原子消费（T-29 / FR-46）。

防重放不能靠「先查再写」：两个 Worker 同时拿着同一张令牌，两个进程都会查到
「还没被消费」，然后各自往下走。所以消费是**一条带条件的原子 UPDATE**：

```sql
UPDATE harness_capability_grants
   SET consumed_at = now(), consumed_by = :subject
 WHERE token_sha256 = :digest AND consumed_at IS NULL AND subject_id = :subject
```

行数为 0 就是不放行 —— 已经被消费过、或者持有人对不上。行锁由数据库给，
不依赖应用层的互斥。

`token_sha256` 上的唯一约束是第二道：同一张令牌被登记两次在数据层就不成立，
因此「同一份声明重放」与「登记重复」在结构上都被挡住。

`subject_id` 一并进 WHERE 条件而不是单独查一次：令牌被偷走后换个主体来用，
在这种写法下和重放在同一条语句里被拒，不需要额外的判断分支。

这里**不额外写审计事件**：`harness_capability_grants` 本身就是这条事实的台账，
再复制一份进 `audit_events` 只会让「哪份是权威」变得含糊。
"""

import uuid
from datetime import UTC, datetime
from typing import Any, cast

from sqlalchemy import select, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from data_integration_platform.application.harness.capability import SignedCapability
from data_integration_platform.domain.errors import DomainError, ResourceNotFound, VersionConflict
from data_integration_platform.infrastructure.models import HarnessCapabilityGrant


async def record(
    session: AsyncSession, *, preparation_id: uuid.UUID, token: SignedCapability
) -> HarnessCapabilityGrant:
    """登记一张刚签发的令牌；摘要重复即冲突。"""
    grant = HarnessCapabilityGrant(
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


async def consume(
    session: AsyncSession,
    *,
    token_sha256: str,
    subject_id: str,
    execution_run_id: uuid.UUID,
    now: datetime | None = None,
) -> HarnessCapabilityGrant:
    """原子消费一张令牌；第二次使用必失败。

    返回被消费的那一行，便于调用方取声明体继续往下走。
    """
    try:
        consumer = uuid.UUID(subject_id)
    except ValueError as exc:
        raise DomainError(f"主体标识不是合法的 UUID：{subject_id!r}") from exc

    result = await session.execute(
        update(HarnessCapabilityGrant)
        .where(
            HarnessCapabilityGrant.token_sha256 == token_sha256,
            HarnessCapabilityGrant.consumed_at.is_(None),
            HarnessCapabilityGrant.subject_id == subject_id,
        )
        .values(
            consumed_at=now if now is not None else datetime.now(UTC),
            consumed_by=consumer,
            execution_run_id=execution_run_id,
        )
        .execution_options(synchronize_session=False)
    )
    consumed = cast("CursorResult[Any]", result).rowcount == 1
    existing = await session.scalar(
        select(HarnessCapabilityGrant).where(HarnessCapabilityGrant.token_sha256 == token_sha256)
    )
    if consumed:
        assert existing is not None
        return existing

    # 到这里已经确定**不放行**；下面只是把失败原因说清楚，不影响安全性。
    if existing is None:
        raise ResourceNotFound("能力令牌不存在或未被登记")
    if existing.subject_id != subject_id:
        raise DomainError("能力令牌的主体与本次执行不一致")
    raise VersionConflict("该能力令牌已被使用过（单次令牌不允许重放）")
