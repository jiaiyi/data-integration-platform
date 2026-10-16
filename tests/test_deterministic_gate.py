"""确定性静态门禁（T-20 / FR-34）。

七项检查各自都要有**拒绝**用例 —— 一个只会说「通过」的门禁等于没有门禁。
这里全部是纯计算：不调用模型、不连数据源，因此边界可以被穷举。
"""

from typing import Any

import pytest

from data_integration_platform.application.generation import GeneratedCandidate
from data_integration_platform.application.policy import (
    DETERMINISTIC_GATE_KINDS,
    GateKind,
    SourceFacts,
    TargetFacts,
    evaluate,
)
from data_integration_platform.config import get_settings
from data_integration_platform.domain.enums import ConnectorType, ScheduleMode

SOURCE_COLUMNS = frozenset(
    {"id", "user_id", "amount", "customer_name", "email", "created_at", "updated_at"}
)
TARGET_COLUMNS = frozenset({"order_id", "user_id", "amount", "email_masked", "order_at"})
QUALITY_CODES = ("E_NOT_NULL", "E_POSITIVE", "E_EMAIL")


def baseline_hocon(*, extra: str = "", table: str = "orders_wide") -> str:
    """一份最小但完整的 SeaTunnel 配置；错误码必须出现在其中才能被追溯。"""
    codes = "\n".join(f"      {code} = true" for code in QUALITY_CODES)
    return f"""env {{
  job.mode = "STREAMING"
}}
source {{
  Jdbc {{
    url = "jdbc:mysql://mysql:3306/source_db"
    user = "etl"
    password = "${{MYSQL_PASSWORD}}"
  }}
}}
transform {{
  Sql {{
    query = "select * from orders"
  }}
}}
sink {{
  Doris {{
    table = "{table}"
    {codes}
    {extra}
  }}
}}
"""


def baseline_plan(**overrides: Any) -> dict[str, Any]:
    plan: dict[str, Any] = {
        "source": {"schema_name": "source_db", "table_name": "orders"},
        "target": {
            "schema_name": "dw",
            "table_name": "orders_wide",
            "primary_key": ["order_id"],
        },
        "mappings": [
            {"source_column": "id", "target_column": "order_id"},
            {"source_column": "user_id", "target_column": "user_id"},
            {"source_column": "amount", "target_column": "amount"},
            {
                "source_column": "email",
                "target_column": "email_masked",
                "transforms": ["mask_email"],
            },
            {"source_column": "updated_at", "target_column": "order_at"},
        ],
        "transforms": [{"name": "mask_email", "kind": "MASK", "target_column": "email_masked"}],
        "quality_rules": [
            {"column": "amount", "kind": "POSITIVE", "error_code": "E_POSITIVE"},
            {"column": "customer_name", "kind": "NOT_NULL", "error_code": "E_NOT_NULL"},
            {"column": "email", "kind": "EMAIL_FORMAT", "error_code": "E_EMAIL"},
        ],
        "incremental": {
            "mode": ScheduleMode.WATERMARK_INCREMENTAL.value,
            "watermark_column": "updated_at",
        },
        "dedup_keys": ["order_id"],
    }
    plan.update(overrides)
    return plan


def candidate(plan: dict[str, Any] | None = None, hocon: str | None = None) -> GeneratedCandidate:
    return GeneratedCandidate.model_validate(
        {"plan": plan or baseline_plan(), "hocon": hocon or baseline_hocon()}
    )


def findings_for(candidate_: GeneratedCandidate) -> dict[GateKind, Any]:
    report = evaluate(
        candidate_,
        source=SourceFacts(connector_type=ConnectorType.MYSQL, columns=SOURCE_COLUMNS),
        target=TargetFacts(connector_type=ConnectorType.DORIS, columns=TARGET_COLUMNS),
    )
    return {finding.kind: finding for finding in report.findings}


# ------------------------------------------------------------------ 基线
def test_baseline_candidate_passes_every_gate() -> None:
    """先证明基线全过 —— 否则后面的「拒绝」用例可能只是因为基线本身就是坏的。"""
    report = evaluate(
        candidate(),
        source=SourceFacts(connector_type=ConnectorType.MYSQL, columns=SOURCE_COLUMNS),
        target=TargetFacts(connector_type=ConnectorType.DORIS, columns=TARGET_COLUMNS),
    )
    assert report.passed, report.repair_hint()
    assert {finding.kind for finding in report.findings} == DETERMINISTIC_GATE_KINDS
    # 真实引擎检查由引擎检查器追加，不属于这七项；确定性门禁不该替它下结论。
    assert GateKind.ENGINE_CHECK not in DETERMINISTIC_GATE_KINDS
    assert report.repair_hint() is None


