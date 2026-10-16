"""真实引擎检查（T-21 / FR-35）。

这里要验的是「引擎说什么，平台就认什么」：引擎不可用、引擎拒绝、引擎放行三种结论
是否如实变成门禁结论，以及**引擎不可用时冻结一定被阻止**（FR-35 的验收口径）。
对真实 SeaTunnel 的验证在 `tests/integration/test_engine_check_live.py`，
本文件用 httpx 的 MockTransport 冒充引擎，因此不依赖 Compose。
"""

import json
from typing import Any

import httpx

from data_integration_platform.application.engine_check import (
    build_gate_runner,
    gate_runner_with_engine,
)
from data_integration_platform.application.generation import GeneratedCandidate
from data_integration_platform.application.policy import (
    DETERMINISTIC_GATE_KINDS,
    GateFinding,
    GateKind,
    SourceFacts,
    TargetFacts,
)
from data_integration_platform.config import Settings
from data_integration_platform.domain.enums import ConnectorType
from data_integration_platform.infrastructure.seatunnel_engine_check import SeaTunnelEngineCheck

SOURCE_COLUMNS = frozenset({"id", "amount"})
TARGET_COLUMNS = frozenset({"order_id", "amount"})
QUALITY_CODE = "E_POSITIVE"

HOCON = f"""env {{
  job.mode = "BATCH"
}}
source {{
  Jdbc {{
    url = "jdbc:mysql://mysql:3306/source_db"
    user = "etl"
    password = "${{MYSQL_PASSWORD}}"
  }}
}}
sink {{
  Doris {{
    table = "orders_wide"
    {QUALITY_CODE} = true
  }}
}}
"""

PLAN: dict[str, Any] = {
    "source": {"schema_name": "source_db", "table_name": "orders"},
    "target": {"schema_name": "dw", "table_name": "orders_wide", "primary_key": ["order_id"]},
    "mappings": [
        {"source_column": "id", "target_column": "order_id"},
        {"source_column": "amount", "target_column": "amount"},
    ],
    "transforms": [],
    "quality_rules": [{"column": "amount", "kind": "POSITIVE", "error_code": QUALITY_CODE}],
    "incremental": {"mode": "BATCH"},
    "dedup_keys": ["order_id"],
}

OVERVIEW = {"projectVersion": "2.3.13", "workers": "1"}


def candidate(hocon: str = HOCON) -> GeneratedCandidate:
    return GeneratedCandidate.model_validate({"plan": PLAN, "hocon": hocon})


def engine_settings() -> Settings:
    return Settings(_env_file=None, seatunnel_api_url="http://127.0.0.1:8080")


def serve(handler: Any) -> SeaTunnelEngineCheck:
    return SeaTunnelEngineCheck(engine_settings(), transport=httpx.MockTransport(handler))


def healthy_handler(*, validate: httpx.Response | None = None) -> Any:
    """正常引擎：`/overview` 自述版本，`/encrypt-config` 回显配置。"""
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        if request.url.path == "/overview":
            return httpx.Response(200, json=OVERVIEW)
        return validate if validate is not None else httpx.Response(200, json={})

    handler.calls = calls  # type: ignore[attr-defined]
    return handler


# ------------------------------------------------------------------ 引擎裁决
async def test_engine_acceptance_becomes_a_passing_finding() -> None:
    handler = healthy_handler()
    finding = await serve(handler).check(hocon=HOCON)

    assert finding.kind is GateKind.ENGINE_CHECK
    assert finding.passed
    # 结论里要带上引擎自述的事实，便于事后对账是哪台引擎、哪个版本放行的。
    assert "2.3.13" in finding.detail


async def test_unavailable_engine_blocks_freeze() -> None:
    """FR-35：引擎不可用时冻结被阻止并明确报告「引擎不可用」，不允许跳过检查直接冻结。"""

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("连接被拒绝", request=request)

    finding = await serve(handler).check(hocon=HOCON)

    assert not finding.passed
    assert finding.kind is GateKind.ENGINE_CHECK
    assert "引擎不可用" in finding.detail


async def test_engine_rejection_is_reported_with_its_own_wording() -> None:
    """引擎拒了就是拒了；平台不复述成自己的判断。"""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/overview":
            return httpx.Response(200, json=OVERVIEW)
        return httpx.Response(
            400, json={"status": "fail", "message": "Miss <Sink> config! Please check it."}
        )

    finding = await serve(handler).check(hocon=HOCON)

    assert not finding.passed
    assert "Miss <Sink> config!" in finding.detail


async def test_engine_only_gives_its_verdict_after_being_asked() -> None:
    """先探可用性、再交配置；顺序反了会先撞超时，错误信息也说不清是哪种失败。"""
    handler = healthy_handler()
    await serve(handler).check(hocon=HOCON)

    assert handler.calls == ["/overview", "/encrypt-config"]  # type: ignore[attr-defined]


