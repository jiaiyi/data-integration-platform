"""失败关闭分级（T-32 / FR-50）。

把「这次操作依赖的几项信任输入各自是什么状态」收成一份报告，再由**一个判定**决定放不放行。

关键的一条是：**「验不了」与「验过了但不通过」都必须拦住，而「没提供」记作「验不了」**。
把缺失当成通过，是失败关闭最典型的失效方式 —— 少配一把公钥、Secret 提供方没起来，
系统看起来一切正常，实际每条路都开着。

只读操作不在这条判定里（FR-50 点名的是写入 / 删除 / 外发 / 密钥使用）：
把它们一起拦住，会让平台在缺公钥时连探查都做不了，而那并不是安全要求。
"""

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum

from data_integration_platform.application.harness.ledger import EvidenceVerification
from data_integration_platform.application.harness.signing import VerifierPort
from data_integration_platform.domain.errors import PermissionDenied
from data_integration_platform.domain.harness import ToolEffect


class TrustInput(StrEnum):
    """一次副作用所依赖的信任输入；任意一项不成立就不放行。"""

    POLICY = "POLICY"
    APPROVAL = "APPROVAL"
    SIGNATURE = "SIGNATURE"
    EVIDENCE = "EVIDENCE"
    SECRET = "SECRET"


class TrustStatus(StrEnum):
    VERIFIED = "VERIFIED"
    # 「验不了」与「验过了但不通过」刻意分开：前者的处置是补配置，后者是查篡改。
    UNVERIFIABLE = "UNVERIFIABLE"
    REJECTED = "REJECTED"


@dataclass(frozen=True, slots=True)
class TrustVerdict:
    status: TrustStatus
    detail: str


# 会产生副作用的效果；只有这些才受失败关闭约束。
SIDE_EFFECTING_EFFECTS = frozenset(
    {
        ToolEffect.WRITE,
        ToolEffect.DESTRUCTIVE,
        ToolEffect.EXTERNAL_EGRESS,
        ToolEffect.SECRET_USE,
    }
)


@dataclass(frozen=True, slots=True)
class TrustReport:
    """各项信任输入的状态；**没登记的那一项按「验不了」处理**。"""

    verdicts: Mapping[TrustInput, TrustVerdict]

    def verdict_of(self, source: TrustInput) -> TrustVerdict:
        return self.verdicts.get(
            source, TrustVerdict(TrustStatus.UNVERIFIABLE, "该项未提供，按不可验证处理")
        )

    def blockers(self) -> tuple[str, ...]:
        """所有不是 `VERIFIED` 的项，附上原因 —— 「不放行」要能说清是卡在哪一环。"""
        return tuple(
            f"[{source.value}] {verdict.detail}"
            for source in TrustInput
            if (verdict := self.verdict_of(source)).status is not TrustStatus.VERIFIED
        )

    @property
    def verified(self) -> bool:
        return not self.blockers()


def require_side_effect_allowed(*, effect: ToolEffect, report: TrustReport) -> None:
    """放行判定：只读直接过，其余效果必须每一项信任输入都 `VERIFIED`。"""
    if effect not in SIDE_EFFECTING_EFFECTS:
        return
    blockers = report.blockers()
    if blockers:
        raise PermissionDenied("失败关闭：本次操作的信任输入不完整 —— " + "；".join(blockers))


# ------------------------------------------------------------------ 各输入的状态
def policy_verdict(*, decision_version: str, configured_version: str) -> TrustVerdict:
    """决策里的策略包版本必须与当前配置一致。

    不一致说明决策是按另一套规则出的：它可能少了刚加的那条禁令。
    """
    if decision_version != configured_version:
        return TrustVerdict(
            TrustStatus.REJECTED,
            f"策略包版本不一致：决策为 {decision_version!r}，当前为 {configured_version!r}",
        )
    return TrustVerdict(TrustStatus.VERIFIED, f"策略包 {decision_version!r} 一致")


def signature_verdict(*, verifier: VerifierPort, key_id: str) -> TrustVerdict:
    """公钥映射里有没有这把钥匙；没有就**验不了**，而不是「无需验签」。"""
    if not verifier.knows(key_id):
        return TrustVerdict(TrustStatus.UNVERIFIABLE, f"公钥映射中没有 {key_id!r}，无法验证签名")
    return TrustVerdict(TrustStatus.VERIFIED, f"公钥 {key_id!r} 可用")


def evidence_verdict(verification: EvidenceVerification) -> TrustVerdict:
    """证据账本必须完整：链被改过就不该继续放行新的副作用。"""
    if verification.findings:
        anomalies = "、".join(sorted({finding.anomaly.value for finding in verification.findings}))
        return TrustVerdict(
            TrustStatus.REJECTED,
            f"证据账本完整性校验未通过（{verification.aggregate_id}）：{anomalies}",
        )
    return TrustVerdict(
        TrustStatus.VERIFIED, f"证据账本完好（{verification.verified_events} 条事件）"
    )


def secret_verdict(*, provider: str, ready: bool) -> TrustVerdict:
    """凭据存储是否可用；不可用就**验不了**凭据，写操作不得降级放行。"""
    if not ready:
        return TrustVerdict(TrustStatus.UNVERIFIABLE, f"凭据存储 {provider!r} 未就绪，无法读取凭据")
    return TrustVerdict(TrustStatus.VERIFIED, f"凭据存储 {provider!r} 可用")


def approval_verdict(
    *, required_approvals: int, granted_approvals: int, distinct_holders: bool
) -> TrustVerdict:
    """审批证据：人数够、且各槽位不是同一人（SOD-02）。"""
    if not distinct_holders:
        return TrustVerdict(TrustStatus.REJECTED, "审批槽位由同一人担任（SOD-02）")
    if granted_approvals < required_approvals:
        return TrustVerdict(
            TrustStatus.UNVERIFIABLE,
            f"审批不足：需要 {required_approvals} 人，已有 {granted_approvals} 人",
        )
    return TrustVerdict(
        TrustStatus.VERIFIED, f"审批齐备（{granted_approvals}/{required_approvals}）"
    )
