"""运行时监督契约（SPEC T-43 / PRD FR-75）。

`RuntimeBudget` 是冻结的硬上限：作业开始后不得扩权，Plan 可以收紧平台上限，
但不能在 Commit 时临时放宽。
"""

from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from data_integration_platform.domain.harness import canonical_sha256


class SupervisionAction(StrEnum):
    """分级减损动作；动作只能收窄权限，不能修改作业语义。"""

    CONTINUE = "CONTINUE"
    THROTTLE = "THROTTLE"
    PAUSE = "PAUSE"
    CANCEL = "CANCEL"
    QUARANTINE = "QUARANTINE"


class RuntimeMitigationStatus(StrEnum):
    """需要外部副作用的监督动作的独立可重试状态。"""

    PENDING = "PENDING"
    APPLIED = "APPLIED"
    FAILED = "FAILED"


@dataclass(frozen=True, slots=True)
class RuntimeBudget:
    """单次运行的监督预算与预警口径。"""

    max_records: int
    max_bytes: int
    timeout_seconds: int
    max_output_ratio: float
    max_reject_rate: float
    minimum_quality_rows: int
    warning_ratio: float

    def canonical_payload(self) -> dict[str, Any]:
        return {
            "max_records": self.max_records,
            "max_bytes": self.max_bytes,
            "timeout_seconds": self.timeout_seconds,
            "max_output_ratio": self.max_output_ratio,
            "max_reject_rate": self.max_reject_rate,
            "minimum_quality_rows": self.minimum_quality_rows,
            "warning_ratio": self.warning_ratio,
        }

    def fingerprint(self) -> str:
        """监督契约摘要；写入 Capability 声明，使授权与预算绑定。"""
        return canonical_sha256(self.canonical_payload())
