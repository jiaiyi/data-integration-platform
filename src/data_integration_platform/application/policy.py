"""确定性静态门禁（T-20 / FR-34）。

七项校验逐一给出结论，**任一项失败即不允许冻结**；失败项要能说清「哪一项、为什么」，
好让修复循环有针对性的输入，而不是笼统地重试。

这里全部是纯计算：不调用模型、不连数据源、不写库。因此它可以脱离任何外部依赖被穷举测试 ——
这也是「模型不可信、边界在确定性代码」这条设计要在这一层兑现的地方（ADR-06）。
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from data_integration_platform.application.generation import (
    EtlPlan,
    GeneratedCandidate,
    require_no_inline_secrets,
)
from data_integration_platform.config import get_settings
from data_integration_platform.domain.enums import ConnectorType, ScheduleMode
from data_integration_platform.domain.errors import DomainError

# 逐行 LLM Transform 的规范化写法；模型若用别的名字，门禁不认。
ROW_LLM_TRANSFORM_KINDS = frozenset({"ROW_LLM"})

# **引擎真能搬运**的连接器白名单。
# 注意 REST_API 与 LOCAL_FILE 的差别：受管文件可由 LocalFile Connector 读取，
# 而 REST API 目前只支持「登记与探查」（FR-103），不能作为管道的端点 ——
# 把它放进管道会被这里拦下，而不是等到执行阶段才失败。
ENGINE_MOVABLE_CONNECTORS = frozenset(
    {
        ConnectorType.MYSQL,
        ConnectorType.POSTGRESQL,
        ConnectorType.ORACLE,
        ConnectorType.DORIS,
        ConnectorType.CLICKHOUSE,
        ConnectorType.SQL_SERVER,
        ConnectorType.HIVE,
        ConnectorType.HDFS,
        ConnectorType.S3_MINIO,
        ConnectorType.LOCAL_FILE,
    }
)

# 可作为写入端的连接器；HDFS / S3 也可作为目标，但 Doris 是原子发布的唯一实现（ADR-05）。
WRITABLE_CONNECTORS = ENGINE_MOVABLE_CONNECTORS - {ConnectorType.HIVE}


class GateKind(StrEnum):
    CONNECTOR_ALLOWLIST = "CONNECTOR_ALLOWLIST"
    SECRET_PLACEHOLDER = "SECRET_PLACEHOLDER"
    FIELD_TOPOLOGY = "FIELD_TOPOLOGY"
    QUALITY_ERROR_CODES = "QUALITY_ERROR_CODES"
    INCREMENTAL_CONTRACT = "INCREMENTAL_CONTRACT"
    DORIS_PROJECTION = "DORIS_PROJECTION"
    ROW_LLM_CONTRACT = "ROW_LLM_CONTRACT"
    # 真实引擎检查（T-21）：由引擎检查器**追加**到报告里，不属于下面那七项确定性检查。
    # 单独成一类是因为状态机需要区分「确定性失败」与「引擎失败」——
    # 两者的修复提示措辞不同，虽然它们共享同一份候选预算。
    ENGINE_CHECK = "ENGINE_CHECK"


# 确定性门禁的七项；`evaluate()` 只产出这些，引擎检查由调用方追加。
DETERMINISTIC_GATE_KINDS = frozenset(kind for kind in GateKind if kind is not GateKind.ENGINE_CHECK)


@dataclass(frozen=True, slots=True)
class GateFinding:
    kind: GateKind
    passed: bool
    detail: str


@dataclass(frozen=True, slots=True)
class GateReport:
    findings: tuple[GateFinding, ...]

    def engine_failed(self) -> bool:
        """真实引擎检查是否未通过（T-21）；据此区分修复提示的措辞。"""
        return any(item.kind is GateKind.ENGINE_CHECK and not item.passed for item in self.findings)

    @property
    def passed(self) -> bool:
        return all(finding.passed for finding in self.findings)

    def failing(self) -> tuple[GateFinding, ...]:
        return tuple(finding for finding in self.findings if not finding.passed)

    def repair_hint(self) -> str | None:
        """给修复循环的定向提示；全部通过时返回 `None`。"""
        failing = self.failing()
        if not failing:
            return None
        return "；".join(f"[{item.kind.value}] {item.detail}" for item in failing)


@dataclass(frozen=True, slots=True)
class SourceFacts:
    """源端事实：连接器类型 + Profile 给出的字段清单。"""

    connector_type: ConnectorType
    columns: frozenset[str]


@dataclass(frozen=True, slots=True)
class TargetFacts:
    """目标端事实；`columns` 为 `None` 表示目标表尚不存在（新建表）。"""

    connector_type: ConnectorType
    columns: frozenset[str] | None = None


def evaluate(
    candidate: GeneratedCandidate, *, source: SourceFacts, target: TargetFacts
) -> GateReport:
    """跑完七项门禁并返回逐项结论。"""
    plan = candidate.plan
    return GateReport(
        findings=(
            _connector_allowlist(source=source, target=target),
            _secret_placeholder(candidate),
            _field_topology(plan=plan, source=source, target=target),
            _quality_error_codes(plan=plan, hocon=candidate.hocon),
            _incremental_contract(plan=plan, source=source),
            _doris_projection(plan=plan, target=target, hocon=candidate.hocon),
            _row_llm_contract(plan=plan),
        )
    )


# ------------------------------------------------------------------ 逐项实现
def _connector_allowlist(*, source: SourceFacts, target: TargetFacts) -> GateFinding:
    unsupported = [
        endpoint.connector_type.value
        for endpoint in (source, target)
        if endpoint.connector_type not in ENGINE_MOVABLE_CONNECTORS
    ]
    if unsupported:
        return GateFinding(
            GateKind.CONNECTOR_ALLOWLIST,
            False,
            f"连接器不在引擎可搬运白名单内：{'、'.join(unsupported)}",
        )
    if target.connector_type not in WRITABLE_CONNECTORS:
        return GateFinding(
            GateKind.CONNECTOR_ALLOWLIST,
            False,
            f"{target.connector_type.value} 不能作为写入端",
        )
    return GateFinding(GateKind.CONNECTOR_ALLOWLIST, True, "源端与目标端连接器均在白名单内")


def _secret_placeholder(candidate: GeneratedCandidate) -> GateFinding:
    try:
        require_no_inline_secrets(candidate.hocon)
    except DomainError as exc:
        return GateFinding(GateKind.SECRET_PLACEHOLDER, False, str(exc))
    return GateFinding(GateKind.SECRET_PLACEHOLDER, True, "配置中只出现 Secret 占位符")


def _field_topology(*, plan: EtlPlan, source: SourceFacts, target: TargetFacts) -> GateFinding:
    problems: list[str] = []
    declared = {step.name for step in plan.transforms}
    seen_targets: set[str] = set()

    for mapping in plan.mappings:
        if mapping.source_column not in source.columns:
            problems.append(f"源字段不存在：{mapping.source_column}")
        if mapping.target_column in seen_targets:
            problems.append(f"目标字段重复映射：{mapping.target_column}")
        seen_targets.add(mapping.target_column)
        missing_steps = [name for name in mapping.transforms if name not in declared]
        if missing_steps:
            problems.append(
                f"{mapping.target_column} 引用了未声明的转换：{'、'.join(missing_steps)}"
            )
    if target.columns is not None:
        unknown = sorted(seen_targets - target.columns)
        if unknown:
            problems.append(f"目标字段不存在：{'、'.join(unknown)}")

    # 主键去重：目标声明了主键却没有去重依据，就会写出重复行。
    # 这正是 PRD 主场景 A 里 Agent 应当**中断提问**的那一类缺口。
    if plan.target.primary_key and not plan.dedup_keys:
        problems.append("目标表声明了主键，但方案未给出主键去重逻辑（dedup_keys）")
    for key in plan.dedup_keys:
        if key not in seen_targets and key not in source.columns:
            problems.append(f"去重键不在映射或源字段中：{key}")

    if problems:
        return GateFinding(GateKind.FIELD_TOPOLOGY, False, "；".join(problems))
    return GateFinding(GateKind.FIELD_TOPOLOGY, True, "字段与转换引用拓扑一致")


def _quality_error_codes(*, plan: EtlPlan, hocon: str) -> GateFinding:
    codes = [rule.error_code for rule in plan.quality_rules]
    if len(codes) != len(set(codes)):
        duplicated = sorted({code for code in codes if codes.count(code) > 1})
        return GateFinding(
            GateKind.QUALITY_ERROR_CODES,
            False,
            f"质量错误码重复：{'、'.join(duplicated)}（错误码必须唯一才能分流溯源）",
        )
    # 可追溯：规则里声明的错误码必须能在配置里找到，否则分流与规则对不上。
    untraceable = sorted({code for code in codes if code not in hocon})
    if untraceable:
        return GateFinding(
            GateKind.QUALITY_ERROR_CODES,
            False,
            f"错误码未出现在配置中，无法追溯分流去向：{'、'.join(untraceable)}",
        )
    return GateFinding(
        GateKind.QUALITY_ERROR_CODES, True, f"{len(codes)} 条质量规则错误码唯一且可追溯"
    )


def _incremental_contract(*, plan: EtlPlan, source: SourceFacts) -> GateFinding:
    contract = plan.incremental
    if contract.mode is ScheduleMode.WATERMARK_INCREMENTAL:
        if not contract.watermark_column:
            return GateFinding(GateKind.INCREMENTAL_CONTRACT, False, "水位增量模式未指定水位列")
        if contract.watermark_column not in source.columns:
            return GateFinding(
                GateKind.INCREMENTAL_CONTRACT,
                False,
                f"水位列不在源字段中：{contract.watermark_column}",
            )
        return GateFinding(
            GateKind.INCREMENTAL_CONTRACT, True, f"水位列 {contract.watermark_column} 已在源端确认"
        )
    if contract.watermark_column:
        return GateFinding(
            GateKind.INCREMENTAL_CONTRACT,
            False,
            "全量模式下不应声明水位列（契约只声明一半会让语义含糊）",
        )
    return GateFinding(GateKind.INCREMENTAL_CONTRACT, True, "全量模式，无需水位列")


def _doris_projection(*, plan: EtlPlan, target: TargetFacts, hocon: str) -> GateFinding:
    if target.connector_type is not ConnectorType.DORIS:
        return GateFinding(
            GateKind.DORIS_PROJECTION,
            True,
            f"目标端为 {target.connector_type.value}，Doris 投影检查不适用",
        )
    problems: list[str] = []
    if plan.target.table_name not in hocon:
        problems.append(f"配置中找不到目标表 {plan.target.table_name}")
    if target.columns is not None:
        unknown = sorted({m.target_column for m in plan.mappings} - target.columns)
        if unknown:
            problems.append(f"Doris 目标表缺少投影列：{'、'.join(unknown)}")
    if problems:
        return GateFinding(GateKind.DORIS_PROJECTION, False, "；".join(problems))
    return GateFinding(GateKind.DORIS_PROJECTION, True, "Doris 投影列与配置一致")


def _row_llm_contract(*, plan: EtlPlan) -> GateFinding:
    used = plan.row_llm.enabled or any(
        step.kind.upper() in ROW_LLM_TRANSFORM_KINDS for step in plan.transforms
    )
    if not used:
        return GateFinding(GateKind.ROW_LLM_CONTRACT, True, "未使用逐行 LLM Transform")
    if not get_settings().row_llm_transform_enabled:
        return GateFinding(
            GateKind.ROW_LLM_CONTRACT,
            False,
            "方案使用了逐行 LLM Transform，但平台默认严格关闭该能力（FR-33）",
        )
    if not plan.row_llm.model or not plan.row_llm.prompt_ref:
        return GateFinding(
            GateKind.ROW_LLM_CONTRACT,
            False,
            "启用逐行 LLM Transform 必须声明模型与提示词引用",
        )
    return GateFinding(GateKind.ROW_LLM_CONTRACT, True, "逐行 LLM 契约齐备且能力已开启")


def _column_names(columns: Sequence[Mapping[str, Any]] | None) -> frozenset[str]:
    """Profile 落库的字段清单是 `list[dict]`（见 `ProbeResult.canonical_columns`）。"""
    return frozenset(str(column["name"]) for column in (columns or ()) if column.get("name"))


def with_engine_check(report: GateReport, finding: GateFinding) -> GateReport:
    """把真实引擎检查的结论追加到确定性门禁报告上 —— 两者共享同一份候选预算。"""
    return GateReport(findings=(*report.findings, finding))


def facts_from_profile(
    *, connector_type: ConnectorType, columns: Sequence[Mapping[str, Any]] | None
) -> SourceFacts:
    """把源端 Profile 的落库字段清单转成门禁事实。"""
    return SourceFacts(connector_type=connector_type, columns=_column_names(columns))


def facts_from_target(
    *, connector_type: ConnectorType, columns: Sequence[Mapping[str, Any]] | None
) -> TargetFacts:
    """目标端事实；字段清单为 `None` 表示目标表尚未建立（新建表）。"""
    if columns is None:
        return TargetFacts(connector_type=connector_type, columns=None)
    names = _column_names(columns)
    return TargetFacts(connector_type=connector_type, columns=names or None)