def test_non_engine_movable_source_is_rejected() -> None:
    """REST API 只支持「登记与探查」，不能作为管道端点（FR-103 只要求探查）。

    这一条如果不拦，会一路拖到执行阶段才失败。
    """
    report = evaluate(
        candidate(),
        source=SourceFacts(connector_type=ConnectorType.REST_API, columns=SOURCE_COLUMNS),
        target=TargetFacts(connector_type=ConnectorType.DORIS, columns=TARGET_COLUMNS),
    )
    assert not report.passed
    assert any(finding.kind is GateKind.CONNECTOR_ALLOWLIST for finding in report.failing())


def test_hive_cannot_be_a_sink() -> None:
    report = evaluate(
        candidate(),
        source=SourceFacts(connector_type=ConnectorType.MYSQL, columns=SOURCE_COLUMNS),
        target=TargetFacts(connector_type=ConnectorType.HIVE),
    )
    assert not report.passed
    assert "不能作为写入端" in report.repair_hint()


# ------------------------------------------------------------ 内联明文凭据
@pytest.mark.parametrize(
    "line",
    [
        'password = "hunter2"',
        'password="hunter2"',
        'secret_key = "AKIAIOSFODNN7EXAMPLE"',
        'api_key: "sk-live-123"',
        'token = "abc"',
    ],
)
def test_inline_plaintext_credentials_are_rejected(line: str) -> None:
    hocon = baseline_hocon(extra=line)
    assert not findings_for(candidate(hocon=hocon))[GateKind.SECRET_PLACEHOLDER].passed


@pytest.mark.parametrize(
    "value",
    ["${MYSQL_PASSWORD}", "${env:MYSQL_PASSWORD}", "{{secret://db/mysql}}", "vault://secret/db"],
)
def test_secret_placeholders_are_accepted(value: str) -> None:
    hocon = baseline_hocon(extra=f'password2 = "{value}"')
    assert findings_for(candidate(hocon=hocon))[GateKind.SECRET_PLACEHOLDER].passed


# ---------------------------------------------------------------- 字段拓扑
def test_unknown_source_column_is_rejected() -> None:
    plan = baseline_plan()
    plan["mappings"] = [*plan["mappings"], {"source_column": "nonexistent", "target_column": "x"}]
    assert not findings_for(candidate(plan=plan))[GateKind.FIELD_TOPOLOGY].passed


def test_unknown_target_column_is_rejected_when_table_exists() -> None:
    plan = baseline_plan()
    plan["mappings"] = [*plan["mappings"], {"source_column": "id", "target_column": "not_a_column"}]
    assert not findings_for(candidate(plan=plan))[GateKind.FIELD_TOPOLOGY].passed


def test_duplicate_target_column_is_rejected() -> None:
    plan = baseline_plan()
    plan["mappings"] = [*plan["mappings"], {"source_column": "id", "target_column": "amount"}]
    assert not findings_for(candidate(plan=plan))[GateKind.FIELD_TOPOLOGY].passed


def test_undeclared_transform_reference_is_rejected() -> None:
    plan = baseline_plan()
    plan["mappings"][1]["transforms"] = ["not_declared"]
    assert not findings_for(candidate(plan=plan))[GateKind.FIELD_TOPOLOGY].passed


def test_missing_dedup_for_declared_primary_key_is_rejected() -> None:
    """PRD 主场景 A 里 Agent 应当中断提问的那一类缺口；门禁必须拦住。"""
    plan = baseline_plan(dedup_keys=[])
    finding = findings_for(candidate(plan=plan))[GateKind.FIELD_TOPOLOGY]
    assert not finding.passed
    assert "主键去重" in finding.detail


def test_new_target_table_needs_no_column_check() -> None:
    """目标表尚不存在（新建表）时不校验目标列，但仍校验源列与去重。"""
    report = evaluate(
        candidate(),
        source=SourceFacts(connector_type=ConnectorType.MYSQL, columns=SOURCE_COLUMNS),
        target=TargetFacts(connector_type=ConnectorType.DORIS, columns=None),
    )
    assert report.passed, report.repair_hint()


# -------------------------------------------------------------- 质量错误码
def test_duplicate_error_code_is_rejected() -> None:
    plan = baseline_plan()
    plan["quality_rules"] = [
        {"column": "amount", "kind": "POSITIVE", "error_code": "E_AMOUNT"},
        {"column": "customer_name", "kind": "NOT_NULL", "error_code": "E_AMOUNT"},
    ]
    finding = findings_for(candidate(plan=plan))[GateKind.QUALITY_ERROR_CODES]
    assert not finding.passed
    assert "重复" in finding.detail