# ------------------------------------------------------------------ 交给引擎，不本地代答
async def test_missing_sink_is_left_to_the_engine() -> None:
    """缺 sink 不在平台侧拦；照原样交给引擎，由引擎报错（这才是「真实引擎检查」）。"""
    sent: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/overview":
            return httpx.Response(200, json=OVERVIEW)
        sent.append(json.loads(request.content))
        return httpx.Response(400, json={"status": "fail", "message": "Miss <Sink> config!"})

    finding = await serve(handler).check(hocon='env { job.mode = "BATCH" }\nsource { Jdbc { } }\n')

    assert not finding.passed
    assert sent and sent[0]["sink"] == []
    assert "Miss <Sink> config!" in finding.detail


async def test_config_document_keeps_env_and_turns_sections_into_plugin_lists() -> None:
    """文档形状要按 SeaTunnel V2 来：env 原样，source/sink 是插件对象数组。"""
    sent: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/overview":
            return httpx.Response(200, json=OVERVIEW)
        sent.append(json.loads(request.content))
        return httpx.Response(200, json={})

    await serve(handler).check(hocon=HOCON)

    document = sent[0]
    assert document["env"] == {"job": {"mode": "BATCH"}}
    assert [list(plugin) for plugin in document["source"]] == [["Jdbc"]]
    assert [list(plugin) for plugin in document["sink"]] == [["Doris"]]
    # 占位符必须原样送达引擎，不能被平台展开成明文。
    assert document["source"][0]["Jdbc"]["password"] == "${MYSQL_PASSWORD}"


# ------------------------------------------------------------------ 本地解析失败
async def test_unparsable_hocon_fails_without_touching_the_engine() -> None:
    """连 HOCON 都不是，就没必要外呼；但仍然是一条「检查未通过」，不是异常。"""
    handler = healthy_handler()
    finding = await serve(handler).check(hocon='env { job.mode = "BATCH"\n')

    assert not finding.passed
    assert handler.calls == []  # type: ignore[attr-defined]


async def test_engine_message_is_scrubbed() -> None:
    """引擎原话里若夹带凭据，回到平台时必须是掩码（FR-08 口径）。"""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/overview":
            return httpx.Response(200, json=OVERVIEW)
        return httpx.Response(
            400,
            json={
                "status": "fail",
                "message": "connect failed: jdbc:mysql://etl:sup3rs3cret@mysql:3306/db",
            },
        )

    finding = await serve(handler).check(hocon=HOCON)

    assert not finding.passed
    assert "sup3rs3cret" not in finding.detail


# ------------------------------------------------------------------ 与确定性门禁的组装
class RecordingEngine:
    def __init__(self, finding: GateFinding) -> None:
        self.finding = finding
        self.calls = 0

    async def check(self, *, hocon: str) -> GateFinding:
        self.calls += 1
        return self.finding


def facts() -> dict[str, Any]:
    return {
        "source": SourceFacts(connector_type=ConnectorType.MYSQL, columns=SOURCE_COLUMNS),
        "target": TargetFacts(connector_type=ConnectorType.DORIS, columns=TARGET_COLUMNS),
    }


async def test_passing_engine_check_is_appended_to_the_report() -> None:
    engine = RecordingEngine(GateFinding(GateKind.ENGINE_CHECK, True, "引擎已接受"))
    report = await gate_runner_with_engine(candidate(), engine=engine, **facts())

    assert report.passed
    assert not report.engine_failed()
    # 引擎结论是**追加**在七项确定性结论之后的第八条，不是替换掉它们。
    assert {finding.kind for finding in report.findings} == {
        *DETERMINISTIC_GATE_KINDS,
        GateKind.ENGINE_CHECK,
    }


async def test_engine_failure_marks_the_report_as_engine_failed() -> None:
    """状态机据此把修复提示的措辞从「确定性失败」切到「引擎失败」（agent/graph.py）。"""
    engine = RecordingEngine(GateFinding(GateKind.ENGINE_CHECK, False, "引擎不可用：连接被拒绝"))
    report = await gate_runner_with_engine(candidate(), engine=engine, **facts())

    assert not report.passed
    assert report.engine_failed()
    assert "引擎不可用" in (report.repair_hint() or "")


async def test_engine_is_not_called_when_the_deterministic_gate_already_failed() -> None:
    """确定性门禁不过就不再外呼：候选已被拒，多打一次引擎只会挤掉修复提示。"""
    engine = RecordingEngine(GateFinding(GateKind.ENGINE_CHECK, True, "不该被用到"))
    # 映射引用了源端不存在的字段，字段拓扑这一项必然失败。
    broken_plan = {**PLAN, "mappings": [{"source_column": "nope", "target_column": "order_id"}]}
    broken = GeneratedCandidate.model_validate({"plan": broken_plan, "hocon": HOCON})
    report = await gate_runner_with_engine(broken, engine=engine, **facts())

    assert not report.passed
    assert engine.calls == 0
    assert all(finding.kind is not GateKind.ENGINE_CHECK for finding in report.findings)


async def test_build_gate_runner_returns_the_state_machine_shape() -> None:
    engine = RecordingEngine(GateFinding(GateKind.ENGINE_CHECK, True, "引擎已接受"))
    runner = build_gate_runner(engine=engine, **facts())

    report = await runner(candidate())

    assert report.passed
    assert engine.calls == 1
