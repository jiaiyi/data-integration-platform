"""数据面引擎的端口与状态投影（T-38）。

引擎只提供**它自己的事实**：作业 id、它那边的状态字符串、起止时间、错误原文。
把「引擎说的状态」翻译成控制面状态是这里唯一的判断，而它有一条硬要求：

> **状态未知时不猜。** 引擎的历史有保留期，作业也可能刚好落在两次轮询之间。
> 查不到就返回 `None`，让调用方去对账（SPEC T-38「状态未知先对账」），
> 而不是顺手记一个 `FAILED` 或 `SUCCEEDED` —— 那是把「不知道」伪装成「知道」。
"""

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from data_integration_platform.domain.enums import ExecutionStatus


@dataclass(frozen=True, slots=True)
class EngineJobState:
    """引擎侧的一次作业状态；字段名保持引擎的原文，不做二次解释。"""

    job_id: str
    engine_status: str
    error_message: str | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None

    def canonical_payload(self) -> dict[str, object]:
        return {
            "job_id": self.job_id,
            "engine_status": self.engine_status,
            "error_message": self.error_message,
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "finished_at": self.finished_at.isoformat() if self.finished_at else None,
        }


class ExecutionEnginePort(Protocol):
    async def submit(self, *, config: str, job_name: str) -> str:
        """提交作业，返回引擎的作业 id。"""
        ...

    async def status(self, *, job_id: str) -> EngineJobState | None:
        """查作业状态；**引擎报告里查不到时返回 `None`**，不猜。"""
        ...

    async def cancel(self, *, job_id: str) -> None:
        """请求取消；取消成功不等于作业已停（引擎需要自己走完 CANCELLING）。"""
        ...


# 引擎的状态词表 → 控制面状态。映射不上的状态**故意缺席**：缺席就会落到 None，
# 于是走「对账」而不是被随手归到某个近似的终态。
ENGINE_STATUS_TO_EXECUTION: Mapping[str, ExecutionStatus] = {
    "CREATED": ExecutionStatus.SUBMITTED,
    "SCHEDULED": ExecutionStatus.SUBMITTED,
    "RUNNING": ExecutionStatus.RUNNING,
    "FAILING": ExecutionStatus.RUNNING,
    "CANCELLING": ExecutionStatus.CANCELLING,
    "CANCELED": ExecutionStatus.CANCELLED,
    "CANCELLED": ExecutionStatus.CANCELLED,
    "FAILED": ExecutionStatus.FAILED,
    "FINISHED": ExecutionStatus.SUCCEEDED,
    "SUCCEEDED": ExecutionStatus.SUCCEEDED,
}


def project_status(state: EngineJobState | None) -> ExecutionStatus | None:
    """把引擎状态投影成控制面状态；**认不出来就返回 `None`**（未知，待对账）。"""
    if state is None:
        return None
    return ENGINE_STATUS_TO_EXECUTION.get(state.engine_status.upper())


def is_terminal(status: ExecutionStatus) -> bool:
    return status in (
        ExecutionStatus.SUCCEEDED,
        ExecutionStatus.FAILED,
        ExecutionStatus.CANCELLED,
    )
