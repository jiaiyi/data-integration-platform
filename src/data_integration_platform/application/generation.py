"""生成契约与候选结构校验（T-18 / FR-29、FR-30）。

模型产出的是**一个 JSON 对象**，含两块：
- `plan`：结构化 EtlPlan（源/目标、字段映射、转换、质量规则、增量契约、预算）；
- `hocon`：基于该 plan 写出的 SeaTunnel 配置文本。

用 `plan` 与 `hocon` **互为交叉检查**：门禁既看结构化字段，也看配置文件本身，
避免「plan 说一套、配置写另一套」。结构闭合校验在模型输出进入系统的最外层执行，
不合格的候选根本进不到门禁（PRD FR-31：候选完整性失败也消耗同一份修复预算）。
"""

import json
import re
from collections.abc import Mapping, Sequence
from typing import Any, Protocol

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from data_integration_platform.config import get_settings
from data_integration_platform.domain.enums import ScheduleMode
from data_integration_platform.domain.errors import DomainError

# 模型有时会把 JSON 包在 ```json 围栏里；这里容忍围栏，但不容忍多块内容。
_JSON_FENCE = re.compile(r"```(?:json)?\s*(?P<body>.*?)```", re.DOTALL | re.IGNORECASE)
# HOCON 里不允许出现内联明文凭据（FR-17：只允许 Secret 占位符）。
# 正则只负责「把敏感键的引号取值抓出来」，是否属于占位符由 `_SECRET_PLACEHOLDER` 判定 ——
# 把判定写进正则的负向前瞻会让它既难读又容易漏。
_INLINE_SECRET = re.compile(
    r"(?i)\b(password|passwd|pwd|secret|token|api[_-]?key|access[_-]?key|secret[_-]?key)\b"
    r"\s*[:=]\s*\"([^\"]{0,512})\""
)
# 允许的 Secret 占位符形态：`${SECRET_REF}`、`{{...}}`、`secret://`、`vault://`、`${env:...}`。
_SECRET_PLACEHOLDER = re.compile(
    r"^(?:\$\{[A-Za-z0-9_:.-]+\}|\{\{[^}]+\}\}|secret://.+|vault://.+)$"
)


class FieldMapping(BaseModel):
    """一个字段的源→目标映射；`transform` 只允许引用已声明的转换步骤名。"""

    model_config = ConfigDict(extra="forbid")

    source_column: str = Field(min_length=1, max_length=255)
    target_column: str = Field(min_length=1, max_length=255)
    target_type: str | None = Field(default=None, max_length=128)
    transforms: list[str] = Field(default_factory=list)


class TransformStep(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=128)
    kind: str = Field(min_length=1, max_length=64)
    target_column: str | None = Field(default=None, max_length=255)
    expression: str | None = Field(default=None, max_length=2000)
    config: dict[str, Any] = Field(default_factory=dict)


class QualityRule(BaseModel):
    """质量规则：每条都必须带可追溯的错误码（FR-68 / 门禁第 4 项）。"""

    model_config = ConfigDict(extra="forbid")

    column: str = Field(min_length=1, max_length=255)
    kind: str = Field(min_length=1, max_length=64)
    error_code: str = Field(min_length=1, max_length=64, pattern=r"^[A-Z][A-Z0-9_]*$")
    params: dict[str, Any] = Field(default_factory=dict)


class IncrementalContract(BaseModel):
    """增量契约：水位模式必须给出水位列，否则门禁拒绝（门禁第 5 项）。"""

    model_config = ConfigDict(extra="forbid")

    mode: ScheduleMode
    watermark_column: str | None = Field(default=None, max_length=255)
    watermark_window_end: str | None = Field(default=None, max_length=64)


class RuntimeBudgetSpec(BaseModel):
    """Plan 可以收紧平台预算，但 Commit 时不能临时放宽（FR-75）。"""

    model_config = ConfigDict(extra="forbid")

    max_records: int | None = Field(default=None, ge=1)
    max_bytes: int | None = Field(default=None, ge=1)
    timeout_seconds: int | None = Field(default=None, ge=1)


