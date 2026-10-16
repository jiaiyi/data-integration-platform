"""字段级数据分类与传播（T-26 / FR-43）。

证据要求是**报告摘要冻结且可复现**：同一份 Schema 与血缘必须给出逐位相同的报告与摘要，
字段顺序、遍历顺序都不能影响结果。另外守住三条语义：未命中记 UNKNOWN 并进未解析清单、
血缘只升不降、显式声明可以给出公开。

`UNKNOWN` 的保守程度与 PDP 用的是**同一份序位**（`domain/harness.py`），
这里也钉一条，免得两处各写一遍又漂移。
"""

import pytest

from data_integration_platform.application.harness.classification import (
    NAME_RULES,
    ClassificationReport,
    classify,
)
from data_integration_platform.domain.harness import (
    SENSITIVITY_ORDER,
    DataSensitivity,
    sensitivity_rank,
)

ARTIFACT = "a" * 64


def columns(*names: str) -> list[dict[str, str]]:
    return [{"name": name, "type": "varchar"} for name in names]


def report(**overrides: object) -> ClassificationReport:
    values: dict[str, object] = {
        "columns": columns("id", "amount", "email", "customer_name"),
        "artifact_sha256": ARTIFACT,
    }
    values.update(overrides)
    return classify(**values)  # type: ignore[arg-type]


# ------------------------------------------------------------------ 名称规则
def test_name_rules_classify_each_level() -> None:
    result = report(
        columns=columns("password", "id_card", "email", "amount", "order_id"),
    )

    assert result.classification_of("password") is DataSensitivity.SECRET
    assert result.classification_of("id_card") is DataSensitivity.RESTRICTED
    assert result.classification_of("email") is DataSensitivity.CONFIDENTIAL
    assert result.classification_of("amount") is DataSensitivity.INTERNAL
    assert result.classification_of("order_id") is DataSensitivity.UNKNOWN


def test_matching_is_token_exact_not_substring() -> None:
    """`cardinality` 不该被认成 `card`：方向虽保守，但结论要能解释得清。"""
    result = report(columns=columns("cardinality"))

    assert result.classification_of("cardinality") is DataSensitivity.UNKNOWN


def test_multi_token_names_match() -> None:
    result = report(columns=columns("customer_email", "email_masked", "user_name", "id_card"))

    assert result.classification_of("customer_email") is DataSensitivity.CONFIDENTIAL
    assert result.classification_of("email_masked") is DataSensitivity.CONFIDENTIAL
    assert result.classification_of("user_name") is DataSensitivity.CONFIDENTIAL
    # 连写形式的关键词也要能命中带分隔符的真实列名。
    assert result.classification_of("id_card") is DataSensitivity.RESTRICTED


def test_unmatched_fields_are_unknown_and_listed() -> None:
    """分类缺失记 UNKNOWN 并进未解析清单 —— 不按「看起来像 ID」蒙一个等级。"""
    result = report(columns=columns("order_id", "amount", "email"))

    assert result.unresolved == ("order_id",)
    assert result.classification_of("order_id") is DataSensitivity.UNKNOWN


def test_field_absent_from_the_report_is_unknown() -> None:
    """报告里没有的字段不等于公开；查不到就按 UNKNOWN。"""
    result = report(columns=columns("amount"))

    assert result.classification_of("从来没见过的列") is DataSensitivity.UNKNOWN


# ------------------------------------------------------------------ 传播
def test_derived_field_inherits_its_source() -> None:
    """派生列继承来源分级，即使它的名字命中不了任何规则。"""
    result = report(
        columns=columns("email", "contact_blob"),
        lineage={"contact_blob": ["email"]},
    )

    field = next(item for item in result.fields if item.name == "contact_blob")
    assert field.sensitivity is DataSensitivity.CONFIDENTIAL
    assert field.propagated_from == ("email",)
    assert "contact_blob" not in result.unresolved


def test_propagation_never_lowers_a_level() -> None:
    """来源是秘密字段，派生列的名字规则只能把它定到内部 —— 但结论必须取秘密。"""
    result = report(
        columns=columns("password", "amount_copy"),
        lineage={"amount_copy": ["password"]},
    )

    field = next(item for item in result.fields if item.name == "amount_copy")
    assert field.sensitivity is DataSensitivity.SECRET
    assert field.propagated_from == ("password",)


def test_propagation_is_transitive() -> None:
    """A ← B ← C：多跳派生必须继承到最源头的分级，否则「先脱敏再改名」会丢掉来源。"""
    # 中间列刻意避开任何关键词（`email_token` 里的 `token` 本身就命中秘密级规则）。
    result = report(
        columns=columns("email", "email_copy", "final_blob"),
        lineage={"email_copy": ["email"], "final_blob": ["email_copy"]},
    )

    assert result.classification_of("final_blob") is DataSensitivity.CONFIDENTIAL


