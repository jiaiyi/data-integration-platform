"""版本化 Benchmark 词表（PRD FR-83~FR-87）。

场景分三层仿真：L0 静态无副作用、L1 注入故障与重放、L2 真实链路。
"""

from enum import StrEnum


class BenchmarkLayer(StrEnum):
    L0 = "L0"
    L1 = "L1"
    L2 = "L2"


class BenchmarkRunStatus(StrEnum):
    QUEUED = "QUEUED"
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"


class BenchmarkGateConclusion(StrEnum):
    """门禁结论：任一 P0/P1 漏拦即整体失败，高平均分不能抵消。"""

    PASSED = "PASSED"
    FAILED = "FAILED"
