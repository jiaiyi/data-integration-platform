"""生成状态机（T-19 / FR-25 ~ FR-27、FR-31）。

图的结构（与 SPEC §7.2 一致）：

```
generate ──QUESTION──▶ clarify ──▶ generate
   │
   ├──ERROR───────────▶ repair_generation ──▶ generate
   │                                    └──▶ failed
   └──CONFIG──────────▶ policy ──通过──▶ wait_for_review ──▶ END
                            └──不通过──▶ repair_generation
```

**修复预算只在 `repair_generation` 一个节点里消耗。** 三类失败 ——
① 候选完整性失败（`generate` 的 ERROR 分支）、② 确定性门禁失败、③ 真实引擎检查未通过
（②③ 都在 `policy` 节点内）—— 全都汇流到这一个节点，因此「共享同一计数」是**结构上成立**的，
而不是靠三处各自记得自增（那样迟早会有一处漏掉，预算就能被绕过）。

真实引擎检查在 `policy` 节点内完成，也就是**在人工审批中断之前**（SPEC §7.2 的顺序约束）：
否则错误候选会先占用人工时间，而已经用掉的候选预算也收不回来。
"""

from collections.abc import Awaitable, Callable
from enum import StrEnum
from typing import Any, TypedDict

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph
from langgraph.types import interrupt

from data_integration_platform.application.generation import (
    GeneratedCandidate,
    GenerationPort,
    GenerationRequest,
    parse_generation_outcome,
)
from data_integration_platform.application.policy import GateReport
from data_integration_platform.domain.enums import GenerationKind

# 门禁 + 真实引擎检查的统一入口；由编排方注入（本层不碰数据库，也不碰引擎）。
GateRunner = Callable[[GeneratedCandidate], Awaitable[GateReport]]


class GenerationPhase(StrEnum):
    """状态机当前所处阶段；供 AgentRun 的可观测字段使用（FR-32）。"""

    GENERATE = "GENERATE"
    CLARIFY = "CLARIFY"
    REPAIR_GENERATION = "REPAIR_GENERATION"
    POLICY = "POLICY"
    WAIT_FOR_REVIEW = "WAIT_FOR_REVIEW"
    FAILED = "FAILED"


class FailureKind(StrEnum):
    """三类失败共享同一份预算，这里显式登记它们的来源。"""

    CANDIDATE_INTEGRITY = "CANDIDATE_INTEGRITY"
    DETERMINISTIC_GATE = "DETERMINISTIC_GATE"
    ENGINE_CHECK = "ENGINE_CHECK"


class GenerationState(TypedDict, total=False):
    """图状态；全部字段可 JSON 序列化，以便 Checkpoint 持久化与恢复。"""

    requirement: str
    source_summary: str
    target_summary: str
    profile_context: str
    # 澄清问答的累积文本，作为后续生成的额外上下文
    clarification: str
    pending_question: str | None
    plan: dict[str, Any] | None
    hocon: str | None
    gate_findings: list[dict[str, Any]]
    repair_count: int
    candidates: int
    phase: str
    failure_reason: str | None
    diagnostics: list[dict[str, Any]]
    awaiting_approval: bool


def initial_state(
    *,
    requirement: str,
    source_summary: str,
    target_summary: str,
    profile_context: str = "",
) -> GenerationState:
    return GenerationState(
        requirement=requirement,
        source_summary=source_summary,
        target_summary=target_summary,
        profile_context=profile_context,
        clarification="",
        pending_question=None,
        plan=None,
        hocon=None,
        gate_findings=[],
        repair_count=0,
        candidates=0,
        phase=GenerationPhase.GENERATE.value,
        failure_reason=None,
        diagnostics=[],
        awaiting_approval=False,
    )


