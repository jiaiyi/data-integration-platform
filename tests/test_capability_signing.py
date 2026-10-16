"""能力令牌的签发与校验（T-28 / FR-45）。

证据要求：**篡改、过期、跨环境均失败**。除此之外还守住两件更要紧的事：
- Worker 只挂公钥，因此**签不出来**（不是靠约定，是手里没有钥匙）；
- 公钥映射为空时任何验签都失败（「找不到钥匙」绝不能被当成「不需要验签」）。
"""

from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from data_integration_platform.application.harness.capability import (
    BOUND_FACTS,
    CapabilityExpectation,
    CapabilityMismatch,
    issue_capability,
    verify_capability,
)
from data_integration_platform.application.harness.signing import (
    SignatureError,
    signing_payload,
)
from data_integration_platform.config import Settings
from data_integration_platform.domain.harness import RiskLevel, canonical_json
from data_integration_platform.infrastructure.ed25519_signing import (
    Ed25519KeyRingVerifier,
    Ed25519Signer,
    create_capability_signer,
    create_capability_verifier,
    decode_key,
    encode_key,
    generate_keypair,
    parse_public_keys,
)

NOW = datetime(2026, 10, 16, 2, 0, tzinfo=UTC)
TTL = 300
PREPARATION = "66666666-6666-6666-6666-666666666666"
SUBJECT = "22222222-2222-2222-2222-222222222222"
KEY_ID = "harness-local-1"


@pytest.fixture(scope="module")
def keypair() -> tuple[str, str]:
    return generate_keypair()


@pytest.fixture
def signer(keypair: tuple[str, str]) -> Ed25519Signer:
    return Ed25519Signer(key_id=KEY_ID, private_key=keypair[0])


@pytest.fixture
def verifier(keypair: tuple[str, str]) -> Ed25519KeyRingVerifier:
    return Ed25519KeyRingVerifier(parse_public_keys(f"{KEY_ID}:{keypair[1]}"))


def expectation(**overrides: object) -> CapabilityExpectation:
    values: dict[str, object] = {
        "tool_name": "seatunnel.submit_job",
        "tool_version": "2.3.13",
        "subject_id": SUBJECT,
        "environment": "local",
        "preparation_id": PREPARATION,
        "artifact_sha256": "a" * 64,
        "input_fingerprint": "b" * 64,
        "resource_fingerprint": "c" * 64,
        "supervision_contract_sha256": "d" * 64,
        "data_classification_sha256": "e" * 64,
    }
    values.update(overrides)
    return CapabilityExpectation(**values)  # type: ignore[arg-type]


