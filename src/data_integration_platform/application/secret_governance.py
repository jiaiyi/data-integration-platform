"""凭据治理共用原语（T-15 / FR-18 ~ FR-23）。

这里放**四类申请都要遵守的同一套规矩**，不放各类自己的业务：
- 状态前置校验：不满足前置状态就拒绝，不接受「跳步」；
- 审批人身份：具名、且不得是申请人（SOD-01）；
- 到期口径：由「签发时间 + 最大年龄」与「预警天数」推导等级，是纯函数，可脱离数据库测试。

> FR-23 要求四类操作都走「申请—审批—提交」同一受管入口。本轮实现的是这条链路的
> 数据结构与状态机；把它接到 Harness 的 Capability / 事务 Outbox 上属 M5 的 T-36，
> 在那之前**不能声称 FR-57 已满足**。
"""

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from data_integration_platform.domain.enums import CredentialAlertLevel
from data_integration_platform.domain.errors import DomainError, InvalidStateTransition
from data_integration_platform.domain.harness import require_no_self_approval
from data_integration_platform.infrastructure.models import EncryptedSecret


@dataclass(frozen=True, slots=True)
class OperationOutcome:
    """一次凭据受管操作的执行结论。

    **刻意不带 `retryable`**：凭据操作不做自动重试。迁移/轮换中途失败可能已经写入了
    新的存储位置，自动重试会把「部分完成」放大成更难收拾的状态；
    失败一律落成 `FAILED` 事实，由人决定是重新申请还是回退。
    """

    succeeded: bool
    reason: str | None = None

    @classmethod
    def success(cls) -> "OperationOutcome":
        return cls(succeeded=True)

    @classmethod
    def failure(cls, reason: str) -> "OperationOutcome":
        return cls(succeeded=False, reason=reason)


async def local_secret_id(session: AsyncSession, secret_ref: str | None) -> uuid.UUID | None:
    """Fernet 后端的凭据在 `encrypted_secrets` 里有行；Vault 引用没有，返回 `None`。

    该列只是**便利指针**，申请表的锚点是 `connection_id`，因此允许为空。
    """
    if not secret_ref:
        return None
    return await session.scalar(
        select(EncryptedSecret.id).where(EncryptedSecret.secret_ref == secret_ref)
    )


def as_utc(value: datetime) -> datetime:
    """把可能缺失时区的时间按 UTC 解释。

    SPEC 要求统一 UTC 存储，但**不能假设存储层一定保留了 tzinfo**：
    SQLite 这类后端读回来的 `DateTime(timezone=True)` 是 naive 的，
    直接与 aware 时间比较会抛 `TypeError`。领域规则只关心「哪个时刻更早」，
    因此这里统一补上 UTC，而不是把时区假设散落到每个比较点。
    """
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def _now(now: datetime | None) -> datetime:
    """比较基准时刻；调用方传进来的也可能是 naive。"""
    return as_utc(now) if now is not None else datetime.now(UTC)


def require_status(current: StrEnum, expected: frozenset[StrEnum], *, action: str) -> None:
    """前置状态校验：不在允许集合内一律拒绝，并说清当前状态与允许状态。"""
    if current not in expected:
        allowed = "/".join(sorted(state.value for state in expected))
        raise InvalidStateTransition(f"{action}要求状态为 {allowed}，当前为 {current.value}")


def require_independent_approval(*, requested_by: Any, approver_id: Any) -> None:
    """SOD-01：申请人禁止自批，凭据类受管副作用同样适用。"""
    require_no_self_approval(applicant_id=str(requested_by), approver_id=str(approver_id))


def credential_due_at(*, issued_at: datetime | None, max_age_days: int | None) -> datetime | None:
    """由「凭据签发时间 + 最大年龄」推导到期时刻。

    两者缺一即无法判定期限，返回 `None` —— 不猜一个默认值，
    因为猜出来的到期日会直接变成误报或漏报。
    """
    if issued_at is None or max_age_days is None or max_age_days <= 0:
        return None
    return as_utc(issued_at) + timedelta(days=max_age_days)


def classify_credential_expiry(
    *, due_at: datetime | None, warning_days: int | None, now: datetime | None = None
) -> CredentialAlertLevel | None:
    """判定告警等级：已到期记 `OVERDUE`，进入预警窗口记 `WARNING`，否则不告警。"""
    if due_at is None:
        return None
    moment = _now(now)
    if moment >= as_utc(due_at):
        return CredentialAlertLevel.OVERDUE
    if (
        warning_days is not None
        and warning_days > 0
        and moment >= as_utc(due_at) - timedelta(days=warning_days)
    ):
        return CredentialAlertLevel.WARNING
    return None


def cooldown_for(
    level: CredentialAlertLevel, *, warning_hours: int, overdue_hours: int
) -> timedelta:
    """同一等级的重复告警冷却窗口；预警与逾期分开配置，避免逾期被预警节奏掩盖。"""
    if level is CredentialAlertLevel.OVERDUE:
        return timedelta(hours=overdue_hours)
    return timedelta(hours=warning_hours)


def is_in_cooldown(
    *, last_opened_at: datetime | None, cooldown: timedelta, now: datetime | None = None
) -> bool:
    """距上次开告警是否仍在冷却窗口内。"""
    if last_opened_at is None:
        return False
    return _now(now) - as_utc(last_opened_at) < cooldown


def require_rotation_rollback_window_open(
    *, activated_at: datetime | None, deadline: datetime | None, now: datetime | None = None
) -> None:
    """回退必须在保留期内；过期后拒绝，而不是照做。"""
    if activated_at is None:
        raise DomainError("该轮换尚未激活，无法回退")
    if deadline is None:
        raise DomainError("该轮换缺少回退截止时间，按不可回退处理")
    if _now(now) >= as_utc(deadline):
        raise DomainError("轮换回退窗口已过，需改为重新轮换而不是回退旧凭据")
