"""策略决策点（T-25 / FR-42）。

**纯计算**：给定一次 `ToolIntent` 与它命中的工具清单，输出
「允许 / 拒绝 / 要求审批 / 延迟」+ P0~P3 + 所需职责槽 + 审批人数（SPEC §8.2：
PDP 不调模型、不做 I/O，因此可以在 20ms 的非 LLM 预算里跑完）。

规则表**按优先级排列，先命中先裁决**，同时记录所有命中的规则 id ——
「决策记录策略包版本与规则命中证据」要的是可追溯，所以命中的都留痕，
裁决取最靠前的那条。

三条不变量：

1. **没命中任何规则 = 拒绝**（默认拒绝）。策略包不认识的动作不放行 ——
   这是 T-32 失败关闭在 PDP 侧的那一半。
2. **清单与意图对不上 = 拒绝**。名字、版本、效果三者任一不一致，
   说明调用方拿的不是它以为的那个工具，继续判下去没有意义。
3. **环境不认识 = 拒绝**。环境会随能力令牌一起绑定（T-28），
   在这里先挡住拼错的环境名，免得它一路走到执行。

**策略包的「签名」目前只做到版本化**：规则集在代码里，自签自验证明不了什么。
真要能热更与回滚，得把规则集外置成清单文件再对文件签名；在需要热更之前，
决策里记录的 `policy_bundle_version` + 命中规则就是可追溯的那部分。
"""

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass

from data_integration_platform.application.harness.tools import ToolSpec
from data_integration_platform.domain.errors import DomainError
from data_integration_platform.domain.harness import (
    DataSensitivity,
    HarnessRole,
    PolicyDecision,
    RiskLevel,
    ToolEffect,
    ToolIntent,
    ToolIntentOutcome,
    ToolManifest,
)

# 环境取值；生产口径与 `config.PRODUCTION_ENVIRONMENTS` 一致。
PRODUCTION_ENVIRONMENTS = frozenset({"prod", "production"})
KNOWN_ENVIRONMENTS = frozenset({"local", "dev", "staging"}) | PRODUCTION_ENVIRONMENTS

# 数据分级的保守序；`UNKNOWN` 按**受限**处理 —— 见 `_conservative_rank`。
_SENSITIVITY_RANK: Mapping[DataSensitivity, int] = {
    DataSensitivity.PUBLIC: 0,
    DataSensitivity.INTERNAL: 1,
    DataSensitivity.CONFIDENTIAL: 2,
    DataSensitivity.RESTRICTED: 3,
    DataSensitivity.SECRET: 4,
}
_CONFIDENTIAL_RANK = _SENSITIVITY_RANK[DataSensitivity.CONFIDENTIAL]
_SECRET_RANK = _SENSITIVITY_RANK[DataSensitivity.SECRET]


@dataclass(frozen=True, slots=True)
class RuleVerdict:
    """规则命中后的裁决内容。"""

    outcome: ToolIntentOutcome
    risk_level: RiskLevel
    required_roles: tuple[HarnessRole, ...] = ()
    required_approvals: int = 0
    reason: str = ""


@dataclass(frozen=True, slots=True)
class PolicyRule:
    """一条策略规则：命中则给出裁决，不命中返回 `None`。

    `rule_id` 会进决策记录，规则**只允许新增或改判定语义并同时改 id** ——
    复用旧 id 表示不同语义，会让历史决策变得无法解释。
    """

    rule_id: str
    description: str
    matches_and_decides: Callable[[ToolIntent, ToolManifest], RuleVerdict | None]


def _conservative_rank(sensitivity: DataSensitivity) -> int:
    """把分级折算成保守序位。

    `UNKNOWN` 取**受限**（第二高）而不是秘密：分类缺失是常态（分类器本身也是
    决策输入之一），按秘密处理会让任何未分类数据都彻底出不了门，系统直接不可用；
    取受限则强制走安全审批槽 —— 既不静默放行，也不把「还没分类」当成「不能干活」。
    """
    if sensitivity is DataSensitivity.UNKNOWN:
        return _SENSITIVITY_RANK[DataSensitivity.RESTRICTED]
    return _SENSITIVITY_RANK[sensitivity]


