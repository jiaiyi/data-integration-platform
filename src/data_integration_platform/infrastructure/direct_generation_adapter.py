"""直连生成后端：OpenAI 兼容协议（T-18 / FR-28）。

只负责「把提示词发给模型、把原始文本拿回来」。**结构解析与校验不在这里** ——
候选是否可信由 `application/generation.parse_candidate` 统一裁决，
这样换一个后端不会顺带换掉校验口径。

平台主动外发，因此与其它外发一致：不走系统代理（否则 API Key 会被交给未知中间人）。
生成端点来自部署配置（可信），故不需要出口白名单；逐行 LLM Transform 的出口才需要
白名单（FR-33），因为那是按管道配置的。
"""

from typing import Any

import httpx

from data_integration_platform.application.generation import GenerationPort, GenerationRequest
from data_integration_platform.config import Settings, get_settings
from data_integration_platform.domain.errors import (
    DomainError,
    ExternalServiceUnavailable,
)

_CHAT_COMPLETIONS_PATH = "/chat/completions"

SYSTEM_PROMPT = """你是企业数据集成平台的方案生成器。你只输出 JSON，不输出任何解释文字。

输出必须是**单个 JSON 对象**，形如：
{
  "plan": { ...结构化计划... },
  "hocon": "SeaTunnel HOCON 配置文本"
}

plan 的字段（全部必填，除非标注可选）：
- source: {schema_name, table_name}           源端库表
- target: {schema_name, table_name, primary_key: [字符串]}  目标端库表
- mappings: [{source_column, target_column, target_type?, transforms: [转换步骤名]}]  至少一条
- transforms: [{name, kind, target_column?, expression?, config?}]  kind 可用值见下
- quality_rules: [{column, kind, error_code, params?}]  error_code 必须形如 E_XXX 且唯一
- incremental: {mode, watermark_column?}          mode 为 BATCH 或 WATERMARK_INCREMENTAL
- budget: {max_records?, max_bytes?, timeout_seconds?}   可选，只能收紧平台上限
- row_llm: {enabled, model?, prompt_ref?}          逐行 LLM，默认必须为 false
- dedup_keys: [字符串]                             目标表有主键时必须给出

强制规则（违反会被确定性门禁拒绝）：
1. **只用给定的源端字段**写 mappings，不得臆造字段名。
2. 目标表有主键时**必须**给出 dedup_keys，否则方案不完整。
3. 每条质量规则都要有唯一的 error_code，且该错误码必须出现在 hocon 里。
4. WATERMARK_INCREMENTAL 模式必须给出 watermark_column，且该列要在源端字段中。
5. **hocon 里绝不能出现明文口令**，只能用上下文「可用凭据占位符」里**列出的那些名字**；
   自己编的名字会被平台拒绝（执行阶段没有对应凭据可替换）。
6. 逐行 LLM Transform（kind=ROW_LLM）默认关闭，不要主动使用。
7. hocon 必须是**单一闭合配置块**：env / source / transform / sink 各一段，花括号配平。

transforms 的 kind 可用值：CAST、MASK、FILTER、DEDUP、CONCAT、EXPRESSION、ROW_LLM。
"""


class DirectGenerationAdapter:
    """直连生成后端；OpenAI 兼容的 `/chat/completions`。"""

    backend_name = "direct"

    def __init__(self, settings: Settings | None = None) -> None:
        self._settings = settings

    @property
    def settings(self) -> Settings:
        return self._settings if self._settings is not None else get_settings()

    async def generate(self, request: GenerationRequest) -> str:
        """返回模型原始响应文本；不在这里做结构校验。"""
        settings = self.settings
        if settings.llm_base_url is None or settings.llm_api_key is None or not settings.llm_model:
            # 外部依赖未就绪时明确报告，而不是静默失败（PRD §5.3）。
            raise ExternalServiceUnavailable("生成能力未就绪：未配置 LLM 接入信息")

        url = f"{str(settings.llm_base_url).rstrip('/')}{_CHAT_COMPLETIONS_PATH}"
        payload: dict[str, Any] = {
            "model": settings.llm_model,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": _user_prompt(request)},
            ],
            # 方案生成要可复现：温度取 0，减少同一需求产出不同结构的概率。
            "temperature": 0,
            "stream": False,
        }
        headers = {
            "Authorization": f"Bearer {settings.llm_api_key.get_secret_value()}",
            "Content-Type": "application/json",
        }
        try:
            async with httpx.AsyncClient(
                timeout=settings.llm_request_timeout_seconds,
                # 不走系统代理：API Key 不能被交给未知中间人。
                trust_env=False,
            ) as client:
                response = await client.post(url, json=payload, headers=headers)
        except httpx.HTTPError as exc:
            raise ExternalServiceUnavailable(f"生成请求失败：{type(exc).__name__}") from exc

        if response.status_code >= 400:
            # 只回显状态码与截断后的正文；密钥只走请求头，不会出现在正文里。
            raise ExternalServiceUnavailable(
                f"生成服务返回 {response.status_code}：{_short(response.text)}"
            )
        parsed = response.json()
        if not isinstance(parsed, dict):
            raise ExternalServiceUnavailable("生成服务返回的不是 JSON 对象")
        return _extract_message_content(parsed)


def _user_prompt(request: GenerationRequest) -> str:
    sections = [
        f"# 需求\n{request.requirement}",
        f"# 源端事实\n{request.source_summary}",
        f"# 目标端事实\n{request.target_summary}",
    ]
    if request.profile_context:
        sections.append(f"# 受管元数据事实（字段与脱敏样本）\n{request.profile_context}")
    if request.repair_hint:
        sections.append(
            f"# 上一份候选被门禁拒绝的原因（必须修正这些点，其余保持不变）\n{request.repair_hint}"
        )
    sections.append("# 输出\n只输出那一个 JSON 对象。")
    return "\n\n".join(sections)


def _extract_message_content(payload: dict[str, Any]) -> str:
    """从 OpenAI 兼容响应里取正文；结构不符时明确报错，不猜。"""
    choices = payload.get("choices")
    if not isinstance(choices, list) or not choices:
        raise ExternalServiceUnavailable("生成服务返回里没有 choices")
    first = choices[0]
    message = first.get("message") if isinstance(first, dict) else None
    content = message.get("content") if isinstance(message, dict) else None
    if not isinstance(content, str) or not content.strip():
        raise DomainError("生成服务返回了空内容")
    return content


def _short(text: str, *, limit: int = 300) -> str:
    collapsed = " ".join(text.split())
    return collapsed if len(collapsed) <= limit else f"{collapsed[:limit]}…"


def create_generation_adapter(*, settings: Settings | None = None) -> GenerationPort:
    """按配置构造生成后端；未接入的后端明确报错（FR-28 的切换点）。"""
    resolved = settings if settings is not None else get_settings()
    if resolved.generation_backend == "direct":
        return DirectGenerationAdapter(resolved)
    if resolved.generation_backend == "seatunnel-cli":
        raise DomainError(
            "seatunnel-cli 生成后端尚未接入；该后端只改变「候选如何产生」，"
            "不影响门禁、版本、审批与 Harness 行为"
        )
    raise DomainError(f"未支持的生成后端：{resolved.generation_backend}")
