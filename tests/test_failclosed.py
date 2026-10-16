"""失败关闭分级（T-32 / FR-50）。

核心证据：**缺公钥时写操作被拒**。更一般地，五项信任输入里任意一项不是「已验证」，
写入 / 删除 / 外发 / 密钥使用都不得放行；其中**「没提供」记作「验不了」**，
这是失败关闭最容易被绕开的地方 —— 少配一项就被当成不需要那一项。
只读不被拦住，因为 FR-50 点名的是那四类副作用。
"""

import uuid

import pytest

from data_integration_platform.application.harness.failclosed import (
    SIDE_EFFECTING_EFFECTS,
    TrustInput,
    TrustReport,
    TrustStatus,
    TrustVerdict,
    approval_verdict,
    evidence_verdict,
    policy_verdict,
    require_side_effect_allowed,
    secret_verdict,
    signature_verdict,
)
from data_integration_platform.application.harness.ledger import (
    EvidenceAnomaly,
    EvidenceFinding,
    EvidenceVerification,
)
from data_integration_platform.domain.errors import PermissionDenied
from data_integration_platform.domain.harness import ToolEffect
from data_integration_platform.infrastructure.ed25519_signing import (
    Ed25519KeyRingVerifier,
    generate_keypair,
    parse_public_keys,
)

KEY_ID = "harness-local-1"


def verified(**overrides: TrustVerdict) -> TrustReport:
    """一份全都通过的报告；用例只把关心的那一项改坏。"""
    baseline = {source: TrustVerdict(TrustStatus.VERIFIED, "ok") for source in TrustInput}
    baseline.update(overrides)
    return TrustReport(verdicts=baseline)


# ------------------------------------------------------------------ 核心证据
def test_write_is_refused_when_the_public_key_is_missing() -> None:
    """缺公钥 → 写操作被拒（T-32 的具名验收）。"""
    report = verified(
        SIGNATURE=signature_verdict(verifier=Ed25519KeyRingVerifier({}), key_id=KEY_ID)
    )

    with pytest.raises(PermissionDenied, match="公钥映射中没有"):
        require_side_effect_allowed(effect=ToolEffect.WRITE, report=report)


def test_write_passes_when_every_input_is_verified() -> None:
    _, public = generate_keypair()
    verifier = Ed25519KeyRingVerifier(parse_public_keys(f"{KEY_ID}:{public}"))
    report = verified(SIGNATURE=signature_verdict(verifier=verifier, key_id=KEY_ID))

    require_side_effect_allowed(effect=ToolEffect.WRITE, report=report)


# ------------------------------------------------------------------ 五类都不放行
@pytest.mark.parametrize(
    "source",
    [
        TrustInput.POLICY,
        TrustInput.APPROVAL,
        TrustInput.SIGNATURE,
        TrustInput.EVIDENCE,
        TrustInput.SECRET,
    ],
)
@pytest.mark.parametrize(
    "effect",
    sorted(SIDE_EFFECTING_EFFECTS, key=lambda item: item.value),
)
def test_any_unverified_input_blocks_every_side_effect(
    source: TrustInput, effect: ToolEffect
) -> None:
    report = verified(**{source: TrustVerdict(TrustStatus.UNVERIFIABLE, f"{source.value} 验不了")})

    with pytest.raises(PermissionDenied, match=source.value):
        require_side_effect_allowed(effect=effect, report=report)


def test_missing_input_is_treated_as_unverifiable() -> None:
    """**没提供 = 验不了**，不是「不需要」。少配一项就被当成不需要，是失败关闭最典型的失效。"""
    report = TrustReport(verdicts={})

    with pytest.raises(PermissionDenied, match="按不可验证处理"):
        require_side_effect_allowed(effect=ToolEffect.WRITE, report=report)


def test_read_only_is_not_blocked() -> None:
    """只读不产生副作用；把它也拦住，缺公钥时连探查都做不了，而那并不是安全要求。"""
    require_side_effect_allowed(effect=ToolEffect.READ_ONLY, report=TrustReport(verdicts={}))


def test_side_effecting_effects_are_exactly_the_four_fr50_names() -> None:
    assert set(SIDE_EFFECTING_EFFECTS) == {
        ToolEffect.WRITE,
        ToolEffect.DESTRUCTIVE,
        ToolEffect.EXTERNAL_EGRESS,
        ToolEffect.SECRET_USE,
    }


