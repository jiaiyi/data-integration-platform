"""能力令牌的签发与校验（T-28 / FR-45、FR-49）。

令牌是**短时、单次、绑定一切**的：工具 + 版本 + 主体 + 环境 + 制品摘要 +
输入/资源指纹 + 监督契约摘要 + 数据分类摘要 + 策略包版本 + 风险等级。
绑定得这么全，是为了让「审批时看到的事实」与「执行时拿到的事实」必须是同一份 ——
任何一项对不上，令牌就不成立。

校验分两段，顺序不能换：

1. **验签**（`SignatureError`）—— 声明体被改过、换了钥匙、算法不对，都在这里失败；
2. **比对**（`CapabilityMismatch`）—— 签名没问题，但这份令牌不是给**这次**操作用的。

把验签放在比对之前是有意的：比对会读声明体里的字段，先确认这串字节确实由持钥方
签发，后面基于它做的任何判断才站得住。
"""

import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from data_integration_platform.application.harness.signing import (
    SIGNATURE_ALGORITHM,
    SignatureError,
    SignerPort,
    VerifierPort,
    signing_payload,
)
from data_integration_platform.domain.harness import (
    CapabilityClaims,
    RiskLevel,
    canonical_sha256,
)


class CapabilityMismatch(Exception):
    """令牌与本次操作对不上。

    与 `SignatureError` 分开：一个是「这串东西不可信」，一个是「可信但不是给你的」。
    两者的处置不同，混在一起会让日志说不清到底发生了什么。
    """


@dataclass(frozen=True, slots=True)
class SignedCapability:
    """签发结果；`token_sha256` 是进库、进重放表的那把钥匙。"""

    claims: CapabilityClaims
    algorithm: str
    key_id: str
    signature: str

    def token_sha256(self) -> str:
        """令牌摘要：签名也计入。

        只对声明体取摘要的话，同一份声明用不同钥匙签两次会得到同一个值，
        重放表就分不出这两张令牌了。
        """
        return canonical_sha256(
            {
                "claims": self.claims.canonical_payload(),
                "algorithm": self.algorithm,
                "key_id": self.key_id,
                "signature": self.signature,
            }
        )

    def canonical_payload(self) -> dict[str, Any]:
        return {
            "token_id": self.claims.token_id,
            "algorithm": self.algorithm,
            "key_id": self.key_id,
            "signature": self.signature,
        }


# 令牌必须逐项对齐的事实；改这里就等于改「令牌绑定了什么」。
BOUND_FACTS: tuple[str, ...] = (
    "tool_name",
    "tool_version",
    "subject_id",
    "environment",
    "preparation_id",
    "artifact_sha256",
    "input_fingerprint",
    "resource_fingerprint",
    "supervision_contract_sha256",
    "data_classification_sha256",
)


@dataclass(frozen=True, slots=True)
class CapabilityExpectation:
    """「这次操作用的是哪张令牌」—— 执行方手里的事实，逐项与声明体比对。"""

    tool_name: str
    tool_version: str
    subject_id: str
    environment: str
    preparation_id: str
    artifact_sha256: str
    input_fingerprint: str
    resource_fingerprint: str
    supervision_contract_sha256: str
    data_classification_sha256: str


def claims_from_payload(payload: Mapping[str, Any]) -> CapabilityClaims:
    """把落库的声明体还原成值对象。

    `claims` 列存的是 `canonical_payload()`，里面时间已是 ISO 字符串、风险等级已是字符串；
    直接 `CapabilityClaims(**payload)` 会得到一个「看起来对、但 `.value` 会炸」的对象 ——
    枚举与时间必须显式还原。
    """
    return CapabilityClaims(
        token_id=str(payload["token_id"]),
        preparation_id=str(payload["preparation_id"]),
        tool_name=str(payload["tool_name"]),
        tool_version=str(payload["tool_version"]),
        subject_id=str(payload["subject_id"]),
        environment=str(payload["environment"]),
        artifact_sha256=str(payload["artifact_sha256"]),
        input_fingerprint=str(payload["input_fingerprint"]),
        resource_fingerprint=str(payload["resource_fingerprint"]),
        supervision_contract_sha256=str(payload["supervision_contract_sha256"]),
        data_classification_sha256=str(payload["data_classification_sha256"]),
        policy_bundle_version=str(payload["policy_bundle_version"]),
        risk_level=RiskLevel(str(payload["risk_level"])),
        issued_at=datetime.fromisoformat(str(payload["issued_at"])),
        expires_at=datetime.fromisoformat(str(payload["expires_at"])),
        single_use=bool(payload["single_use"]),
    )


