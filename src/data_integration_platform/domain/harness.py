"""Harness 安全内核的领域词表与值对象（SPEC T-06）。

这里只放**纯数据与纯规则**：不导入框架、不访问数据库、不做 I/O。所有指纹都基于
规范 JSON 序列化（键排序 + 固定分隔符 + 集合排序），保证同一逻辑输入产生同一摘要。
"""

import hashlib
import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any

from data_integration_platform.domain.errors import PermissionDenied


# ------------------------------------------------------------------ 词表
class HarnessRole(StrEnum):
    """Harness 职责槽：决定「能批准/执行什么动作」，与 ProjectRole 相互独立。"""

    DATA_ENGINEER = "DATA_ENGINEER"
    DATA_APPROVER = "DATA_APPROVER"
    SECURITY_APPROVER = "SECURITY_APPROVER"
    OPERATOR = "OPERATOR"
    AUDITOR = "AUDITOR"


class ToolEffect(StrEnum):
    READ_ONLY = "READ_ONLY"
    WRITE = "WRITE"
    DESTRUCTIVE = "DESTRUCTIVE"
    EXTERNAL_EGRESS = "EXTERNAL_EGRESS"
    SECRET_USE = "SECRET_USE"


class ToolIntentOutcome(StrEnum):
    ALLOW = "ALLOW"
    DENY = "DENY"
    REQUIRE_APPROVAL = "REQUIRE_APPROVAL"
    DEFER = "DEFER"


class RiskLevel(StrEnum):
    P0 = "P0"
    P1 = "P1"
    P2 = "P2"
    P3 = "P3"


class DataSensitivity(StrEnum):
    PUBLIC = "PUBLIC"
    INTERNAL = "INTERNAL"
    CONFIDENTIAL = "CONFIDENTIAL"
    RESTRICTED = "RESTRICTED"
    SECRET = "SECRET"
    UNKNOWN = "UNKNOWN"


class ExecutionPreparationStatus(StrEnum):
    DENIED = "DENIED"
    WAITING_APPROVAL = "WAITING_APPROVAL"
    READY = "READY"
    REJECTED = "REJECTED"
    EXPIRED = "EXPIRED"
    COMMITTED = "COMMITTED"


class HarnessApprovalValue(StrEnum):
    APPROVE = "APPROVE"
    REJECT = "REJECT"


class HarnessOutboxStatus(StrEnum):
    PENDING = "PENDING"
    DISPATCHING = "DISPATCHING"
    DISPATCHED = "DISPATCHED"
    FAILED = "FAILED"


# -------------------------------------------------------------- 规范摘要
def canonical_json(payload: Mapping[str, Any]) -> str:
    """把映射序列化为稳定文本：键排序、无多余空白、不转义非 ASCII。"""
    return json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )


def canonical_sha256(payload: Mapping[str, Any]) -> str:
    """计算规范 JSON 的 SHA256，作为输入/资源/制品摘要的统一口径。"""
    return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()


# -------------------------------------------------------------- 值对象
@dataclass(frozen=True, slots=True)
class ResourceScope:
    """一次操作可触及的资源范围；跨项目访问由 PDP 按 project_id 直接判为 P0 拒绝。"""

    project_id: str
    objects: tuple[str, ...] = ()
    connection_ids: tuple[str, ...] = ()

    def canonical_payload(self) -> dict[str, Any]:
        return {
            "project_id": self.project_id,
            "objects": sorted(self.objects),
            "connection_ids": sorted(self.connection_ids),
        }

    def fingerprint(self) -> str:
        return canonical_sha256(self.canonical_payload())


@dataclass(frozen=True, slots=True)
class ToolIntent:
    """一次副作用意图的规范描述，是 PDP 的唯一输入。

    `subject_project_id` 与 `scope.project_id` **必须并列出现**：只凭资源所属项目
    无法判断「这是不是跨项目访问」——跨项目正是两者的差集（SPEC §8.1）。
    """

    tool_name: str
    tool_version: str
    effect: ToolEffect
    subject_id: str
    subject_project_id: str
    scope: ResourceScope
    environment: str
    data_sensitivity: DataSensitivity = DataSensitivity.UNKNOWN

    def canonical_payload(self) -> dict[str, Any]:
        return {
            "tool_name": self.tool_name,
            "tool_version": self.tool_version,
            "effect": self.effect.value,
            "subject_id": self.subject_id,
            "subject_project_id": self.subject_project_id,
            "environment": self.environment,
            "data_sensitivity": self.data_sensitivity.value,
            "scope": self.scope.canonical_payload(),
        }

    def fingerprint(self) -> str:
        return canonical_sha256(self.canonical_payload())


