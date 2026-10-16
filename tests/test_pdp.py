"""策略决策点（T-25 / FR-42）。

PRD 对 FR-42 有**三条具名验收**，逐条在这里钉住：跨项目 → P0 拒绝、
秘密级外发 → P0 拒绝且不可审批放行、破坏性写入 → P0 要求双人审批。
其余用例守的是「没命中就拒绝」「清单与意图对不上就拒绝」这两条失败关闭。
"""

from typing import Any

import pytest

from data_integration_platform.application.harness.pdp import (
    DEFAULT_RULES,
    PolicyBundle,
    RuleVerdict,
    build_policy_bundle,
)
from data_integration_platform.application.harness.tools import ToolSpec, build_tool_registry
from data_integration_platform.domain.errors import DomainError
from data_integration_platform.domain.harness import (
    DataSensitivity,
    HarnessRole,
    ResourceScope,
    RiskLevel,
    ToolEffect,
    ToolIntent,
    ToolIntentOutcome,
)

VERSION = "harness-v1-20261016"
PROJECT = "11111111-1111-1111-1111-111111111111"
OTHER_PROJECT = "99999999-9999-9999-9999-999999999999"
SUBJECT = "22222222-2222-2222-2222-222222222222"
ENGINE_VERSION = "2.3.13"


@pytest.fixture
def bundle() -> PolicyBundle:
    return build_policy_bundle(version=VERSION)


@pytest.fixture
def registry() -> Any:
    return build_tool_registry(seatunnel_version=ENGINE_VERSION)


def spec_for(registry: Any, name: str, version: str = ENGINE_VERSION) -> ToolSpec:
    return registry.resolve(name=name, version=version)


def intent(
    *,
    tool_name: str = "seatunnel.submit_job",
    tool_version: str = ENGINE_VERSION,
    effect: ToolEffect = ToolEffect.WRITE,
    sensitivity: DataSensitivity = DataSensitivity.INTERNAL,
    environment: str = "local",
    project_id: str = PROJECT,
    subject_project_id: str = PROJECT,
) -> ToolIntent:
    return ToolIntent(
        tool_name=tool_name,
        tool_version=tool_version,
        effect=effect,
        subject_id=SUBJECT,
        subject_project_id=subject_project_id,
        scope=ResourceScope(project_id=project_id, objects=("dw.orders_wide",)),
        environment=environment,
        data_sensitivity=sensitivity,
    )


def decide(registry: Any, bundle: PolicyBundle, request: ToolIntent) -> Any:
    return bundle.evaluate(
        request, spec=spec_for(registry, request.tool_name, request.tool_version)
    )


# ------------------------------------------------------------------ PRD 三条验收
def test_fr42_cross_project_is_denied_at_p0(registry: Any, bundle: PolicyBundle) -> None:
    decision = decide(registry, bundle, intent(subject_project_id=OTHER_PROJECT))

    assert decision.outcome is ToolIntentOutcome.DENY
    assert decision.risk_level is RiskLevel.P0
    # 命中的规则全部留痕，裁决取最靠前的那条。
    assert decision.matched_rules[0] == "CROSS_PROJECT"
    assert "跨项目" in decision.reason


def test_fr42_secret_egress_is_denied_and_not_appealable(
    registry: Any, bundle: PolicyBundle
) -> None:
    """秘密级外发是 P0 **拒绝**而不是「要求审批」—— 不可审批放行是这条的要害。"""
    decision = decide(
        registry,
        bundle,
        intent(
            tool_name="alert.deliver_webhook",
            tool_version="1.0.0",
            effect=ToolEffect.EXTERNAL_EGRESS,
            sensitivity=DataSensitivity.SECRET,
        ),
    )

    assert decision.outcome is ToolIntentOutcome.DENY
    assert decision.risk_level is RiskLevel.P0
    assert decision.matched_rules[0] == "SECRET_EGRESS"
    assert decision.required_approvals == 0
    assert "不接受审批放行" in decision.reason


