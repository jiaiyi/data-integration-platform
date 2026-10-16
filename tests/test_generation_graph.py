"""生成状态机（T-19 / FR-25 ~ FR-27、FR-31）。

用**剧本化**的假后端与假门禁跑，因为要验的恰恰是「失败怎么处理」：
澄清中断与恢复、三类失败共享同一份候选预算、以及真实引擎检查必须早于人工审批中断。
真实模型只产出候选，这些边界是确定性代码的事（ADR-06），所以用剧本测才有意义。
"""

import json
from typing import Any

import pytest
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command

from data_integration_platform.agent.graph import (
    FailureKind,
    GenerationPhase,
    build_generation_graph,
    initial_state,
)
from data_integration_platform.application.policy import (
    DETERMINISTIC_GATE_KINDS,
    GateFinding,
    GateKind,
    GateReport,
)
from data_integration_platform.config import get_settings

VALID_PLAN: dict[str, Any] = {
    "source": {"schema_name": "source_db", "table_name": "orders"},
    "target": {"schema_name": "dw", "table_name": "orders_wide", "primary_key": ["order_id"]},
    "mappings": [{"source_column": "id", "target_column": "order_id"}],
    "transforms": [],
    "quality_rules": [{"column": "id", "kind": "NOT_NULL", "error_code": "E_NOT_NULL"}],
    "incremental": {"mode": "BATCH"},
    "dedup_keys": ["order_id"],
}
VALID_HOCON = (
    'env { job.mode = "BATCH" }\nsink { Doris { table = "orders_wide" E_NOT_NULL = 1 } }\n'
)
MAX_REPAIRS = 2


def candidate_json() -> str:
    return json.dumps({"plan": VALID_PLAN, "hocon": VALID_HOCON}, ensure_ascii=False)


def question_json(text: str = "增量水位列未指定，请指定") -> str:
    return json.dumps({"question": text}, ensure_ascii=False)


def broken_json() -> str:
    return '{"plan": {"source": '


def passing_report() -> GateReport:
    findings = tuple(
        GateFinding(kind=kind, passed=True, detail="ok") for kind in DETERMINISTIC_GATE_KINDS
    )
    return GateReport(findings=findings)


def failing_report(*, engine: bool = False) -> GateReport:
    kind = GateKind.ENGINE_CHECK if engine else GateKind.FIELD_TOPOLOGY
    findings = tuple(
        GateFinding(
            kind=item,
            passed=item is not kind,
            detail="ok" if item is not kind else "故意失败",
        )
        for item in [*DETERMINISTIC_GATE_KINDS, GateKind.ENGINE_CHECK]
    )
    return GateReport(findings=findings)


class ScriptedAdapter:
    """按剧本依次返回响应；记录每次请求以便断言修复提示被传回模型。"""

    backend_name = "scripted"

    def __init__(self, responses: list[str]) -> None:
        self.responses = list(responses)
        self.requests: list[Any] = []

    async def generate(self, request: Any) -> str:
        self.requests.append(request)
        if not self.responses:
            raise AssertionError("剧本已用尽：状态机请求次数超出预期")
        return self.responses.pop(0)


class ScriptedGate:
    def __init__(self, reports: list[GateReport]) -> None:
        self.reports = list(reports)
        self.calls = 0

    async def __call__(self, candidate: Any) -> GateReport:
        self.calls += 1
        if not self.reports:
            raise AssertionError("门禁剧本已用尽：调用次数超出预期")
        return self.reports.pop(0)


def build(adapter: ScriptedAdapter, gate: ScriptedGate) -> Any:
    return build_generation_graph(
        adapter=adapter,
        gate_runner=gate,
        max_repairs=MAX_REPAIRS,
        checkpointer=InMemorySaver(),
    )


def config(thread: str = "thread-1") -> dict[str, Any]:
    return {"configurable": {"thread_id": thread}}


def state_of(**overrides: Any) -> dict[str, Any]:
    values: dict[str, Any] = {
        "requirement": "把订单同步到宽表，金额必须为正",
        "source_summary": "MySQL source_db.orders",
        "target_summary": "Doris dw.orders_wide",
    }
    values.update(overrides)
    return dict(initial_state(**values))


# ------------------------------------------------------------ 正常路径
async def test_happy_path_stops_at_human_approval() -> None:
    adapter = ScriptedAdapter([candidate_json()])
    gate = ScriptedGate([passing_report()])
    graph = build(adapter, gate)

    result = await graph.ainvoke(state_of(), config())

    assert result["candidates"] == 1
    assert result["repair_count"] == 0
    assert "__interrupt__" in result
    assert result["__interrupt__"][0].value["kind"] == "APPROVAL_REQUIRED"
    snapshot = await graph.aget_state(config())
    assert snapshot.next == ("wait_for_review",)


# ------------------------------------------------ 澄清中断与恢复（FR-26/27）
async def test_clarification_interrupts_and_resumes_from_same_thread() -> None:
    adapter = ScriptedAdapter([question_json(), candidate_json()])
    gate = ScriptedGate([passing_report()])
    graph = build(adapter, gate)

    first = await graph.ainvoke(state_of(), config("thread-clarify"))
    assert "__interrupt__" in first
    assert first["__interrupt__"][0].value["kind"] == "CLARIFICATION_REQUIRED"
    assert "水位列" in first["__interrupt__"][0].value["question"]
    assert first["candidates"] == 1
    # 中断时不得产出任何可行制品
    assert first.get("plan") is None
    snapshot = await graph.aget_state(config("thread-clarify"))
    assert snapshot.next == ("clarify",)

    resumed = await graph.ainvoke(
        Command(resume="用 updated_at 作为水位列"), config("thread-clarify")
    )

    assert resumed["candidates"] == 2
    assert "__interrupt__" in resumed
    assert resumed["__interrupt__"][0].value["kind"] == "APPROVAL_REQUIRED"
    # 恢复后**带着澄清继续**，而不是从头重跑：模型看到的是补充内容
    assert "updated_at" in adapter.requests[1].profile_context
    assert adapter.requests[1].requirement == adapter.requests[0].requirement