@dataclass(frozen=True, slots=True)
class PolicyDecision:
    """PDP 裁决快照；策略包版本与规则命中随决策一并留存以便追溯。"""

    outcome: ToolIntentOutcome
    risk_level: RiskLevel
    required_roles: tuple[HarnessRole, ...]
    required_approvals: int
    policy_bundle_version: str
    matched_rules: tuple[str, ...] = ()
    reason: str = ""

    def canonical_payload(self) -> dict[str, Any]:
        return {
            "outcome": self.outcome.value,
            "risk_level": self.risk_level.value,
            "required_roles": sorted(role.value for role in self.required_roles),
            "required_approvals": self.required_approvals,
            "policy_bundle_version": self.policy_bundle_version,
            "matched_rules": sorted(self.matched_rules),
            "reason": self.reason,
        }

    def fingerprint(self) -> str:
        return canonical_sha256(self.canonical_payload())


@dataclass(frozen=True, slots=True)
class CapabilityClaims:
    """单次能力令牌的声明体；签发时对 `canonical_payload()` 做 Ed25519 分离签名。

    绑定工具、主体、环境与三类指纹，使篡改、过期、跨环境与重复使用都在结构上失败。
    """

    token_id: str
    preparation_id: str
    tool_name: str
    tool_version: str
    subject_id: str
    environment: str
    artifact_sha256: str
    input_fingerprint: str
    resource_fingerprint: str
    supervision_contract_sha256: str
    data_classification_sha256: str
    policy_bundle_version: str
    risk_level: RiskLevel
    issued_at: datetime
    expires_at: datetime
    single_use: bool = True

    def canonical_payload(self) -> dict[str, Any]:
        return {
            "token_id": self.token_id,
            "preparation_id": self.preparation_id,
            "tool_name": self.tool_name,
            "tool_version": self.tool_version,
            "subject_id": self.subject_id,
            "environment": self.environment,
            "artifact_sha256": self.artifact_sha256,
            "input_fingerprint": self.input_fingerprint,
            "resource_fingerprint": self.resource_fingerprint,
            "supervision_contract_sha256": self.supervision_contract_sha256,
            "data_classification_sha256": self.data_classification_sha256,
            "policy_bundle_version": self.policy_bundle_version,
            "risk_level": self.risk_level.value,
            "issued_at": self.issued_at.isoformat(),
            "expires_at": self.expires_at.isoformat(),
            "single_use": self.single_use,
        }

    def fingerprint(self) -> str:
        return canonical_sha256(self.canonical_payload())

    def is_expired(self, *, now: datetime) -> bool:
        return now >= self.expires_at


@dataclass(frozen=True, slots=True)
class ToolManifest:
    """工具契约清单条目；未注册的工具名或版本不匹配一律拒绝。"""

    name: str
    version: str
    effect: ToolEffect
    timeout_seconds: int
    max_records: int
    max_bytes: int
    idempotent: bool
    input_schema: Mapping[str, Any]
    output_schema: Mapping[str, Any]
    compensation_tool: str | None = None

    def canonical_payload(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "version": self.version,
            "effect": self.effect.value,
            "timeout_seconds": self.timeout_seconds,
            "max_records": self.max_records,
            "max_bytes": self.max_bytes,
            "idempotent": self.idempotent,
            "input_schema": self.input_schema,
            "output_schema": self.output_schema,
            "compensation_tool": self.compensation_tool,
        }

    def fingerprint(self) -> str:
        return canonical_sha256(self.canonical_payload())