# ------------------------------------------------------------------ 规则表
def _contract_mismatch(intent: ToolIntent, manifest: ToolManifest) -> RuleVerdict | None:
    if (
        intent.tool_name != manifest.name
        or intent.tool_version != manifest.version
        or intent.effect is not manifest.effect
    ):
        return RuleVerdict(
            outcome=ToolIntentOutcome.DENY,
            risk_level=RiskLevel.P0,
            reason=(
                f"意图与工具清单不一致：意图为 {intent.tool_name}@{intent.tool_version}/"
                f"{intent.effect.value}，清单为 {manifest.name}@{manifest.version}/"
                f"{manifest.effect.value}"
            ),
        )
    return None


def _unknown_environment(intent: ToolIntent, manifest: ToolManifest) -> RuleVerdict | None:
    if intent.environment not in KNOWN_ENVIRONMENTS:
        return RuleVerdict(
            outcome=ToolIntentOutcome.DENY,
            risk_level=RiskLevel.P0,
            reason=f"环境未登记：{intent.environment!r}",
        )
    return None


def _cross_project(intent: ToolIntent, manifest: ToolManifest) -> RuleVerdict | None:
    """SPEC §8.1：`ResourceScope.project_id` 与请求主体项目不一致即 P0 阻断。"""
    if intent.scope.project_id != intent.subject_project_id:
        return RuleVerdict(
            outcome=ToolIntentOutcome.DENY,
            risk_level=RiskLevel.P0,
            reason=(
                f"跨项目访问：主体属于项目 {intent.subject_project_id}，"
                f"意图触及项目 {intent.scope.project_id}"
            ),
        )
    return None


def _secret_egress(intent: ToolIntent, manifest: ToolManifest) -> RuleVerdict | None:
    """秘密级数据外发：**不可审批放行**，直接拒绝（PRD FR-42 验收）。"""
    if (
        intent.effect is ToolEffect.EXTERNAL_EGRESS
        and _conservative_rank(intent.data_sensitivity) >= _SECRET_RANK
    ):
        return RuleVerdict(
            outcome=ToolIntentOutcome.DENY,
            risk_level=RiskLevel.P0,
            reason="秘密级数据不得外发；该判定不接受审批放行",
        )
    return None


def _sensitive_egress(intent: ToolIntent, manifest: ToolManifest) -> RuleVerdict | None:
    """其余外发：分级越高越严。受限及以上要安全审批槽，公开/内部只需数据审批槽。"""
    if intent.effect is not ToolEffect.EXTERNAL_EGRESS:
        return None
    sensitive = (
        _conservative_rank(intent.data_sensitivity)
        >= _SENSITIVITY_RANK[DataSensitivity.CONFIDENTIAL]
    )
    return RuleVerdict(
        outcome=ToolIntentOutcome.REQUIRE_APPROVAL,
        risk_level=RiskLevel.P1 if sensitive else RiskLevel.P2,
        required_roles=(
            (HarnessRole.SECURITY_APPROVER,) if sensitive else (HarnessRole.DATA_APPROVER,)
        ),
        required_approvals=1,
        reason=f"数据外发（分级 {intent.data_sensitivity.value}）需审批",
    )


def _destructive_write(intent: ToolIntent, manifest: ToolManifest) -> RuleVerdict | None:
    """破坏性写入：**双人审批**且两个槽位都要（PRD FR-42 验收；SOD-02 保证不同人）。"""
    if intent.effect is not ToolEffect.DESTRUCTIVE:
        return None
    return RuleVerdict(
        outcome=ToolIntentOutcome.REQUIRE_APPROVAL,
        risk_level=RiskLevel.P0,
        required_roles=(HarnessRole.DATA_APPROVER, HarnessRole.SECURITY_APPROVER),
        required_approvals=2,
        reason="破坏性写入需要数据审批人与安全审批人各自具名批准",
    )


def _secret_use(intent: ToolIntent, manifest: ToolManifest) -> RuleVerdict | None:
    if intent.effect is not ToolEffect.SECRET_USE:
        return None
    return RuleVerdict(
        outcome=ToolIntentOutcome.REQUIRE_APPROVAL,
        risk_level=RiskLevel.P1,
        required_roles=(HarnessRole.SECURITY_APPROVER,),
        required_approvals=1,
        reason="使用凭据需安全审批人具名批准",
    )


