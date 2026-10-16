"""状态机非法流转拒绝（SPEC T-05 / PRD §9.4 FR-37）。

三类违规都必须抛出 `InvalidStateTransition`：跳步、非法回退、终态重启。
"""

from itertools import pairwise

import pytest

from data_integration_platform.domain.enums import (
    AgentRunStatus,
    ExecutionStatus,
    PipelineVersionStatus,
)
from data_integration_platform.domain.errors import InvalidStateTransition
from data_integration_platform.domain.state_machine import (
    require_agent_run_transition,
    require_execution_transition,
    require_pipeline_transition,
)


def test_pipeline_happy_path_is_allowed() -> None:
    """草稿 → 探查 → 已生成 → 已校验 → 等待审批 → 已批准。"""
    chain = [
        PipelineVersionStatus.DRAFT,
        PipelineVersionStatus.PROFILING,
        PipelineVersionStatus.GENERATED,
        PipelineVersionStatus.VALIDATED,
        PipelineVersionStatus.WAITING_APPROVAL,
        PipelineVersionStatus.APPROVED,
    ]
    for current, target in pairwise(chain):
        require_pipeline_transition(current, target)


def test_pipeline_rejects_skipping_states() -> None:
    """PRD §9.4：草稿直接跃迁到等待审批必须被拒绝。"""
    with pytest.raises(InvalidStateTransition, match="DRAFT 转换到 WAITING_APPROVAL"):
        require_pipeline_transition(
            PipelineVersionStatus.DRAFT, PipelineVersionStatus.WAITING_APPROVAL
        )


@pytest.mark.parametrize(
    "terminal", [PipelineVersionStatus.APPROVED, PipelineVersionStatus.REJECTED]
)
def test_pipeline_terminals_cannot_restart(terminal: PipelineVersionStatus) -> None:
    """终态不可重启：需求变更走新版本 V+1，而不是让旧版本回到 DRAFT。"""
    for target in PipelineVersionStatus:
        with pytest.raises(InvalidStateTransition):
            require_pipeline_transition(terminal, target)


def test_pipeline_rejected_is_reachable_from_waiting_approval() -> None:
    require_pipeline_transition(
        PipelineVersionStatus.WAITING_APPROVAL, PipelineVersionStatus.REJECTED
    )


def test_execution_must_pass_through_cancelling() -> None:
    """取消请求成功不等于引擎已停：RUNNING 不得直接跳到 CANCELLED。"""
    with pytest.raises(InvalidStateTransition, match="RUNNING 转换到 CANCELLED"):
        require_execution_transition(ExecutionStatus.RUNNING, ExecutionStatus.CANCELLED)
    require_execution_transition(ExecutionStatus.RUNNING, ExecutionStatus.CANCELLING)
    require_execution_transition(ExecutionStatus.CANCELLING, ExecutionStatus.CANCELLED)


def test_execution_does_not_allow_backwards_transition() -> None:
    with pytest.raises(InvalidStateTransition):
        require_execution_transition(ExecutionStatus.SUBMITTED, ExecutionStatus.QUEUED)


def test_agent_run_waiting_input_returns_to_queue_not_running() -> None:
    """补充信息后重新入队并从同一 thread_id 恢复，不就地续跑。"""
    require_agent_run_transition(AgentRunStatus.WAITING_INPUT, AgentRunStatus.QUEUED)
    with pytest.raises(InvalidStateTransition):
        require_agent_run_transition(AgentRunStatus.WAITING_INPUT, AgentRunStatus.RUNNING)


def test_agent_run_terminals_are_closed() -> None:
    for terminal in (AgentRunStatus.WAITING_APPROVAL, AgentRunStatus.FAILED):
        for target in AgentRunStatus:
            with pytest.raises(InvalidStateTransition):
                require_agent_run_transition(terminal, target)