# -------------------------------------- 3 份候选共享预算（FR-31）关键证据
async def test_candidate_integrity_failures_exhaust_shared_budget() -> None:
    """连续结构失败：总共产出 3 份候选（首次 + 2 次修复）后安全终止。"""
    adapter = ScriptedAdapter([broken_json(), broken_json(), broken_json()])
    gate = ScriptedGate([])
    graph = build(adapter, gate)

    result = await graph.ainvoke(state_of(), config("thread-broken"))

    assert result["candidates"] == 3
    assert result["repair_count"] == 3
    assert result["phase"] == GenerationPhase.FAILED.value
    assert result.get("plan") is None
    assert gate.calls == 0
    kinds = [item["kind"] for item in result["diagnostics"]]
    assert kinds == [FailureKind.CANDIDATE_INTEGRITY.value] * 3


async def test_gate_failures_exhaust_shared_budget() -> None:
    adapter = ScriptedAdapter([candidate_json(), candidate_json(), candidate_json()])
    gate = ScriptedGate([failing_report(), failing_report(), failing_report()])
    graph = build(adapter, gate)

    result = await graph.ainvoke(state_of(), config("thread-gate"))

    assert result["candidates"] == 3
    assert result["repair_count"] == 3
    assert result["phase"] == GenerationPhase.FAILED.value
    assert result["diagnostics"][0]["kind"] == FailureKind.DETERMINISTIC_GATE.value


async def test_three_failure_kinds_share_one_counter() -> None:
    """**核心断言**：三类失败累加同一个计数，不存在各自独立的预算空间。

    如果实现让「格式失败」和「策略失败」各留 2 次，这里会多跑出候选，
    预算就从 3 份膨胀成 7 份 —— 这条测试就是防这个。
    """
    adapter = ScriptedAdapter([broken_json(), candidate_json(), candidate_json()])
    gate = ScriptedGate([failing_report(), failing_report(engine=True)])
    graph = build(adapter, gate)

    result = await graph.ainvoke(state_of(), config("thread-mixed"))

    assert result["candidates"] == 3
    assert result["repair_count"] == 3
    assert result["phase"] == GenerationPhase.FAILED.value
    kinds = [item["kind"] for item in result["diagnostics"]]
    assert kinds == [
        FailureKind.CANDIDATE_INTEGRITY.value,
        FailureKind.DETERMINISTIC_GATE.value,
        FailureKind.ENGINE_CHECK.value,
    ]


async def test_engine_check_failure_never_reaches_human_approval() -> None:
    """真实引擎检查必须早于人工审批中断（SPEC §7.2 的顺序约束）。

    否则错误候选会先占用人工时间，而已经用掉的候选预算也收不回来。
    """
    adapter = ScriptedAdapter([candidate_json(), candidate_json(), candidate_json()])
    gate = ScriptedGate(
        [failing_report(engine=True), failing_report(engine=True), failing_report(engine=True)]
    )
    graph = build(adapter, gate)

    result = await graph.ainvoke(state_of(), config("thread-engine"))

    assert "__interrupt__" not in result
    assert result["awaiting_approval"] is False
    assert result["phase"] == GenerationPhase.FAILED.value


async def test_second_candidate_passing_continues_the_flow() -> None:
    """第 2 份候选通过门禁时流程继续，修复计数如实记为 1。"""
    adapter = ScriptedAdapter([broken_json(), candidate_json()])
    gate = ScriptedGate([passing_report()])
    graph = build(adapter, gate)

    result = await graph.ainvoke(state_of(), config("thread-recover"))

    assert result["candidates"] == 2
    assert result["repair_count"] == 1
    assert result["__interrupt__"][0].value["kind"] == "APPROVAL_REQUIRED"


async def test_repair_hint_is_fed_back_to_the_model() -> None:
    """修复必须带上去一次的失败原因，否则只是重试而不是修复。"""
    adapter = ScriptedAdapter([candidate_json(), candidate_json()])
    gate = ScriptedGate([failing_report(), passing_report()])
    graph = build(adapter, gate)

    await graph.ainvoke(state_of(), config("thread-hint"))

    assert adapter.requests[0].repair_hint is None
    assert adapter.requests[1].repair_hint is not None
    assert GateKind.FIELD_TOPOLOGY.value in adapter.requests[1].repair_hint


def test_budget_is_one_initial_plus_configured_repairs() -> None:
    """候选总量 = 首次 1 份 + 修复次数（PRD 决策 Q1 定档为 3）。"""
    assert get_settings().max_enterprise_repair_attempts == MAX_REPAIRS


@pytest.mark.parametrize("bad", ['{"plan": 1}', '{"nope": true}', "not json at all"])
async def test_unnamed_structural_failures_are_treated_as_candidate_failures(bad: str) -> None:
    """既没有 plan 也没有 question、或干脆不是 JSON，都归入候选完整性失败。"""
    adapter = ScriptedAdapter([bad, candidate_json()])
    gate = ScriptedGate([passing_report()])
    graph = build(adapter, gate)

    result = await graph.ainvoke(state_of(), config("thread-bad"))

    assert result["diagnostics"][0]["kind"] == FailureKind.CANDIDATE_INTEGRITY.value
    assert result["repair_count"] == 1