# -------------------------------------------------------------- 分级序位
# 从宽到严的**唯一**排序口径。`UNKNOWN` 刻意排在「受限」之后、「秘密」之前：
# 分类缺失比「已知机密」更该被盯着，但把它等同于秘密会让任何未分类数据都被彻底
# 拒之门外 —— 分类器本身也是决策输入之一，缺失是常态而不是异常。
#
# 这条序位是 PDP 判风险与分类报告算总级**共用的同一份**：两处各写一遍，
# 迟早会出现「同一份 UNKNOWN 在一处按受限、在另一处按秘密」。
SENSITIVITY_ORDER: tuple[DataSensitivity, ...] = (
    DataSensitivity.PUBLIC,
    DataSensitivity.INTERNAL,
    DataSensitivity.CONFIDENTIAL,
    DataSensitivity.RESTRICTED,
    DataSensitivity.UNKNOWN,
    DataSensitivity.SECRET,
)


def sensitivity_rank(sensitivity: DataSensitivity) -> int:
    """分级在保守序位上的位置；数值越大越敏感。"""
    return SENSITIVITY_ORDER.index(sensitivity)


def stricter_of(*values: DataSensitivity) -> DataSensitivity:
    """取更严的一档；空输入按 `SECRET` 处理 —— 问不出分级时不能默认宽松。"""
    if not values:
        return DataSensitivity.SECRET
    return max(values, key=sensitivity_rank)


# ------------------------------------------------------ 职责分离强制规则
def require_no_self_approval(*, applicant_id: str, approver_id: str) -> None:
    """SOD-01：申请人禁止自批。"""
    if applicant_id == approver_id:
        raise PermissionDenied("SOD-01：申请人不得审批自己提交的内容")


def require_distinct_slot_holders(*, role_holders: Mapping[HarnessRole, str]) -> None:
    """SOD-02：同一准备单的多个审批槽不得由同一人担任。

    按角色名排序遍历，保证同一组槽位分配在任何调用顺序下给出同一错误信息。
    """
    holder_to_role: dict[str, HarnessRole] = {}
    for role, holder in sorted(role_holders.items(), key=lambda item: item[0].value):
        occupying = holder_to_role.get(holder)
        if occupying is not None:
            raise PermissionDenied(
                f"SOD-02：{role.value} 与 {occupying.value} 审批槽不得由同一人担任"
            )
        holder_to_role[holder] = role


def require_executor_not_approver(*, executor_id: str, approver_ids: Iterable[str]) -> None:
    """SOD-03：Commit 操作员不得同时是本次审批的审批人。"""
    if executor_id in set(approver_ids):
        raise PermissionDenied("SOD-03：Commit 操作员不得同时担任本次审批的审批人")


def require_auditor_is_read_only(*, harness_roles: frozenset[HarnessRole]) -> None:
    """SOD-05：审计只读。持有审计职责的主体不得参与审批、执行与写操作。

    PRD 把这条的拦截点定在**路由级**权限校验，因此这里只做判定，由调用方在写入口使用。
    """
    if HarnessRole.AUDITOR in harness_roles:
        raise PermissionDenied("SOD-05：审计人员不可参与任何审批、执行与写操作")


# 不可由同一主体同时持有的职责槽组合（SOD-02）。
MUTUALLY_EXCLUSIVE_HARNESS_ROLES: tuple[frozenset[HarnessRole], ...] = (
    frozenset({HarnessRole.DATA_APPROVER, HarnessRole.SECURITY_APPROVER}),
)


def require_no_conflicting_harness_roles(
    *, granted: frozenset[HarnessRole], additional: HarnessRole
) -> None:
    """SOD-02 的授予期校验：同一成员不得同时占据互斥的审批槽（PRD §9.1 FR-04）。

    Approve 阶段的槽位校验由 `require_distinct_slot_holders` 负责，两者互补：
    授予期拦住「一人身兼两个审批槽」，审批期拦住「同一人依次完成两个槽位」。
    """
    combined = granted | {additional}
    for exclusive in MUTUALLY_EXCLUSIVE_HARNESS_ROLES:
        if exclusive <= combined:
            conflicting = sorted(role.value for role in exclusive)
            raise PermissionDenied(f"SOD-02：{' 与 '.join(conflicting)} 审批槽不得由同一人担任")