def token_from_grant(
    *,
    claims_payload: Mapping[str, Any],
    algorithm: str,
    key_id: str,
    signature: str,
) -> SignedCapability:
    """按 `harness_capability_grants` 的列还原一张令牌，供执行方独立验签。"""
    return SignedCapability(
        claims=claims_from_payload(claims_payload),
        algorithm=algorithm,
        key_id=key_id,
        signature=signature,
    )


def issue_capability(
    *,
    preparation_id: str,
    tool_name: str,
    tool_version: str,
    subject_id: str,
    environment: str,
    artifact_sha256: str,
    input_fingerprint: str,
    resource_fingerprint: str,
    supervision_contract_sha256: str,
    data_classification_sha256: str,
    policy_bundle_version: str,
    risk_level: RiskLevel,
    ttl_seconds: int,
    signer: SignerPort,
    now: datetime | None = None,
    token_id: str | None = None,
) -> SignedCapability:
    """签发一张单次令牌。

    `token_id` 与 `now` 可注入，好让测试在固定时刻上验过期；生产留空。
    """
    issued_at = now if now is not None else datetime.now(UTC)
    claims = CapabilityClaims(
        token_id=token_id or str(uuid.uuid4()),
        preparation_id=preparation_id,
        tool_name=tool_name,
        tool_version=tool_version,
        subject_id=subject_id,
        environment=environment,
        artifact_sha256=artifact_sha256,
        input_fingerprint=input_fingerprint,
        resource_fingerprint=resource_fingerprint,
        supervision_contract_sha256=supervision_contract_sha256,
        data_classification_sha256=data_classification_sha256,
        policy_bundle_version=policy_bundle_version,
        risk_level=risk_level,
        issued_at=issued_at,
        expires_at=issued_at + timedelta(seconds=ttl_seconds),
        single_use=True,
    )
    return SignedCapability(
        claims=claims,
        algorithm=SIGNATURE_ALGORITHM,
        key_id=signer.key_id,
        signature=signer.sign(signing_payload(claims.canonical_payload())),
    )


def verify_capability(
    token: SignedCapability,
    *,
    expectation: CapabilityExpectation,
    verifier: VerifierPort,
    now: datetime | None = None,
) -> CapabilityClaims:
    """验签 + 逐项比对；任何一项不成立都抛出，不做「部分通过」的降级。"""
    if token.algorithm != SIGNATURE_ALGORITHM:
        raise SignatureError(f"不支持的签名算法：{token.algorithm!r}")
    verifier.verify(
        signing_payload(token.claims.canonical_payload()),
        signature=token.signature,
        key_id=token.key_id,
    )

    moment = now if now is not None else datetime.now(UTC)
    claims = token.claims
    if claims.is_expired(now=moment):
        raise CapabilityMismatch(
            f"能力令牌已过期（{claims.expires_at.isoformat()} 到期，当前 {moment.isoformat()}）"
        )
    if not claims.single_use:
        # 单次令牌是防重放的前提；声明体说它不是单次的令牌不该被放行。
        raise CapabilityMismatch("能力令牌不是单次的，拒绝执行")

    mismatched = _mismatched_facts(claims, expectation)
    if mismatched:
        raise CapabilityMismatch("能力令牌与本次操作不匹配：" + "、".join(mismatched))
    return claims


def _mismatched_facts(claims: CapabilityClaims, expectation: CapabilityExpectation) -> list[str]:
    """逐项比对绑定事实。

    绑定项**只在这一处列举**：两处各写一遍，迟早会出现「新增了一项绑定，
    但只加在声明体那一侧」，于是令牌看起来绑了、其实根本没比。
    """
    return sorted(
        f"{name}={getattr(claims, name)!r}"
        for name in BOUND_FACTS
        if getattr(claims, name) != getattr(expectation, name)
    )