def test_every_blocker_is_reported_not_just_the_first() -> None:
    """一次拒了哪几项要说全 —— 补配置的人不用来回试。"""
    report = TrustReport(
        verdicts={
            TrustInput.POLICY: TrustVerdict(TrustStatus.REJECTED, "版本不一致"),
            TrustInput.SECRET: TrustVerdict(TrustStatus.UNVERIFIABLE, "存储未就绪"),
        }
    )

    with pytest.raises(PermissionDenied, match="SECRET"):
        require_side_effect_allowed(effect=ToolEffect.WRITE, report=report)

    assert len(report.blockers()) == len(TrustInput)


# ------------------------------------------------------------------ 各输入的状态
def test_policy_version_mismatch_is_rejected() -> None:
    verdict = policy_verdict(decision_version="harness-v1", configured_version="harness-v2")

    assert verdict.status is TrustStatus.REJECTED
    assert "不一致" in verdict.detail


def test_policy_version_match_is_verified() -> None:
    verdict = policy_verdict(decision_version="harness-v1", configured_version="harness-v1")

    assert verdict.status is TrustStatus.VERIFIED


def test_evidence_anomaly_is_rejected() -> None:
    verification = EvidenceVerification(
        aggregate_type="execution",
        aggregate_id="run-1",
        verified_events=3,
        findings=(
            EvidenceFinding(
                aggregate_type="execution",
                aggregate_id="run-1",
                sequence=2,
                anomaly=EvidenceAnomaly.BROKEN_CHAIN,
                detail="前驱哈希对不上",
            ),
        ),
    )

    verdict = evidence_verdict(verification)

    assert verdict.status is TrustStatus.REJECTED
    assert "BROKEN_CHAIN" in verdict.detail


def test_intact_evidence_is_verified() -> None:
    verdict = evidence_verdict(
        EvidenceVerification(aggregate_type="execution", aggregate_id="run-1", verified_events=3)
    )

    assert verdict.status is TrustStatus.VERIFIED


def test_unready_secret_provider_is_unverifiable_not_rejected() -> None:
    """区分「验不了」与「验过了不通过」：前者补配置，后者查篡改。"""
    verdict = secret_verdict(provider="fernet-db", ready=False)

    assert verdict.status is TrustStatus.UNVERIFIABLE
    assert "未就绪" in verdict.detail


def test_approval_shortfall_is_unverifiable() -> None:
    verdict = approval_verdict(required_approvals=2, granted_approvals=1, distinct_holders=True)

    assert verdict.status is TrustStatus.UNVERIFIABLE
    assert "审批不足" in verdict.detail


def test_same_person_holding_both_slots_is_rejected() -> None:
    verdict = approval_verdict(required_approvals=2, granted_approvals=2, distinct_holders=False)

    assert verdict.status is TrustStatus.REJECTED
    assert "SOD-02" in verdict.detail


def test_approval_complete_is_verified() -> None:
    verdict = approval_verdict(required_approvals=2, granted_approvals=2, distinct_holders=True)

    assert verdict.status is TrustStatus.VERIFIED


def test_report_is_intact_only_when_all_five_are_verified() -> None:
    assert verified().verified is True
    assert TrustReport(verdicts={}).verified is False


def test_verdict_for_an_explicit_input_is_returned_as_given() -> None:
    report = TrustReport(
        verdicts={TrustInput.EVIDENCE: TrustVerdict(TrustStatus.REJECTED, "链断了")}
    )

    assert report.verdict_of(TrustInput.EVIDENCE).status is TrustStatus.REJECTED
    assert report.verdict_of(TrustInput.POLICY).status is TrustStatus.UNVERIFIABLE


def test_blockers_name_the_input_and_the_reason() -> None:
    report = TrustReport(
        verdicts={TrustInput.SIGNATURE: TrustVerdict(TrustStatus.UNVERIFIABLE, "没有公钥")}
    )

    blockers = report.blockers()

    assert any("SIGNATURE" in item and "没有公钥" in item for item in blockers)


def test_unknown_aggregate_does_not_break_the_report() -> None:
    """报告只依赖传进来的裁决，不自己做查询；传个不存在的聚合也不该炸。"""
    verdict = evidence_verdict(
        EvidenceVerification(
            aggregate_type="execution", aggregate_id=uuid.uuid4().hex, verified_events=0
        )
    )

    assert verdict.status is TrustStatus.VERIFIED