def test_fr42_destructive_write_requires_two_approvals(registry: Any, bundle: PolicyBundle) -> None:
    """破坏性写入双人审批：两个槽位各一人，人数与槽位数必须一致（SOD-02 才有意义）。"""
    decision = decide(
        registry,
        bundle,
        intent(
            tool_name="doris.atomic_swap",
            tool_version="3.0.8",
            effect=ToolEffect.DESTRUCTIVE,
        ),
    )

    assert decision.outcome is ToolIntentOutcome.REQUIRE_APPROVAL
    assert decision.risk_level is RiskLevel.P0
    assert decision.required_approvals == 2
    assert set(decision.required_roles) == {
        HarnessRole.DATA_APPROVER,
        HarnessRole.SECURITY_APPROVER,
    }


# ------------------------------------------------------------------ 失败关闭
def test_uncovered_action_is_denied(registry: Any) -> None:
    """策略包没覆盖的动作必须拒绝：空规则集就是「什么都不放行」。"""
    empty = build_policy_bundle(version=VERSION, rules=())

    decision = decide(registry, empty, intent())

    assert decision.outcome is ToolIntentOutcome.DENY
    assert decision.risk_level is RiskLevel.P0
    assert decision.matched_rules == ()
    assert "失败关闭" in decision.reason


def test_contract_mismatch_is_denied(registry: Any, bundle: PolicyBundle) -> None:
    """意图里的效果与清单对不上，说明调用方拿的不是它以为的那个工具。"""
    lying = intent(effect=ToolEffect.READ_ONLY)

    decision = decide(registry, bundle, lying)

    assert decision.outcome is ToolIntentOutcome.DENY
    assert decision.risk_level is RiskLevel.P0
    assert decision.matched_rules[0] == "CONTRACT_MISMATCH"


def test_unknown_environment_is_denied(registry: Any, bundle: PolicyBundle) -> None:
    decision = decide(registry, bundle, intent(environment="prod-ish"))

    assert decision.outcome is ToolIntentOutcome.DENY
    assert decision.matched_rules[0] == "UNKNOWN_ENVIRONMENT"


def test_severe_rules_win_over_ordinary_ones(registry: Any, bundle: PolicyBundle) -> None:
    """跨项目且是破坏性写入：裁决取最靠前的那条，但两条命中都要留痕。"""
    decision = decide(
        registry,
        bundle,
        intent(
            tool_name="doris.atomic_swap",
            tool_version="3.0.8",
            effect=ToolEffect.DESTRUCTIVE,
            subject_project_id=OTHER_PROJECT,
        ),
    )

    assert decision.outcome is ToolIntentOutcome.DENY
    assert decision.matched_rules[0] == "CROSS_PROJECT"
    assert "DESTRUCTIVE_WRITE" in decision.matched_rules


# ------------------------------------------------------------------ 分级与效果
def test_read_only_is_allowed_without_approval(registry: Any, bundle: PolicyBundle) -> None:
    decision = decide(
        registry,
        bundle,
        intent(
            tool_name="metadata.probe_table",
            tool_version="1.0.0",
            effect=ToolEffect.READ_ONLY,
            sensitivity=DataSensitivity.PUBLIC,
        ),
    )

    assert decision.outcome is ToolIntentOutcome.ALLOW
    assert decision.risk_level is RiskLevel.P3
    assert decision.required_approvals == 0


def test_plain_write_needs_only_the_data_approver(registry: Any, bundle: PolicyBundle) -> None:
    decision = decide(registry, bundle, intent(effect=ToolEffect.WRITE))

    assert decision.outcome is ToolIntentOutcome.REQUIRE_APPROVAL
    assert decision.required_roles == (HarnessRole.DATA_APPROVER,)
    assert decision.required_approvals == 1
    assert decision.risk_level is RiskLevel.P2


def test_secret_use_needs_the_security_approver(registry: Any, bundle: PolicyBundle) -> None:
    decision = decide(
        registry,
        bundle,
        intent(tool_name="secret.use", tool_version="1.0.0", effect=ToolEffect.SECRET_USE),
    )

    assert decision.required_roles == (HarnessRole.SECURITY_APPROVER,)
    assert decision.risk_level is RiskLevel.P1


