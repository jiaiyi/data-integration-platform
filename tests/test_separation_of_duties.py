"""强制职责分离规则（SPEC T-09 / PRD SOD-01 ~ SOD-05）。

这几条是红线：绕过它们，四眼原则与「审计只读」就是文案而不是约束。
"""

import pytest

from data_integration_platform.domain.errors import PermissionDenied
from data_integration_platform.domain.harness import (
    HarnessRole,
    require_auditor_is_read_only,
    require_distinct_slot_holders,
    require_executor_not_approver,
    require_no_conflicting_harness_roles,
    require_no_self_approval,
)

APPLICANT = "11111111-1111-1111-1111-111111111111"
APPROVER = "22222222-2222-2222-2222-222222222222"
SECOND_APPROVER = "33333333-3333-3333-3333-333333333333"


# ------------------------------------------------------------------ SOD-01
def test_sod01_rejects_self_approval() -> None:
    with pytest.raises(PermissionDenied, match="SOD-01"):
        require_no_self_approval(applicant_id=APPLICANT, approver_id=APPLICANT)


def test_sod01_allows_independent_approval() -> None:
    require_no_self_approval(applicant_id=APPLICANT, approver_id=APPROVER)


# ------------------------------------------------------------------ SOD-02
def test_sod02_rejects_one_person_holding_two_slots() -> None:
    with pytest.raises(PermissionDenied, match="SOD-02"):
        require_distinct_slot_holders(
            role_holders={
                HarnessRole.DATA_APPROVER: APPROVER,
                HarnessRole.SECURITY_APPROVER: APPROVER,
            }
        )


def test_sod02_allows_two_distinct_slot_holders() -> None:
    require_distinct_slot_holders(
        role_holders={
            HarnessRole.DATA_APPROVER: APPROVER,
            HarnessRole.SECURITY_APPROVER: SECOND_APPROVER,
        }
    )


def test_sod02_error_message_is_order_independent() -> None:
    """声明顺序不同不应产出不同错误信息，否则审计记录会随调用顺序漂移。"""
    first = {
        HarnessRole.DATA_APPROVER: APPROVER,
        HarnessRole.SECURITY_APPROVER: APPROVER,
    }
    second = {
        HarnessRole.SECURITY_APPROVER: APPROVER,
        HarnessRole.DATA_APPROVER: APPROVER,
    }
    with pytest.raises(PermissionDenied) as first_error:
        require_distinct_slot_holders(role_holders=first)
    with pytest.raises(PermissionDenied) as second_error:
        require_distinct_slot_holders(role_holders=second)
    assert str(first_error.value) == str(second_error.value)


def test_sod02_grant_time_rejects_conflicting_slots() -> None:
    """PRD §9.1 FR-04：把同一人映射到数据槽与安全槽，必须在授予期就被拒绝。"""
    with pytest.raises(PermissionDenied, match="SOD-02"):
        require_no_conflicting_harness_roles(
            granted=frozenset({HarnessRole.DATA_APPROVER}),
            additional=HarnessRole.SECURITY_APPROVER,
        )
    with pytest.raises(PermissionDenied, match="SOD-02"):
        require_no_conflicting_harness_roles(
            granted=frozenset({HarnessRole.SECURITY_APPROVER}),
            additional=HarnessRole.DATA_APPROVER,
        )


@pytest.mark.parametrize(
    ("granted", "additional"),
    [
        (frozenset(), HarnessRole.DATA_APPROVER),
        (frozenset(), HarnessRole.SECURITY_APPROVER),
        (frozenset({HarnessRole.OPERATOR}), HarnessRole.DATA_APPROVER),
        (frozenset({HarnessRole.DATA_ENGINEER}), HarnessRole.SECURITY_APPROVER),
        (frozenset({HarnessRole.DATA_APPROVER}), HarnessRole.OPERATOR),
        (frozenset({HarnessRole.DATA_APPROVER}), HarnessRole.DATA_APPROVER),
    ],
)
def test_sod02_grant_time_allows_non_conflicting_combinations(
    granted: frozenset[HarnessRole], additional: HarnessRole
) -> None:
    require_no_conflicting_harness_roles(granted=granted, additional=additional)


# ------------------------------------------------------------------ SOD-03
def test_sod03_rejects_approver_committing_own_approval() -> None:
    with pytest.raises(PermissionDenied, match="SOD-03"):
        require_executor_not_approver(
            executor_id=APPROVER, approver_ids=[APPROVER, SECOND_APPROVER]
        )


def test_sod03_allows_independent_committer() -> None:
    require_executor_not_approver(
        executor_id="44444444-4444-4444-4444-444444444444",
        approver_ids=[APPROVER, SECOND_APPROVER],
    )


# ------------------------------------------------------------------ SOD-05
def test_sod05_auditor_cannot_write() -> None:
    with pytest.raises(PermissionDenied, match="SOD-05"):
        require_auditor_is_read_only(
            harness_roles=frozenset({HarnessRole.AUDITOR, HarnessRole.OPERATOR})
        )


def test_sod05_allows_non_auditors_to_write() -> None:
    require_auditor_is_read_only(
        harness_roles=frozenset({HarnessRole.DATA_ENGINEER, HarnessRole.OPERATOR})
    )
    require_auditor_is_read_only(harness_roles=frozenset())
