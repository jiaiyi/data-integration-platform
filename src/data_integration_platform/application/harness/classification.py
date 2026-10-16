"""字段级数据分类与传播（T-26 / FR-43）。

分类报告是 **PDP 与监督层的共同输入**，所以它必须是**纯函数**：
同一份 Schema 与同一份血缘关系，任何时候都得到逐位相同的报告与摘要
（SPEC T-26 的证据就是「报告摘要冻结且可复现」）。

三条来源按「只升不降」合成，最终取其中最严的一档：

1. **显式声明**（`overrides`）—— 「公开」是业务决定，不是能从字段名猜出来的东西，
   因此只有显式声明才可能得到 `PUBLIC`；
2. **名称规则** —— 按字段名的分词命中关键词表；
3. **血缘传播** —— 派生字段继承其来源字段的分级。派生数据不可能比来源更不敏感，
   所以名称规则给出的较低等级**不能**把一个来自机密字段的派生列拉低。

一条都没命中的字段记 `UNKNOWN` 并进**未解析清单**，由 PDP 按保守等级处理 ——
不按「看起来像 ID」蒙一个等级。名称匹配按分词（`_`、`-`、空格等）做整词相等，
不做子串包含：子串会把 `cardinality` 认成 `card`，方向虽然保守，
但会让「这个字段为什么是受限」变得没法解释。
"""

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from data_integration_platform.domain.harness import (
    DataSensitivity,
    canonical_sha256,
    sensitivity_rank,
    stricter_of,
)

_TOKEN_SPLIT = re.compile(r"[^a-z0-9]+")


@dataclass(frozen=True, slots=True)
class ClassificationRule:
    """一条名称规则；`rule_id` 进报告，方便回答「这条为什么是机密」。"""

    rule_id: str
    sensitivity: DataSensitivity
    keywords: frozenset[str]


# 关键词一律小写、按分词命中；规则表按敏感度从高到低，命中即止。
NAME_RULES: tuple[ClassificationRule, ...] = (
    ClassificationRule(
        "SECRET_CREDENTIAL",
        DataSensitivity.SECRET,
        frozenset({"password", "passwd", "pwd", "secret", "token", "apikey", "privatekey"}),
    ),
    ClassificationRule(
        "RESTRICTED_PERSONAL",
        DataSensitivity.RESTRICTED,
        frozenset(
            {
                "idcard",
                "idnumber",
                "passport",
                "bankaccount",
                "cardnumber",
                "creditcard",
                "salary",
                "medical",
                "diagnosis",
            }
        ),
    ),
    ClassificationRule(
        "CONFIDENTIAL_CONTACT",
        DataSensitivity.CONFIDENTIAL,
        frozenset(
            {
                "email",
                "mail",
                "phone",
                "mobile",
                "tel",
                "telephone",
                "address",
                "name",
                "birthday",
                "birthdate",
                "birth",
                "gender",
            }
        ),
    ),
    ClassificationRule(
        "INTERNAL_BUSINESS",
        DataSensitivity.INTERNAL,
        frozenset(
            {
                "amount",
                "price",
                "fee",
                "cost",
                "quantity",
                "count",
                "total",
                "balance",
                "revenue",
                "margin",
                "status",
                "state",
                "region",
                "level",
                "category",
                "type",
            }
        ),
    ),
)


@dataclass(frozen=True, slots=True)
class FieldClassification:
    """一个字段的分类结论；`rule_id` 与 `propagated_from` 至少一个非空才算「已解析」。

    `propagated_from` 记的是**全部传递来源**（不只直接父列）：出现「它为什么比名称
    规则更高」的疑问时，能一路看到源头是哪一列。
    """

    name: str
    sensitivity: DataSensitivity
    rule_id: str | None = None
    propagated_from: tuple[str, ...] = ()

    def resolved(self) -> bool:
        return self.rule_id is not None or bool(self.propagated_from)

    def canonical_payload(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "sensitivity": self.sensitivity.value,
            "rule_id": self.rule_id,
            "propagated_from": sorted(self.propagated_from),
        }


@dataclass(frozen=True, slots=True)
class ClassificationReport:
    """冻结的分类报告；`artifact_sha256` 把它绑到具体版本上。"""

    artifact_sha256: str
    overall: DataSensitivity
    fields: tuple[FieldClassification, ...]

    @property
    def unresolved(self) -> tuple[str, ...]:
        """未解析清单：既没命中规则、也没有血缘的字段，按 `UNKNOWN` 保守处理。"""
        return tuple(field.name for field in self.fields if not field.resolved())

    def classification_of(self, name: str) -> DataSensitivity:
        """报告里没有的字段按 `UNKNOWN` 处理，而不是当成公开。"""
        for field in self.fields:
            if field.name == name:
                return field.sensitivity
        return DataSensitivity.UNKNOWN

    def canonical_payload(self) -> dict[str, Any]:
        return {
            "artifact_sha256": self.artifact_sha256,
            "overall": self.overall.value,
            # 字段顺序在算摘要前固定下来，否则同样的内容会因为遍历顺序不同而摘要不同。
            "fields": [
                field.canonical_payload()
                for field in sorted(self.fields, key=lambda item: item.name)
            ],
        }

    def sha256(self) -> str:
        """报告摘要；冻结进准备单与能力令牌的就是这个值。"""
        return canonical_sha256(self.canonical_payload())


