"""凭据治理共用原语（T-15）。

这些是**纯函数**：到期口径、冷却窗口、回退窗口、状态前置校验与申请人自批拦截。
不连数据库也不连外部服务，因此可以在秒级覆盖边界。
"""

from datetime import UTC, datetime, timedelta

import pytest

from data_integration_platform.application.secret_governance import (
    OperationOutcome,
    classify_credential_expiry,
    cooldown_for,
    credential_due_at,
    is_in_cooldown,
    require_independent_approval,
    require_rotation_rollback_window_open,
    require_status,
)
from data_integration_platform.domain.enums import CredentialAlertLevel, SecretRotationStatus
from data_integration_platform.domain.errors import (
    DomainError,
    InvalidStateTransition,
    PermissionDenied,
)

NOW = datetime(2026, 10, 16, 12, 0, tzinfo=UTC)


# ------------------------------------------------------------------ 到期口径
def test_due_at_needs_both_issue_time_and_max_age() -> None:
    """缺任一项都无法判定期限：返回 None，而不是猜一个默认值造成误报。"""
    assert credential_due_at(issued_at=None, max_age_days=90) is None
    assert credential_due_at(issued_at=NOW, max_age_days=None) is None
    assert credential_due_at(issued_at=NOW, max_age_days=0) is None


def test_due_at_is_issue_time_plus_max_age() -> None:
    assert credential_due_at(issued_at=NOW, max_age_days=90) == NOW + timedelta(days=90)


@pytest.mark.parametrize(
    ("offset_days", "warning_days", "expected"),
    [
        # 距到期还有 30 天，预警窗口 7 天 → 不告警
        (30, 7, None),
        # 正好进入预警窗口边界
        (7, 7, CredentialAlertLevel.WARNING),
        (1, 7, CredentialAlertLevel.WARNING),
        # 已过期
        (0, 7, CredentialAlertLevel.OVERDUE),
        (-5, 7, CredentialAlertLevel.OVERDUE),
    ],
)
def test_classify_expiry_boundaries(
    offset_days: int, warning_days: int, expected: CredentialAlertLevel | None
) -> None:
    due_at = NOW + timedelta(days=offset_days)
    assert classify_credential_expiry(due_at=due_at, warning_days=warning_days, now=NOW) is expected


def test_classify_expiry_without_warning_window_only_reports_overdue() -> None:
    """未配置预警天数时不做预警，但仍必须报告已过期。"""
    assert (
        classify_credential_expiry(due_at=NOW + timedelta(days=10), warning_days=None, now=NOW)
        is None
    )
    assert (
        classify_credential_expiry(due_at=NOW - timedelta(seconds=1), warning_days=None, now=NOW)
        is CredentialAlertLevel.OVERDUE
    )


def test_classify_expiry_requires_due_at() -> None:
    assert classify_credential_expiry(due_at=None, warning_days=7, now=NOW) is None


# ------------------------------------------------------------------ 冷却窗口
def test_cooldown_is_configured_per_level() -> None:
    """逾期与预警的冷却分开配置，避免逾期被预警的节奏掩盖。"""
    assert cooldown_for(
        CredentialAlertLevel.WARNING, warning_hours=24, overdue_hours=6
    ) == timedelta(hours=24)
    assert cooldown_for(
        CredentialAlertLevel.OVERDUE, warning_hours=24, overdue_hours=6
    ) == timedelta(hours=6)


def test_cooldown_window() -> None:
    cooldown = timedelta(hours=24)
    assert is_in_cooldown(last_opened_at=None, cooldown=cooldown, now=NOW) is False
    assert is_in_cooldown(last_opened_at=NOW - timedelta(hours=1), cooldown=cooldown, now=NOW)
    assert not is_in_cooldown(last_opened_at=NOW - timedelta(hours=25), cooldown=cooldown, now=NOW)