def test_egress_of_confidential_data_needs_the_security_approver(
    registry: Any, bundle: PolicyBundle
) -> None:
    decision = decide(
        registry,
        bundle,
        intent(
            tool_name="alert.deliver_webhook",
            tool_version="1.0.0",
            effect=ToolEffect.EXTERNAL_EGRESS,
            sensitivity=DataSensitivity.CONFIDENTIAL,
        ),
    )

    assert decision.outcome is ToolIntentOutcome.REQUIRE_APPROVAL
    assert decision.required_roles == (HarnessRole.SECURITY_APPROVER,)
    assert decision.risk_level is RiskLevel.P1


def test_egress_of_public_data_still_needs_approval(registry: Any, bundle: PolicyBundle) -> None:
    """外发一律要审批，只是分级低的走数据审批槽 —— 没有「低分级就直接出门」这条路。"""
    decision = decide(
        registry,
        bundle,
        intent(
            tool_name="alert.deliver_webhook",
            tool_version="1.0.0",
            effect=ToolEffect.EXTERNAL_EGRESS,
            sensitivity=DataSensitivity.PUBLIC,
        ),
    )

    assert decision.outcome is ToolIntentOutcome.REQUIRE_APPROVAL
    assert decision.required_roles == (HarnessRole.DATA_APPROVER,)


def test_unclassified_data_is_treated_conservatively(registry: Any, bundle: PolicyBundle) -> None:
    """分类缺失按 UNKNOWN 保守处理：走安全审批槽，而不是被当成公开数据放过去。"""
    decision = decide(
        registry,
        bundle,
        intent(
            tool_name="alert.deliver_webhook",
            tool_version="1.0.0",
            effect=ToolEffect.EXTERNAL_EGRESS,
            sensitivity=DataSensitivity.UNKNOWN,
        ),
    )

    assert decision.required_roles == (HarnessRole.SECURITY_APPROVER,)
    assert decision.risk_level is RiskLevel.P1


def test_unknown_is_not_secret(registry: Any, bundle: PolicyBundle) -> None:
    """UNKNOWN 走审批而不是硬拒：分类缺失是常态，按秘密处理会让系统直接不可用。"""
    decision = decide(
        registry,
        bundle,
        intent(
            tool_name="alert.deliver_webhook",
            tool_version="1.0.0",
            effect=ToolEffect.EXTERNAL_EGRESS,
            sensitivity=DataSensitivity.UNKNOWN,
        ),
    )

    assert decision.outcome is ToolIntentOutcome.REQUIRE_APPROVAL


# ------------------------------------------------------------------ 决策可追溯
def test_decision_records_the_bundle_version(registry: Any, bundle: PolicyBundle) -> None:
    decision = decide(registry, bundle, intent())

    assert decision.policy_bundle_version == VERSION


def test_decision_is_reproducible(registry: Any, bundle: PolicyBundle) -> None:
    """同一份输入必须给出逐位相同的决策，否则「批准的内容 = 执行的内容」无从核对。"""
    request = intent()

    first = decide(registry, bundle, request)
    second = decide(registry, bundle, request)

    assert first.fingerprint() == second.fingerprint()


def test_rule_ids_are_unique_and_described() -> None:
    ids = [rule.rule_id for rule in DEFAULT_RULES]

    assert len(ids) == len(set(ids))
    assert all(rule.description for rule in DEFAULT_RULES)


def test_duplicate_rule_ids_are_rejected() -> None:
    duplicate = (DEFAULT_RULES[0], DEFAULT_RULES[0])

    with pytest.raises(DomainError, match="规则 id 重复"):
        build_policy_bundle(version=VERSION, rules=duplicate)


def test_bundle_uses_the_configured_version() -> None:
    """版本来自配置：改了版本号必须能被决策记录反映出来。"""
    bundle = build_policy_bundle(version="harness-v2-20261101")

    assert bundle.version == "harness-v2-20261101"


def test_rule_verdict_defaults_are_closed() -> None:
    """`RuleVerdict` 的默认值是「无审批槽、零审批人」；裁决内容必须显式填写。"""
    verdict = RuleVerdict(outcome=ToolIntentOutcome.ALLOW, risk_level=RiskLevel.P3)

    assert verdict.required_roles == ()
    assert verdict.required_approvals == 0