def test_error_code_absent_from_config_is_rejected() -> None:
    """规则里声明了错误码、配置里却找不到 —— 分流去向不可追溯。"""
    hocon = baseline_hocon().replace("E_EMAIL", "E_UNRELATED")
    assert not findings_for(candidate(hocon=hocon))[GateKind.QUALITY_ERROR_CODES].passed


# ---------------------------------------------------------------- 增量契约
def test_watermark_mode_requires_watermark_column() -> None:
    plan = baseline_plan(
        incremental={"mode": ScheduleMode.WATERMARK_INCREMENTAL.value, "watermark_column": None}
    )
    finding = findings_for(candidate(plan=plan))[GateKind.INCREMENTAL_CONTRACT]
    assert not finding.passed
    assert "未指定水位列" in finding.detail


def test_watermark_column_must_exist_in_source() -> None:
    plan = baseline_plan(
        incremental={
            "mode": ScheduleMode.WATERMARK_INCREMENTAL.value,
            "watermark_column": "not_a_source_column",
        }
    )
    assert not findings_for(candidate(plan=plan))[GateKind.INCREMENTAL_CONTRACT].passed


def test_batch_mode_must_not_declare_watermark() -> None:
    plan = baseline_plan(
        incremental={"mode": ScheduleMode.BATCH.value, "watermark_column": "updated_at"}
    )
    finding = findings_for(candidate(plan=plan))[GateKind.INCREMENTAL_CONTRACT]
    assert not finding.passed
    assert "契约只声明一半" in finding.detail


# -------------------------------------------------------------- Doris 投影
def test_doris_missing_projection_column_is_rejected() -> None:
    plan = baseline_plan()
    plan["mappings"] = [*plan["mappings"], {"source_column": "id", "target_column": "no_such_col"}]
    assert not findings_for(candidate(plan=plan))[GateKind.DORIS_PROJECTION].passed


def test_doris_target_table_must_appear_in_config() -> None:
    hocon = baseline_hocon(table="some_other_table")
    assert not findings_for(candidate(hocon=hocon))[GateKind.DORIS_PROJECTION].passed


def test_non_doris_target_skips_projection_check() -> None:
    report = evaluate(
        candidate(),
        source=SourceFacts(connector_type=ConnectorType.MYSQL, columns=SOURCE_COLUMNS),
        target=TargetFacts(connector_type=ConnectorType.POSTGRESQL, columns=TARGET_COLUMNS),
    )
    assert report.passed, report.repair_hint()


# ----------------------------------------------------------- 逐行 LLM 契约
def test_row_llm_usage_is_rejected_while_capability_is_off() -> None:
    """FR-33：能力默认**严格关闭**；方案擅自使用必须被拦下。"""
    assert get_settings().row_llm_transform_enabled is False
    plan = baseline_plan(
        transforms=[
            {"name": "mask_email", "kind": "MASK", "target_column": "email_masked"},
            {"name": "summarize", "kind": "ROW_LLM", "target_column": "amount"},
        ]
    )
    finding = findings_for(candidate(plan=plan))[GateKind.ROW_LLM_CONTRACT]
    assert not finding.passed
    assert "严格关闭" in finding.detail


def test_row_llm_flag_without_model_contract_is_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DIP_ROW_LLM_TRANSFORM_ENABLED", "true")
    monkeypatch.setenv("DIP_ROW_LLM_API_KEY", "row-llm-key-0123456789")
    monkeypatch.setenv("DIP_ROW_LLM_ALLOWED_API_HOSTS", "llm.internal")
    get_settings.cache_clear()
    try:
        plan = baseline_plan(row_llm={"enabled": True})
        finding = findings_for(candidate(plan=plan))[GateKind.ROW_LLM_CONTRACT]
        assert not finding.passed
        assert "必须声明模型与提示词引用" in finding.detail
    finally:
        get_settings.cache_clear()


# ------------------------------------------------------------------ 修复提示
def test_repair_hint_lists_every_failing_gate() -> None:
    """修复循环需要**定向**提示：一次把所有失败项说清，而不是只报第一条。"""
    plan = baseline_plan(dedup_keys=[])
    plan["incremental"] = {
        "mode": ScheduleMode.WATERMARK_INCREMENTAL.value,
        "watermark_column": None,
    }
    report = evaluate(
        candidate(plan=plan),
        source=SourceFacts(connector_type=ConnectorType.MYSQL, columns=SOURCE_COLUMNS),
        target=TargetFacts(connector_type=ConnectorType.DORIS, columns=TARGET_COLUMNS),
    )
    hint = report.repair_hint()
    assert hint is not None
    assert GateKind.FIELD_TOPOLOGY.value in hint
    assert GateKind.INCREMENTAL_CONTRACT.value in hint
