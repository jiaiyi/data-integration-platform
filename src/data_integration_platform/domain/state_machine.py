"""显式状态迁移表（SPEC T-05 / PRD FR-37）。

三张表分别约束管道版本、执行实例与 Agent 运行的合法流转。没有出现在表中的目标
状态一律拒绝 —— 这同时覆盖「跳步」「非法回退」与「终态重启」三类违规。
"""

from collections.abc import Mapping
from enum import StrEnum
from typing import TypeVar

from data_integration_platform.domain.enums import (
    AgentRunStatus,
    ExecutionStatus,
    PipelineVersionStatus,
)
from data_integration_platform.domain.errors import InvalidStateTransition

StateT = TypeVar("StateT", bound=StrEnum)


PIPELINE_TRANSITIONS: Mapping[PipelineVersionStatus, frozenset[PipelineVersionStatus]] = {
    PipelineVersionStatus.DRAFT: frozenset({PipelineVersionStatus.PROFILING}),
    PipelineVersionStatus.PROFILING: frozenset({PipelineVersionStatus.GENERATED}),
    PipelineVersionStatus.GENERATED: frozenset({PipelineVersionStatus.VALIDATED}),
    PipelineVersionStatus.VALIDATED: frozenset({PipelineVersionStatus.WAITING_APPROVAL}),
    PipelineVersionStatus.WAITING_APPROVAL: frozenset(
        {PipelineVersionStatus.APPROVED, PipelineVersionStatus.REJECTED}
    ),
    # APPROVED / REJECTED 为终态：需求变更走新版本 V+1，不回到 DRAFT。
    PipelineVersionStatus.APPROVED: frozenset(),
    PipelineVersionStatus.REJECTED: frozenset(),
}

EXECUTION_TRANSITIONS: Mapping[ExecutionStatus, frozenset[ExecutionStatus]] = {
    ExecutionStatus.QUEUED: frozenset(
        {ExecutionStatus.SUBMITTED, ExecutionStatus.FAILED, ExecutionStatus.CANCELLED}
    ),
    ExecutionStatus.SUBMITTED: frozenset(
        {ExecutionStatus.RUNNING, ExecutionStatus.FAILED, ExecutionStatus.CANCELLING}
    ),
    ExecutionStatus.RUNNING: frozenset(
        {ExecutionStatus.SUCCEEDED, ExecutionStatus.FAILED, ExecutionStatus.CANCELLING}
    ),
    # 取消请求成功不等于引擎已停：CANCELLING 必须经对账确认后才进终态。
    ExecutionStatus.CANCELLING: frozenset(
        {ExecutionStatus.CANCELLED, ExecutionStatus.FAILED, ExecutionStatus.SUCCEEDED}
    ),
    ExecutionStatus.SUCCEEDED: frozenset(),
    ExecutionStatus.FAILED: frozenset(),
    ExecutionStatus.CANCELLED: frozenset(),
}

AGENT_RUN_TRANSITIONS: Mapping[AgentRunStatus, frozenset[AgentRunStatus]] = {
    AgentRunStatus.QUEUED: frozenset({AgentRunStatus.RUNNING, AgentRunStatus.FAILED}),
    AgentRunStatus.RUNNING: frozenset(
        {
            AgentRunStatus.WAITING_INPUT,
            AgentRunStatus.WAITING_APPROVAL,
            AgentRunStatus.FAILED,
        }
    ),
    # 补充信息后重新入队并从同一 thread_id 恢复，不回到 RUNNING 就地续跑。
    AgentRunStatus.WAITING_INPUT: frozenset({AgentRunStatus.QUEUED, AgentRunStatus.FAILED}),
    AgentRunStatus.WAITING_APPROVAL: frozenset(),
    AgentRunStatus.FAILED: frozenset(),
}


def require_transition(
    current: StateT,
    target: StateT,
    transitions: Mapping[StateT, frozenset[StateT]],
) -> None:
    """依据显式迁移表校验状态变化，拒绝跳步、非法回退与终态重启。"""
    if target not in transitions[current]:
        raise InvalidStateTransition(f"不允许从 {current.value} 转换到 {target.value}")


def require_pipeline_transition(
    current: PipelineVersionStatus, target: PipelineVersionStatus
) -> None:
    require_transition(current, target, PIPELINE_TRANSITIONS)


def require_execution_transition(current: ExecutionStatus, target: ExecutionStatus) -> None:
    require_transition(current, target, EXECUTION_TRANSITIONS)


def require_agent_run_transition(current: AgentRunStatus, target: AgentRunStatus) -> None:
    require_transition(current, target, AGENT_RUN_TRANSITIONS)