def _plain_write(intent: ToolIntent, manifest: ToolManifest) -> RuleVerdict | None:
    if intent.effect is not ToolEffect.WRITE:
        return None
    return RuleVerdict(
        outcome=ToolIntentOutcome.REQUIRE_APPROVAL,
        risk_level=RiskLevel.P2,
        required_roles=(HarnessRole.DATA_APPROVER,),
        required_approvals=1,
        reason="写入需数据审批人具名批准",
    )


def _read_only(intent: ToolIntent, manifest: ToolManifest) -> RuleVerdict | None:
    if intent.effect is not ToolEffect.READ_ONLY:
        return None
    return RuleVerdict(
        outcome=ToolIntentOutcome.ALLOW,
        risk_level=RiskLevel.P3,
        reason="只读操作无需审批",
    )


DEFAULT_RULES: tuple[PolicyRule, ...] = (
    PolicyRule("CONTRACT_MISMATCH", "意图与工具清单不一致", _contract_mismatch),
    PolicyRule("UNKNOWN_ENVIRONMENT", "环境未登记", _unknown_environment),
    PolicyRule("CROSS_PROJECT", "跨项目访问", _cross_project),
    PolicyRule("SECRET_EGRESS", "秘密级数据外发", _secret_egress),
    PolicyRule("SENSITIVE_EGRESS", "数据外发按分级收紧", _sensitive_egress),
    PolicyRule("DESTRUCTIVE_WRITE", "破坏性写入双人审批", _destructive_write),
    PolicyRule("SECRET_USE", "凭据使用需安全审批槽", _secret_use),
    PolicyRule("PLAIN_WRITE", "普通写入需数据审批槽", _plain_write),
    PolicyRule("READ_ONLY", "只读放行", _read_only),
)

# 没命中任何规则时的兜底：策略包不认识的动作不放行。
_FALLBACK_REASON = "策略包未覆盖该动作，按失败关闭拒绝"


@dataclass(frozen=True, slots=True)
class PolicyBundle:
    """版本化规则集。版本随决策一起留存，用于回答「当时按哪套规则判的」。"""

    version: str
    rules: Sequence[PolicyRule]

    def evaluate(self, intent: ToolIntent, *, spec: ToolSpec) -> PolicyDecision:
        """按优先级裁决；记录全部命中规则，裁决取最靠前的那条。"""
        manifest = spec.manifest
        verdicts = [
            (rule.rule_id, verdict)
            for rule in self.rules
            if (verdict := rule.matches_and_decides(intent, manifest)) is not None
        ]
        matched = tuple(rule_id for rule_id, _ in verdicts)
        if not verdicts:
            return PolicyDecision(
                outcome=ToolIntentOutcome.DENY,
                risk_level=RiskLevel.P0,
                required_roles=(),
                required_approvals=0,
                policy_bundle_version=self.version,
                matched_rules=(),
                reason=_FALLBACK_REASON,
            )
        _, verdict = verdicts[0]
        return PolicyDecision(
            outcome=verdict.outcome,
            risk_level=verdict.risk_level,
            required_roles=verdict.required_roles,
            required_approvals=verdict.required_approvals,
            policy_bundle_version=self.version,
            matched_rules=matched,
            reason=verdict.reason,
        )


def build_policy_bundle(*, version: str, rules: Sequence[PolicyRule] | None = None) -> PolicyBundle:
    """构造策略包；`rules` 只在测试里覆盖，生产用默认规则表。"""
    resolved = tuple(rules) if rules is not None else DEFAULT_RULES
    _require_unique_rule_ids(resolved)
    return PolicyBundle(version=version, rules=resolved)


def _require_unique_rule_ids(rules: Sequence[PolicyRule]) -> None:
    """规则 id 必须唯一：重复 id 会让决策记录里的命中证据指代不明。"""
    seen: set[str] = set()
    for rule in rules:
        if rule.rule_id in seen:
            raise DomainError(f"策略规则 id 重复：{rule.rule_id}")
        seen.add(rule.rule_id)
