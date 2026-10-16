"""Harness 值对象的指纹稳定性（SPEC T-06）。

证据要求：**指纹对同输入稳定** —— 同一逻辑输入必须产生同一摘要，否则
「批准的内容 = 执行的内容」这条不变量在 Commit 重算指纹时就会随机失效。
"""

from datetime import UTC, datetime, timedelta

import pytest

from data_integration_platform.domain.harness import (
    CapabilityClaims,
    DataSensitivity,
    HarnessRole,
    PolicyDecision,
    ResourceScope,
    RiskLevel,
    ToolEffect,
    ToolIntent,
    ToolIntentOutcome,
    ToolManifest,
    canonical_json,
    canonical_sha256,
)


def scope(**overrides: object) -> ResourceScope:
    values: dict[str, object] = {
        "project_id": "11111111-1111-1111-1111-111111111111",
        "objects": ("ods.orders", "ods.users"),
        "connection_ids": ("conn-b", "conn-a"),
    }
    values.update(overrides)
    return ResourceScope(**values)  # type: ignore[arg-type]


def intent(**overrides: object) -> ToolIntent:
    values: dict[str, object] = {
        "tool_name": "seatunnel.submit_job",
        "tool_version": "2.3.13",
        "effect": ToolEffect.WRITE,
        "subject_id": "22222222-2222-2222-2222-222222222222",
        "scope": scope(),
        "environment": "local",
        "data_sensitivity": DataSensitivity.CONFIDENTIAL,
    }
    values.update(overrides)
    return ToolIntent(**values)  # type: ignore[arg-type]


def test_canonical_json_is_key_order_independent() -> None:
    assert canonical_json({"b": 1, "a": 2}) == canonical_json({"a": 2, "b": 1})
    assert canonical_json({"a": 2, "b": 1}) == '{"a":2,"b":1}'


def test_canonical_sha256_rejects_unstable_input() -> None:
    """NaN 序列化结果依赖实现细节，必须直接拒绝而不是产出不确定摘要。"""
    with pytest.raises(ValueError, match="Out of range float values"):
        canonical_sha256({"ratio": float("nan")})


def test_resource_scope_fingerprint_ignores_declaration_order() -> None:
    assert scope().fingerprint() == scope(objects=("ods.users", "ods.orders")).fingerprint()


def test_resource_scope_fingerprint_tracks_project() -> None:
    other = scope(project_id="33333333-3333-3333-3333-333333333333")
    assert scope().fingerprint() != other.fingerprint()


def test_tool_intent_fingerprint_is_stable_for_same_input() -> None:
    assert intent().fingerprint() == intent().fingerprint()


@pytest.mark.parametrize(
    "overrides",
    [
        {"subject_id": "44444444-4444-4444-4444-444444444444"},
        {"environment": "production"},
        {"effect": ToolEffect.DESTRUCTIVE},
        {"data_sensitivity": DataSensitivity.SECRET},
        {"tool_version": "2.3.14"},
    ],
)
def test_tool_intent_fingerprint_changes_with_every_bound_field(
    overrides: dict[str, object],
) -> None:
    """任何被绑定的字段变化都必须改变指纹，否则篡改可以绕过比对。"""
    assert intent().fingerprint() != intent(**overrides).fingerprint()


def test_policy_decision_fingerprint_ignores_role_declaration_order() -> None:
    roles = (HarnessRole.DATA_APPROVER, HarnessRole.SECURITY_APPROVER)
    reversed_roles = tuple(reversed(roles))
    first = PolicyDecision(
        outcome=ToolIntentOutcome.REQUIRE_APPROVAL,
        risk_level=RiskLevel.P0,
        required_roles=roles,
        required_approvals=2,
        policy_bundle_version="harness-v1-20261016",
        matched_rules=("DESTRUCTIVE_WRITE", "CROSS_PROJECT"),
    )
    second = PolicyDecision(
        outcome=ToolIntentOutcome.REQUIRE_APPROVAL,
        risk_level=RiskLevel.P0,
        required_roles=reversed_roles,
        required_approvals=2,
        policy_bundle_version="harness-v1-20261016",
        matched_rules=("CROSS_PROJECT", "DESTRUCTIVE_WRITE"),
    )
    assert first.fingerprint() == second.fingerprint()


def claims(**overrides: object) -> CapabilityClaims:
    issued = datetime(2026, 10, 16, 2, 0, tzinfo=UTC)
    values: dict[str, object] = {
        "token_id": "55555555-5555-5555-5555-555555555555",
        "preparation_id": "66666666-6666-6666-6666-666666666666",
        "tool_name": "seatunnel.submit_job",
        "tool_version": "2.3.13",
        "subject_id": "22222222-2222-2222-2222-222222222222",
        "environment": "local",
        "artifact_sha256": "a" * 64,
        "input_fingerprint": "b" * 64,
        "resource_fingerprint": "c" * 64,
        "supervision_contract_sha256": "d" * 64,
        "data_classification_sha256": "e" * 64,
        "policy_bundle_version": "harness-v1-20261016",
        "risk_level": RiskLevel.P0,
        "issued_at": issued,
        "expires_at": issued + timedelta(seconds=300),
    }
    values.update(overrides)
    return CapabilityClaims(**values)  # type: ignore[arg-type]


def test_capability_claims_are_single_use_and_expiring() -> None:
    token = claims()
    assert token.single_use is True
    assert token.is_expired(now=token.expires_at) is True
    assert token.is_expired(now=token.expires_at - timedelta(seconds=1)) is False


def test_capability_claims_bind_artifact_and_supervision_contract() -> None:
    """Capability 必须同时绑定制品摘要与监督契约摘要，两者任一变化即失效。"""
    baseline = claims().fingerprint()
    assert baseline != claims(artifact_sha256="f" * 64).fingerprint()
    assert baseline != claims(supervision_contract_sha256="f" * 64).fingerprint()


def test_tool_manifest_fingerprint_is_stable_and_complete() -> None:
    manifest = ToolManifest(
        name="seatunnel.submit_job",
        version="2.3.13",
        effect=ToolEffect.WRITE,
        timeout_seconds=1800,
        max_records=10_000_000,
        max_bytes=10 * 1024**3,
        idempotent=True,
        input_schema={"type": "object", "required": ["preparation_id"]},
        output_schema={"type": "object", "required": ["job_id"]},
        compensation_tool="seatunnel.cancel_job",
    )
    assert manifest.fingerprint() == manifest.fingerprint()
    assert manifest.canonical_payload()["compensation_tool"] == "seatunnel.cancel_job"