def test_cycle_in_lineage_does_not_hang() -> None:
    """血缘成环（配置写错或改表历史造成）不能让分类器卡住。"""
    result = report(
        columns=columns("a_blob", "b_blob"),
        lineage={"a_blob": ["b_blob"], "b_blob": ["a_blob"]},
    )

    assert len(result.fields) == 2


def test_derived_from_unknown_source_stays_unresolved() -> None:
    """来源自己都没分级，派生列继承不到东西，仍然算未解析。"""
    result = report(
        columns=columns("mystery", "mystery_copy"),
        lineage={"mystery_copy": ["mystery"]},
    )

    assert set(result.unresolved) == {"mystery", "mystery_copy"}


def test_lineage_to_a_column_outside_the_schema_still_propagates() -> None:
    """来源列不在本次 Schema 里（例如目标端看不到的源列）也要按名字规则传播。"""
    result = report(
        columns=columns("blob"),
        lineage={"blob": ["phone_number"]},
    )

    assert result.classification_of("blob") is DataSensitivity.CONFIDENTIAL


# ------------------------------------------------------------------ 显式声明
def test_declaration_can_lower_a_name_rule_hit() -> None:
    """「公开」是业务决定，只能由显式声明给出；声明也就是最终结论。"""
    result = report(
        columns=columns("region"),
        overrides={"region": DataSensitivity.PUBLIC},
    )

    field = next(item for item in result.fields if item.name == "region")
    assert field.sensitivity is DataSensitivity.PUBLIC
    # 声明是人的判断，不该记成「命中了哪条名称规则」。
    assert field.rule_id is None


def test_declaration_cannot_hide_a_sensitive_lineage() -> None:
    """但声明也不能把来自秘密字段的派生列说成公开。"""
    result = report(
        columns=columns("password", "note"),
        lineage={"note": ["password"]},
        overrides={"note": DataSensitivity.PUBLIC},
    )

    assert result.classification_of("note") is DataSensitivity.SECRET


# ------------------------------------------------------------------ 整体分级
def test_overall_takes_the_strictest_field() -> None:
    """表里有一个秘密字段，这张表就是秘密表。"""
    result = report(columns=columns("amount", "email", "password"))

    assert result.overall is DataSensitivity.SECRET


def test_empty_schema_is_unknown_not_public() -> None:
    result = report(columns=[])

    assert result.overall is DataSensitivity.UNKNOWN
    assert result.unresolved == ()


# ------------------------------------------------------------------ 冻结与复现
def test_report_digest_is_stable_regardless_of_field_order() -> None:
    """同一份 Schema 换一个字段顺序，摘要必须一样 —— 否则「冻结」无从核对。"""
    forward = report(columns=columns("id", "amount", "email"))
    backward = report(columns=columns("email", "amount", "id"))

    assert forward.sha256() == backward.sha256()


def test_report_digest_binds_the_artifact() -> None:
    """报告绑在具体版本上：制品换了，报告摘要必须跟着变。"""
    first = report(artifact_sha256="a" * 64)
    second = report(artifact_sha256="b" * 64)

    assert first.sha256() != second.sha256()


def test_report_digest_changes_with_a_single_field() -> None:
    baseline = report(columns=columns("id", "amount", "email"))
    changed = report(columns=columns("id", "amount", "email", "phone"))

    assert baseline.sha256() != changed.sha256()


def test_classification_is_reproducible() -> None:
    """同一输入两次分类结果逐位相同（PRD G-05 的复现口径）。"""
    first = report()
    second = report()

    assert first.canonical_payload() == second.canonical_payload()
    assert first.sha256() == second.sha256()


# ------------------------------------------------------------------ 与 PDP 共用的序位
def test_unknown_sits_between_restricted_and_secret() -> None:
    """UNKNOWN 比「已知受限」更该被盯着，但不等于秘密：等同秘密会让未分类数据完全出不了门。"""
    assert sensitivity_rank(DataSensitivity.RESTRICTED) < sensitivity_rank(DataSensitivity.UNKNOWN)
    assert sensitivity_rank(DataSensitivity.UNKNOWN) < sensitivity_rank(DataSensitivity.SECRET)
    assert len(SENSITIVITY_ORDER) == len(set(SENSITIVITY_ORDER))


def test_name_rules_are_ordered_from_strict_to_loose() -> None:
    ranks = [sensitivity_rank(rule.sensitivity) for rule in NAME_RULES]
    ids = [rule.rule_id for rule in NAME_RULES]

    assert ranks == sorted(ranks, reverse=True)
    assert len(ids) == len(set(ids))


def test_every_rule_has_keywords() -> None:
    for rule in NAME_RULES:
        assert rule.keywords, rule.rule_id


@pytest.mark.parametrize("sensitivity", list(DataSensitivity))
def test_ranking_covers_every_level(sensitivity: DataSensitivity) -> None:
    """新增分级时必须在序位表里也登记，否则判定会静默用错位置。"""
    assert 0 <= sensitivity_rank(sensitivity) < len(SENSITIVITY_ORDER)