def classify(
    columns: Sequence[Mapping[str, Any]],
    *,
    artifact_sha256: str,
    lineage: Mapping[str, Sequence[str]] | None = None,
    overrides: Mapping[str, DataSensitivity] | None = None,
) -> ClassificationReport:
    """给一份 Schema 出分类报告。

    - `columns`：Profile 落库的字段清单（`[{"name": ...}]`），与门禁同一形状；
    - `lineage`：派生字段 → 它的来源字段；缺失表示这个字段不是派生出来的；
    - `overrides`：人工声明的分级，与名称规则、血缘一起参与「取最严」。
    """
    declarations = dict(overrides or {})
    ancestors = _expand_lineage(lineage or {})

    fields: list[FieldClassification] = []
    for column in columns:
        raw_name = column.get("name")
        if not raw_name:
            continue
        name = str(raw_name)
        sources = sorted(ancestors.get(name, ()))
        fields.append(
            _classify_field(
                name=name,
                declared=declarations.get(name),
                sources=sources,
                declarations=declarations,
            )
        )
    return ClassificationReport(
        artifact_sha256=artifact_sha256,
        overall=_overall(fields),
        fields=tuple(fields),
    )


def _classify_field(
    *,
    name: str,
    declared: DataSensitivity | None,
    sources: Sequence[str],
    declarations: Mapping[str, DataSensitivity],
) -> FieldClassification:
    rule = _match_rule(name)
    # 声明与名称规则是同一类「本字段自身的分级」；声明优先，因为没有声明时才轮到规则。
    own = declared if declared is not None else (rule.sensitivity if rule is not None else None)
    inherited = _ancestors_sensitivity(sources, declarations)

    if own is None and inherited is None:
        return FieldClassification(name=name, sensitivity=DataSensitivity.UNKNOWN)

    sensitivity = stricter_of(*[value for value in (own, inherited) if value is not None])
    # 血缘把等级抬起来时才留痕来源；否则「它为什么比名称规则更高」说不清。
    raised = inherited is not None and (
        own is None or sensitivity_rank(inherited) > sensitivity_rank(own)
    )
    return FieldClassification(
        name=name,
        sensitivity=sensitivity,
        rule_id=rule.rule_id if rule is not None and declared is None else None,
        propagated_from=tuple(sorted(sources)) if raised else (),
    )


def _match_rule(name: str) -> ClassificationRule | None:
    """按分词整词匹配，另外把分词拼回去再匹配一次。

    拼接那次是必需的：`id_card`、`card_number` 是最常见的写法，而关键词表里记的是
    `idcard`、`cardnumber` 这类连写形式。只做整词匹配会漏掉带分隔符的真实列名，
    只做拼接匹配又会把 `cardinality` 拼成 `cardinality` —— 正好不命中 `card`，
    所以两者都要，且都要求**完整相等**，不做子串包含。
    """
    parts = [part for part in _TOKEN_SPLIT.split(name.casefold()) if part]
    tokens = set(parts)
    joined = "".join(parts)
    for rule in NAME_RULES:
        if tokens & rule.keywords or joined in rule.keywords:
            return rule
    return None


def _expand_lineage(parents: Mapping[str, Sequence[str]]) -> dict[str, frozenset[str]]:
    """把直接血缘展开成传递闭包：A ← B ← C 时，A 也要继承 C 的分级。

    不做传递展开的话，「先脱敏再改名」这类多跳派生会悄悄丢掉源头分级。
    """
    expanded: dict[str, frozenset[str]] = {}
    for child in parents:
        seen: set[str] = set()
        stack = list(parents.get(child, ()))
        while stack:
            parent = stack.pop()
            if parent in seen or parent == child:
                continue
            seen.add(parent)
            stack.extend(parents.get(parent, ()))
        expanded[child] = frozenset(seen)
    return expanded


def _ancestors_sensitivity(
    names: Sequence[str], declarations: Mapping[str, DataSensitivity]
) -> DataSensitivity | None:
    """来源字段中最高的一档；来源可能不在本 Schema 里（例如目标端看不到的源列）。"""
    values: list[DataSensitivity] = []
    for name in names:
        declared = declarations.get(name)
        if declared is not None:
            values.append(declared)
            continue
        rule = _match_rule(name)
        if rule is not None:
            values.append(rule.sensitivity)
    return stricter_of(*values) if values else None


def _overall(fields: Sequence[FieldClassification]) -> DataSensitivity:
    """整份 Schema 的分级取最高的字段。

    因为 `UNKNOWN` 的序位排在「受限」之上，**只要还有没分类的字段，整表就是 UNKNOWN**。
    这是刻意的保守：连一半列是什么都不知道，却说「这张表只是机密级」，那是假保证。
    想看清楚还差哪些列，看 `unresolved` —— 那条信息没有被丢掉，只是没有混进分级里。
    """
    if not fields:
        return DataSensitivity.UNKNOWN
    return stricter_of(*(field.sensitivity for field in fields))