def build_generation_graph(
    *,
    adapter: GenerationPort,
    gate_runner: GateRunner,
    max_repairs: int,
    checkpointer: BaseCheckpointSaver[Any] | None = None,
) -> CompiledStateGraph[GenerationState, None, GenerationState, GenerationState]:
    """装配状态机；依赖全部注入，便于用假后端与假门禁跑剧本测试。

    `checkpointer` 是中断与恢复的前提：没有它，`interrupt()` 无法持久化挂起点，
    也就谈不上「补充信息后从同一 thread_id 继续」（FR-27）。
    生产用 PostgreSQL Checkpoint，测试用内存实现。
    """

    async def generate(state: GenerationState) -> GenerationState:
        request = GenerationRequest(
            requirement=state["requirement"],
            source_summary=state["source_summary"],
            target_summary=state["target_summary"],
            profile_context=_context_with_clarification(state),
            repair_hint=state.get("failure_reason"),
        )
        raw = await adapter.generate(request)
        outcome = parse_generation_outcome(raw)
        candidates = state["candidates"] + 1

        if outcome.kind is GenerationKind.QUESTION:
            # 参数不足时中断提问，而不是臆造参数（FR-26）。
            return GenerationState(
                phase=GenerationPhase.CLARIFY.value,
                pending_question=outcome.question,
                candidates=candidates,
            )
        if outcome.candidate is None:
            return GenerationState(
                phase=GenerationPhase.REPAIR_GENERATION.value,
                candidates=candidates,
                failure_reason=outcome.error,
                diagnostics=[
                    *state["diagnostics"],
                    _diagnostic(FailureKind.CANDIDATE_INTEGRITY, outcome.error or ""),
                ],
            )
        candidate = outcome.candidate
        return GenerationState(
            phase=GenerationPhase.POLICY.value,
            candidates=candidates,
            plan=candidate.plan.model_dump(mode="json"),
            hocon=candidate.hocon,
            pending_question=None,
        )

    async def clarify(state: GenerationState) -> GenerationState:
        # interrupt 会把状态持久化并在此挂起；恢复时返回用户补充的内容。
        answer = interrupt(
            {
                "kind": "CLARIFICATION_REQUIRED",
                "question": state.get("pending_question") or "",
            }
        )
        merged = f"{state['clarification']}\n补充：{answer}".strip()
        return GenerationState(
            clarification=merged, pending_question=None, phase=GenerationPhase.GENERATE.value
        )

    async def repair_generation(state: GenerationState) -> GenerationState:
        """**唯一**消耗修复预算的地方；三类失败都经过这里。"""
        consumed = state["repair_count"] + 1
        if consumed > max_repairs:
            return GenerationState(
                repair_count=consumed,
                phase=GenerationPhase.FAILED.value,
                failure_reason=state.get("failure_reason"),
            )
        return GenerationState(
            repair_count=consumed,
            phase=GenerationPhase.REPAIR_GENERATION.value,
        )

    async def policy(state: GenerationState) -> GenerationState:
        candidate = _candidate_from_state(state)
        report = await gate_runner(candidate)
        findings = [_finding(item.kind.value, item.passed, item.detail) for item in report.findings]
        if report.passed:
            return GenerationState(
                phase=GenerationPhase.WAIT_FOR_REVIEW.value, gate_findings=findings
            )
        # 确定性门禁与真实引擎检查都在这份报告里，两者共享同一预算。
        kind = (
            FailureKind.ENGINE_CHECK if report.engine_failed() else FailureKind.DETERMINISTIC_GATE
        )
        return GenerationState(
            phase=GenerationPhase.REPAIR_GENERATION.value,
            gate_findings=findings,
            failure_reason=report.repair_hint(),
            diagnostics=[*state["diagnostics"], _diagnostic(kind, report.repair_hint() or "")],
        )

    async def wait_for_review(state: GenerationState) -> GenerationState:
        # 真实引擎检查已经在本节点之前完成；这里是等待**人工**审批的挂起点（M5 接管审批链路）。
        interrupt(
            {
                "kind": "APPROVAL_REQUIRED",
                "summary": {
                    "candidates": state["candidates"],
                    "repairs": state["repair_count"],
                },
            }
        )
        return GenerationState(awaiting_approval=True, phase=GenerationPhase.WAIT_FOR_REVIEW.value)

    async def failed(state: GenerationState) -> GenerationState:
        return GenerationState(phase=GenerationPhase.FAILED.value)

    def route_generation(state: GenerationState) -> str:
        phase = state["phase"]
        if phase == GenerationPhase.CLARIFY.value:
            return "clarify"
        if phase == GenerationPhase.POLICY.value:
            return "policy"
        return "repair_generation"

    def route_repair(state: GenerationState) -> str:
        return "failed" if state["phase"] == GenerationPhase.FAILED.value else "generate"

    def route_policy(state: GenerationState) -> str:
        return (
            "wait_for_review"
            if state["phase"] == GenerationPhase.WAIT_FOR_REVIEW.value
            else "repair_generation"
        )

    builder = StateGraph(GenerationState)
    builder.add_node("generate", generate)
    builder.add_node("clarify", clarify)
    builder.add_node("repair_generation", repair_generation)
    builder.add_node("policy", policy)
    builder.add_node("wait_for_review", wait_for_review)
    builder.add_node("failed", failed)

    builder.add_edge(START, "generate")
    builder.add_conditional_edges(
        "generate",
        route_generation,
        {"clarify": "clarify", "policy": "policy", "repair_generation": "repair_generation"},
    )
    builder.add_edge("clarify", "generate")
    builder.add_conditional_edges(
        "repair_generation", route_repair, {"generate": "generate", "failed": "failed"}
    )
    builder.add_conditional_edges(
        "policy",
        route_policy,
        {"wait_for_review": "wait_for_review", "repair_generation": "repair_generation"},
    )
    builder.add_edge("wait_for_review", END)
    builder.add_edge("failed", END)
    return builder.compile(checkpointer=checkpointer)


def _candidate_from_state(state: GenerationState) -> GeneratedCandidate:
    plan = state.get("plan")
    hocon = state.get("hocon")
    if plan is None or not hocon:
        raise ValueError("状态里没有候选可选：policy 节点只应在生成成功后进入")
    return GeneratedCandidate.model_validate({"plan": plan, "hocon": hocon})


def _context_with_clarification(state: GenerationState) -> str:
    parts = [state.get("profile_context") or ""]
    if state.get("clarification"):
        parts.append("# 工程师补充的澄清\n" + state["clarification"])
    return "\n\n".join(part for part in parts if part)


def _diagnostic(kind: FailureKind, reason: str) -> dict[str, Any]:
    return {"kind": kind.value, "reason": reason}


def _finding(gate: str, passed: bool, detail: str) -> dict[str, Any]:
    return {"gate": gate, "passed": passed, "detail": detail}