# -------------------------------------------------------------- 回退窗口
def test_rollback_requires_activation() -> None:
    with pytest.raises(DomainError, match="尚未激活"):
        require_rotation_rollback_window_open(
            activated_at=None, deadline=NOW + timedelta(days=7), now=NOW
        )


def test_rollback_requires_deadline() -> None:
    with pytest.raises(DomainError, match="缺少回退截止时间"):
        require_rotation_rollback_window_open(
            activated_at=NOW - timedelta(days=1), deadline=None, now=NOW
        )


def test_rollback_window_open_and_expired() -> None:
    require_rotation_rollback_window_open(
        activated_at=NOW - timedelta(days=1),
        deadline=NOW + timedelta(days=1),
        now=NOW,
    )
    with pytest.raises(DomainError, match="窗口已过"):
        require_rotation_rollback_window_open(
            activated_at=NOW - timedelta(days=10),
            deadline=NOW - timedelta(days=1),
            now=NOW,
        )


# -------------------------------------------------------------- 状态与 SOD
def test_require_status_reports_current_and_allowed() -> None:
    require_status(
        SecretRotationStatus.VALIDATING,
        frozenset({SecretRotationStatus.VALIDATING}),
        action="验证",
    )
    with pytest.raises(
        InvalidStateTransition, match="要求状态为 WAITING_APPROVAL，当前为 VALIDATING"
    ):
        require_status(
            SecretRotationStatus.VALIDATING,
            frozenset({SecretRotationStatus.WAITING_APPROVAL}),
            action="审批轮换申请",
        )


def test_operator_outcome_has_no_retry_flag() -> None:
    """凭据操作刻意不带 retryable：自动重试会把「部分完成」放大成更难收拾的状态。"""
    assert OperationOutcome.success().succeeded is True
    assert OperationOutcome.failure("x").succeeded is False
    assert not hasattr(OperationOutcome.success(), "retryable")


def test_credential_operations_apply_sod01() -> None:
    require_independent_approval(requested_by="a", approver_id="b")
    with pytest.raises(PermissionDenied, match="SOD-01"):
        require_independent_approval(requested_by="a", approver_id="a")


def test_naive_datetimes_from_storage_are_treated_as_utc() -> None:
    """存储层不保证保留 tzinfo（SQLite 实测返回 naive），领域规则必须照样成立。

    这条是**可移植性保证**：换后端不该让到期判定抛 TypeError。
    """
    naive_now = datetime(2026, 10, 16, 12, 0)
    naive_due = datetime(2026, 10, 18, 12, 0)
    assert (
        classify_credential_expiry(due_at=naive_due, warning_days=7, now=naive_now)
        is CredentialAlertLevel.WARNING
    )
    # 归一化的方向是「naive → aware UTC」：返回的到期时刻必须无歧义。
    due = credential_due_at(issued_at=naive_now, max_age_days=30)
    assert due == naive_now.replace(tzinfo=UTC) + timedelta(days=30)
    assert due is not None and due.tzinfo is not None
    assert is_in_cooldown(
        last_opened_at=naive_now - timedelta(hours=1),
        cooldown=timedelta(hours=24),
        now=naive_now,
    )
    require_rotation_rollback_window_open(
        activated_at=naive_now - timedelta(days=1),
        deadline=naive_due,
        now=naive_now,
    )


def test_mixed_naive_and_aware_inputs_do_not_raise() -> None:
    """一半 naive 一半 aware 也不能炸：归一化对两侧都生效。"""
    assert (
        classify_credential_expiry(
            due_at=datetime(2026, 10, 18, 12, 0),  # naive
            warning_days=7,
            now=NOW,  # aware
        )
        is CredentialAlertLevel.WARNING
    )
    assert (
        classify_credential_expiry(
            due_at=NOW + timedelta(days=2),  # aware
            warning_days=7,
            now=datetime(2026, 10, 16, 12, 0),  # naive
        )
        is CredentialAlertLevel.WARNING
    )