class RowLlmContract(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool = False
    model: str | None = Field(default=None, max_length=255)
    prompt_ref: str | None = Field(default=None, max_length=512)


class SourceSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    connection_id: str | None = Field(default=None, max_length=64)
    schema_name: str = Field(min_length=1, max_length=255)
    table_name: str = Field(min_length=1, max_length=255)


class TargetSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    connection_id: str | None = Field(default=None, max_length=64)
    schema_name: str = Field(min_length=1, max_length=255)
    table_name: str = Field(min_length=1, max_length=255)
    primary_key: list[str] = Field(default_factory=list)


class EtlPlan(BaseModel):
    """结构化计划；字段都由 Schema 约束，模型不能自由发挥结构（FR-29）。"""

    model_config = ConfigDict(extra="forbid")

    source: SourceSpec
    target: TargetSpec
    mappings: list[FieldMapping] = Field(min_length=1)
    transforms: list[TransformStep] = Field(default_factory=list)
    quality_rules: list[QualityRule] = Field(default_factory=list)
    incremental: IncrementalContract
    budget: RuntimeBudgetSpec = Field(default_factory=RuntimeBudgetSpec)
    row_llm: RowLlmContract = Field(default_factory=RowLlmContract)
    dedup_keys: list[str] = Field(default_factory=list)


class GeneratedCandidate(BaseModel):
    """一次生成的候选：结构化计划 + 配置文本 + 未经校验的原始响应摘要。"""

    model_config = ConfigDict(extra="forbid")

    plan: EtlPlan
    hocon: str = Field(min_length=1, max_length=200_000)

    def canonical_payload(self) -> dict[str, Any]:
        """冻结时用于计算摘要的规范载荷；此处不涉及凭据，可安全参与指纹。"""
        return {
            "plan": self.plan.model_dump(mode="json"),
            "hocon": self.hocon,
        }


class GenerationPort(Protocol):
    """生成后端端口；切换后端只改变「候选如何产生」（FR-28）。"""

    backend_name: str

    async def generate(self, request: "GenerationRequest") -> str:
        """返回模型原始响应文本；结构解析与校验在 `parse_candidate` 里统一做。"""
        ...


class GenerationRequest(BaseModel):
    """一次生成请求的输入；Profile 事实以**文本摘要**形式给出，不含原始业务行。"""

    model_config = ConfigDict(extra="forbid")

    requirement: str = Field(min_length=1, max_length=20_000)
    source_summary: str = Field(min_length=1)
    target_summary: str = Field(min_length=1)
    profile_context: str = Field(default="")
    repair_hint: str | None = None


def parse_candidate(raw: str) -> GeneratedCandidate:
    """把模型原始响应解析成候选；结构不闭合即失败，且失败原因可读。

    三类结构问题在进入门禁之前就被挡住（T-18 的「候选完整性」）：
    截断的 JSON、缺失的必填块、以及非单块的 HOCON。
    """
    payload = _extract_json_object(raw)
    try:
        parsed = json.loads(payload)
    except json.JSONDecodeError as exc:
        raise DomainError(f"候选不是合法 JSON：第 {exc.lineno} 行 {exc.msg}") from exc
    if not isinstance(parsed, dict):
        raise DomainError("候选取必须是 JSON 对象")
    try:
        candidate = GeneratedCandidate.model_validate(parsed)
    except ValidationError as exc:
        raise DomainError(f"候选结构不符合 EtlPlan 契约：{_first_error(exc)}") from exc
    _require_single_hocon_block(candidate.hocon)
    return candidate


def require_no_inline_secrets(hocon: str) -> None:
    """门禁第 2 项：配置里只允许 Secret 占位符，不允许内联明文（FR-17）。"""
    for match in _INLINE_SECRET.finditer(hocon):
        value = match.group(2)
        if _SECRET_PLACEHOLDER.match(value):
            continue
        raise DomainError(f"HOCON 中出现内联明文凭据（{match.group(1)}），必须改为 Secret 占位符")


def _extract_json_object(raw: str) -> str:
    """取出 JSON 主体：先剥代码围栏，再截取最外层对象。

    容忍模型夹带的说明文字（这是常态，不该因此判候选无效）；**不**容忍截断与多对象 ——
    这两种会被 `json.loads` 挡下（缺右括号报解析错误，多对象报 Extra data）。
    结构是否可信由解析结果裁决，而不是靠数围栏。
    """
    text = raw.strip()
    fenced = _JSON_FENCE.search(text)
    if fenced is not None:
        text = fenced.group("body").strip()
    start = text.find("{")
    end = text.rfind("}")
    if start < 0 or end <= start:
        raise DomainError("候选里找不到完整的 JSON 对象（可能被截断）")
    return text[start : end + 1]


def _require_single_hocon_block(hocon: str) -> None:
    """HOCON 必须是单一闭合块：花括号/方括号配平，且不能出现第二份配置。"""
    text = hocon.strip()
    if not text:
        raise DomainError("HOCON 内容为空")
    if text.count("env {") + text.count("env{") > 1 or text.count("source {") > 1:
        raise DomainError("HOCON 包含多份配置块，只允许单块")
    for opener, closer, label in (("{", "}", "花括号"), ("[", "]", "方括号")):
        depth = 0
        for char in text:
            if char == opener:
                depth += 1
            elif char == closer:
                depth -= 1
                if depth < 0:
                    raise DomainError(f"HOCON 的{label}不配平")
        if depth != 0:
            raise DomainError(f"HOCON 的{label}不配平（可能被截断）")


def _first_error(exc: ValidationError) -> str:
    first = exc.errors()[0]
    location = ".".join(str(part) for part in first["loc"]) or "<root>"
    return f"{location}: {first['msg']}"


def generation_readiness() -> tuple[str, bool]:
    """返回（后端名, 是否就绪），供 `/health` 明确报告「生成能力未就绪」（PRD §5.3）。"""
    settings = get_settings()
    ready = settings.llm_base_url is not None and settings.llm_api_key is not None
    return settings.generation_backend, ready


def candidate_budget_candidates() -> int:
    """候选总量上限 = 首次 1 份 + 修复次数（PRD 决策 Q1）。"""
    return 1 + get_settings().max_enterprise_repair_attempts


def collect_quality_error_codes(plan: EtlPlan) -> Sequence[str]:
    return [rule.error_code for rule in plan.quality_rules]


def index_transforms(plan: EtlPlan) -> Mapping[str, TransformStep]:
    """按名称索引转换步骤，供门禁检查字段映射引用的是否为已声明的步骤。"""
    return {step.name: step for step in plan.transforms}
