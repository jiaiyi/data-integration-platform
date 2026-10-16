"""受控安全进化词表（PRD FR-88~FR-91）。

提案与发布使用两个相互独立的状态机；`PROMOTION_APPROVED` 只是授权，
不代表平台已完成生产热更新。
"""

from enum import StrEnum


class SafetyProposalKind(StrEnum):
    POLICY_RULE = "POLICY_RULE"
    PROMPT = "PROMPT"
    CODE = "CODE"


class SafetyProposalStatus(StrEnum):
    DRAFT = "DRAFT"
    SUBMITTED = "SUBMITTED"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    PROMOTION_APPROVED = "PROMOTION_APPROVED"


class SafetyReleaseScope(StrEnum):
    SHADOW = "SHADOW"
    CANARY = "CANARY"


class SafetyReleaseStatus(StrEnum):
    PENDING = "PENDING"
    ACTIVE = "ACTIVE"
    REVOKED = "REVOKED"
    FAILED = "FAILED"