def issue(signer: Ed25519Signer, **overrides: object) -> object:
    values: dict[str, object] = {
        "preparation_id": PREPARATION,
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


# ------------------------------------------------------------------ 正常路径
def test_issued_token_verifies(signer: Ed25519Signer, verifier: Ed25519KeyRingVerifier) -> None:
    token = issue(signer)

    claims = verify_capability(token, expectation=expectation(), verifier=verifier, now=NOW)

    assert claims.preparation_id == PREPARATION
    assert claims.single_use is True
    assert claims.expires_at == NOW + timedelta(seconds=TTL)


def test_token_expires_at_the_configured_ttl(
    signer: Ed25519Signer, verifier: Ed25519KeyRingVerifier
) -> None:
    """TTL 300s 是策略值（FR-45 / T-28）；签出来就应该是这个窗口。"""
    token = issue(signer)

    assert token.claims.expires_at - token.claims.issued_at == timedelta(seconds=300)
    assert token.claims.is_expired(now=NOW + timedelta(seconds=299)) is False
    assert token.claims.is_expired(now=NOW + timedelta(seconds=300)) is True


# ------------------------------------------------------------------ 三条证据
def test_tampered_claims_fail_signature_check(
    signer: Ed25519Signer, verifier: Ed25519KeyRingVerifier
) -> None:
    """改了声明体的任何一位，签名就对不上 —— 这是「审批后替换内容」不可能的结构原因。"""
    token = issue(signer)
    tampered = replace(token, claims=replace(token.claims, artifact_sha256="f" * 64))

    with pytest.raises(SignatureError, match="签名与载荷不匹配"):
        verify_capability(tampered, expectation=expectation(), verifier=verifier, now=NOW)


def test_expired_token_is_rejected(signer: Ed25519Signer, verifier: Ed25519KeyRingVerifier) -> None:
    token = issue(signer)

    with pytest.raises(CapabilityMismatch, match="已过期"):
        verify_capability(
            token,
            expectation=expectation(),
            verifier=verifier,
            now=NOW + timedelta(seconds=TTL + 1),
        )


def test_cross_environment_token_is_rejected(
    signer: Ed25519Signer, verifier: Ed25519KeyRingVerifier
) -> None:
    """跨环境使用：签名没问题，但这份令牌不是给这个环境用的。"""
    token = issue(signer, environment="local")

    with pytest.raises(CapabilityMismatch, match="environment"):
        verify_capability(
            token, expectation=expectation(environment="production"), verifier=verifier, now=NOW
        )


# ------------------------------------------------------------------ 逐项绑定
@pytest.mark.parametrize("field", BOUND_FACTS)
def test_every_bound_fact_is_actually_compared(
    signer: Ed25519Signer, verifier: Ed25519KeyRingVerifier, field: str
) -> None:
    """绑定项逐个改一次都必须被拒 —— 否则「绑定了」只是声明体里多了一个字段。"""
    token = issue(signer)

    with pytest.raises(CapabilityMismatch, match=field):
        verify_capability(
            token,
            expectation=expectation(**{field: "不匹配的值"}),
            verifier=verifier,
            now=NOW,
        )


def test_non_single_use_token_is_rejected(
    signer: Ed25519Signer, verifier: Ed25519KeyRingVerifier
) -> None:
    """声明体说它不是单次的：自己签出来的也要拒 —— 单次是防重放的前提。"""
    token = issue(signer)
    repeated = replace(token, claims=replace(token.claims, single_use=False))
    resigned = replace(
        repeated, signature=signer.sign(signing_payload(repeated.claims.canonical_payload()))
    )

    with pytest.raises(CapabilityMismatch, match="不是单次的"):
        verify_capability(resigned, expectation=expectation(), verifier=verifier, now=NOW)


def test_token_digest_binds_the_signature(signer: Ed25519Signer, keypair: tuple[str, str]) -> None:
    """同一份声明体换一把钥匙签，令牌摘要必须不同 —— 否则重放表分不出两张令牌。"""
    other = Ed25519Signer(key_id="harness-other", private_key=generate_keypair()[0])

    assert issue(signer).token_sha256() != issue(other).token_sha256()


def test_token_digest_is_stable(signer: Ed25519Signer) -> None:
    """Ed25519 是确定性签名，同一份声明签两次得到同一串字节。"""
    fixed = {"token_id": "55555555-5555-5555-5555-555555555555"}

    assert issue(signer, **fixed).token_sha256() == issue(signer, **fixed).token_sha256()


# ------------------------------------------------------------------ 密钥分离
def test_worker_without_private_key_cannot_sign() -> None:
    """执行方只挂公钥，因此**签不出来** —— 这是私钥/公钥分离的实质，不是文档约定。"""
    settings = Settings(
        _env_file=None,
        harness_signing_public_keys=f"{KEY_ID}:{generate_keypair()[1]}",
    )

    signer = create_capability_signer(settings=settings)

    assert signer.key_id == ""
    with pytest.raises(SignatureError, match="未配置签名私钥"):
        signer.sign(b"payload")


def test_missing_public_key_is_a_failure_not_a_bypass() -> None:
    """「找不到钥匙」必须失败关闭：没有公钥映射时任何验签都不通过。"""
    settings = Settings(_env_file=None, harness_signing_public_keys="")
    verifier = create_capability_verifier(settings=settings)
    token = issue(Ed25519Signer(key_id=KEY_ID, private_key=generate_keypair()[0]))

    assert verifier.knows(KEY_ID) is False
    with pytest.raises(SignatureError, match="未配置任何公钥"):
        verify_capability(token, expectation=expectation(), verifier=verifier, now=NOW)


def test_unknown_key_id_is_rejected(keypair: tuple[str, str]) -> None:
    """钥匙不在映射里就是验不过，不退化成「不验签」。"""
    verifier = Ed25519KeyRingVerifier(parse_public_keys(f"{KEY_ID}:{keypair[1]}"))
    stranger = Ed25519Signer(key_id="harness-stranger", private_key=generate_keypair()[0])
    token = issue(stranger)

    with pytest.raises(SignatureError, match="未登记的公钥"):
        verify_capability(token, expectation=expectation(), verifier=verifier, now=NOW)


def test_key_rotation_keeps_old_tokens_verifiable() -> None:
    """轮换期间新旧两把公钥都在映射里，旧令牌仍能验签 —— 否则轮换等于让历史全部失效。"""
    old_private, old_public = generate_keypair()
    new_private, new_public = generate_keypair()
    verifier = Ed25519KeyRingVerifier(
        parse_public_keys(f"harness-old:{old_public},harness-new:{new_public}")
    )

    old_token = issue(Ed25519Signer(key_id="harness-old", private_key=old_private))
    new_token = issue(Ed25519Signer(key_id="harness-new", private_key=new_private))

    assert verify_capability(
        old_token, expectation=expectation(), verifier=verifier, now=NOW
    ).single_use
    assert verify_capability(
        new_token, expectation=expectation(), verifier=verifier, now=NOW
    ).single_use


def test_unsupported_algorithm_is_rejected(
    signer: Ed25519Signer, verifier: Ed25519KeyRingVerifier
) -> None:
    token = issue(signer)

    with pytest.raises(SignatureError, match="不支持的签名算法"):
        verify_capability(
            replace(token, algorithm="HS256"),
            expectation=expectation(),
            verifier=verifier,
            now=NOW,
        )


# ------------------------------------------------------------------ 密钥编解码
def test_key_encoding_round_trip() -> None:
    private, public = generate_keypair()

    assert len(decode_key(private, expect=32)) == 32
    assert len(decode_key(public, expect=32)) == 32
    assert encode_key(decode_key(public, expect=32)) == public


def test_malformed_key_is_rejected() -> None:
    with pytest.raises(SignatureError, match="base64url"):
        decode_key("不是 base64!!")
    with pytest.raises(SignatureError, match="长度"):
        decode_key(encode_key(b"\x00" * 16), expect=32)


def test_public_key_map_rejects_bad_entries() -> None:
    with pytest.raises(SignatureError, match="key_id:base64url"):
        parse_public_keys("没有冒号")
    with pytest.raises(SignatureError, match="重复"):
        entry = f"{KEY_ID}:{generate_keypair()[1]}"
        parse_public_keys(f"{entry},{entry}")


def test_signing_payload_is_canonical() -> None:
    """签名对象是规范 JSON 字节：键序无关，换序不影响签名结果。"""
    left = signing_payload({"b": 1, "a": 2})
    right = signing_payload({"a": 2, "b": 1})

    assert left == right == canonical_json({"a": 2, "b": 1}).encode("utf-8")
