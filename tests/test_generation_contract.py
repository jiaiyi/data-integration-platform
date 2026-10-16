"""生成契约与直连后端（T-18 / FR-28、FR-29、FR-30）。

两块证据：
1. **候选结构校验**：模型输出进入系统的第一道闸门，截断/多对象/结构不符都必须被挡住；
2. **直连后端**：对 OpenAI 兼容协议的请求构造与响应解析（用本地假服务，不需要真 Key）。
"""

import json
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, ClassVar

import pytest

from data_integration_platform.application.generation import (
    GenerationPort,
    GenerationRequest,
    parse_candidate,
    require_no_inline_secrets,
)
from data_integration_platform.config import Settings
from data_integration_platform.domain.errors import DomainError, ExternalServiceUnavailable
from data_integration_platform.infrastructure.direct_generation_adapter import (
    SYSTEM_PROMPT,
    DirectGenerationAdapter,
    create_generation_adapter,
)

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
    'env { job.mode = "BATCH" }\nsink { Doris { table = "orders_wide" E_NOT_NULL = true } }\n'
)


def valid_payload() -> str:
    return json.dumps({"plan": VALID_PLAN, "hocon": VALID_HOCON}, ensure_ascii=False)


# ------------------------------------------------------------ 候选结构校验
def test_valid_candidate_is_accepted() -> None:
    candidate = parse_candidate(valid_payload())
    assert candidate.plan.target.table_name == "orders_wide"
    assert "Doris" in candidate.hocon


def test_candidate_wrapped_in_markdown_fence_is_accepted() -> None:
    """模型把 JSON 包进围栏是常态，不该因此判无效。"""
    raw = f"```json\n{valid_payload()}\n```"
    assert parse_candidate(raw).plan.source.table_name == "orders"


def test_candidate_with_surrounding_prose_is_accepted() -> None:
    """按需求补充的说明文字同样容忍；结构是否可信由解析结果裁决。"""
    raw = f"好的，方案如下：\n```json\n{valid_payload()}\n```\n以上。"
    assert parse_candidate(raw).plan.source.table_name == "orders"


def test_truncated_candidate_is_rejected() -> None:
    """截断的候选必须被挡住；具体报「找不到完整对象」还是「非法 JSON」取决于切在哪。"""
    raw = valid_payload()[: len(valid_payload()) // 2]
    with pytest.raises(DomainError, match="候选"):
        parse_candidate(raw)


def test_two_json_objects_are_rejected() -> None:
    """两份候选粘在一起：json 解析会因 Extra data 失败。"""
    with pytest.raises(DomainError, match="合法 JSON"):
        parse_candidate(f"{valid_payload()}\n{valid_payload()}")


def test_non_object_root_is_rejected() -> None:
    """顶层是数组时连对象边界都找不到 —— 契约要求的就是单个对象。"""
    with pytest.raises(DomainError, match="找不到完整的 JSON 对象"):
        parse_candidate("[1, 2, 3]")


def test_missing_hocon_is_rejected() -> None:
    raw = json.dumps({"plan": VALID_PLAN})
    with pytest.raises(DomainError, match="hocon"):
        parse_candidate(raw)


def test_unknown_plan_field_is_rejected() -> None:
    """Schema 是闭合的：多出来的字段一律拒绝，避免模型自由发挥结构。"""
    plan = {**VALID_PLAN, "extra_field": 1}
    raw = json.dumps({"plan": plan, "hocon": VALID_HOCON})
    with pytest.raises(DomainError, match="extra_field"):
        parse_candidate(raw)


def test_unknown_schedule_mode_is_rejected() -> None:
    plan = {**VALID_PLAN, "incremental": {"mode": "SOMETIMES"}}
    raw = json.dumps({"plan": plan, "hocon": VALID_HOCON})
    with pytest.raises(DomainError, match="mode"):
        parse_candidate(raw)


def test_empty_mappings_are_rejected() -> None:
    plan = {**VALID_PLAN, "mappings": []}
    raw = json.dumps({"plan": plan, "hocon": VALID_HOCON})
    with pytest.raises(DomainError, match="mappings"):
        parse_candidate(raw)


def test_invalid_error_code_pattern_is_rejected() -> None:
    plan = {
        **VALID_PLAN,
        "quality_rules": [{"column": "id", "kind": "NOT_NULL", "error_code": "e_lowercase"}],
    }
    raw = json.dumps({"plan": plan, "hocon": VALID_HOCON})
    with pytest.raises(DomainError, match="error_code"):
        parse_candidate(raw)


# ------------------------------------------------------------ HOCON 闭合校验
def test_unbalanced_braces_are_rejected() -> None:
    raw = json.dumps({"plan": VALID_PLAN, "hocon": 'env { job.mode = "BATCH"'})
    with pytest.raises(DomainError, match="花括号不配平"):
        parse_candidate(raw)


def test_unbalanced_brackets_are_rejected() -> None:
    raw = json.dumps({"plan": VALID_PLAN, "hocon": "env { list = [1, 2 }"})
    with pytest.raises(DomainError, match="不配平"):
        parse_candidate(raw)


def test_multiple_config_blocks_are_rejected() -> None:
    hocon = "env { a = 1 }\nsource { x = 1 }\nsource { y = 2 }\n"
    raw = json.dumps({"plan": VALID_PLAN, "hocon": hocon})
    with pytest.raises(DomainError, match="多份配置块"):
        parse_candidate(raw)


def test_empty_hocon_is_rejected() -> None:
    raw = json.dumps({"plan": VALID_PLAN, "hocon": "   "})
    with pytest.raises(DomainError, match="HOCON 内容为空"):
        parse_candidate(raw)


# ------------------------------------------------------------ 内联明文凭据
@pytest.mark.parametrize(
    "hocon",
    [
        'env { password = "hunter2" }',
        'source { Jdbc { pwd = "abc" } }',
        'sink { Doris { access_key = "AKIAEXAMPLE" } }',
    ],
)
def test_inline_secrets_are_rejected(hocon: str) -> None:
    with pytest.raises(DomainError, match="内联明文凭据"):
        require_no_inline_secrets(hocon)


@pytest.mark.parametrize(
    "hocon",
    [
        'env { password = "${MYSQL_PASSWORD}" }',
        'env { password = "{{secret://db/mysql}}" }',
        'env { password = "vault://secret/db" }',
        'env { password = "${env:MYSQL_PASSWORD}" }',
    ],
)
def test_secret_placeholders_pass(hocon: str) -> None:
    require_no_inline_secrets(hocon)


# ------------------------------------------------------------ 直连后端
class _OpenAIHandler(BaseHTTPRequestHandler):
    """最小的 OpenAI 兼容 `/chat/completions` 实现。"""

    # 类级存储：测试期间跨请求共享，显式声明避免被误当实例属性。
    requests: ClassVar[list[dict[str, Any]]] = []
    response_content: ClassVar[str] = ""
    status: ClassVar[int] = 200
    body_override: ClassVar[bytes | None] = None

    def log_message(self, *args: Any) -> None:
        return

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length)
        type(self).requests.append(
            {
                "path": self.path,
                "authorization": self.headers.get("Authorization"),
                "payload": json.loads(raw),
            }
        )
        body = type(self).body_override
        if body is None:
            body = json.dumps(
                {
                    "choices": [
                        {"message": {"role": "assistant", "content": type(self).response_content}}
                    ]
                }
            ).encode()
        self.send_response(type(self).status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


@contextmanager
def _fake_openai() -> Iterator[str]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), _OpenAIHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def settings_for(base_url: str, **overrides: Any) -> Settings:
    values: dict[str, Any] = {
        "_env_file": None,
        "llm_base_url": base_url,
        "llm_api_key": "test-llm-key-0123456789",
        "llm_model": "test-model",
    }
    values.update(overrides)
    return Settings(**values)


def sample_request(**overrides: Any) -> GenerationRequest:
    values: dict[str, Any] = {
        "requirement": "把订单同步到宽表",
        "source_summary": "MySQL source_db.orders",
        "target_summary": "Doris dw.orders_wide",
    }
    values.update(overrides)
    return GenerationRequest(**values)


def test_direct_adapter_satisfies_generation_port() -> None:
    """后端是 Port 的实现：切换后端只改变「候选如何产生」（FR-28）。"""
    adapter: GenerationPort = DirectGenerationAdapter(settings_for("http://127.0.0.1:1"))
    assert adapter.backend_name == "direct"


async def test_adapter_posts_openai_compatible_request() -> None:
    _OpenAIHandler.requests = []
    _OpenAIHandler.response_content = valid_payload()
    _OpenAIHandler.status = 200
    _OpenAIHandler.body_override = None
    with _fake_openai() as base_url:
        adapter = DirectGenerationAdapter(settings_for(base_url))
        raw = await adapter.generate(sample_request(repair_hint="[FIELD_TOPOLOGY] 缺少去重键"))

    assert parse_candidate(raw).plan.target.table_name == "orders_wide"
    sent = _OpenAIHandler.requests[0]
    assert sent["path"] == "/chat/completions"
    assert sent["authorization"] == "Bearer test-llm-key-0123456789"
    payload = sent["payload"]
    assert payload["model"] == "test-model"
    assert payload["temperature"] == 0
    assert payload["stream"] is False
    assert payload["messages"][0]["content"] == SYSTEM_PROMPT
    user_prompt = payload["messages"][1]["content"]
    assert "把订单同步到宽表" in user_prompt
    # 修复提示必须传给模型，否则修复循环等于重试而不是修复
    assert "缺少去重键" in user_prompt


async def test_adapter_reports_missing_configuration() -> None:
    """未配置模型时明确报告「未就绪」，不静默失败（PRD §5.3）。"""
    adapter = DirectGenerationAdapter(Settings(_env_file=None))
    with pytest.raises(ExternalServiceUnavailable, match="未就绪"):
        await adapter.generate(sample_request())


async def test_adapter_maps_upstream_error_without_leaking_key() -> None:
    _OpenAIHandler.requests = []
    _OpenAIHandler.status = 429
    _OpenAIHandler.body_override = b'{"error": "rate limited"}'
    with _fake_openai() as base_url:
        adapter = DirectGenerationAdapter(settings_for(base_url))
        with pytest.raises(ExternalServiceUnavailable) as error:
            await adapter.generate(sample_request())
    assert "429" in str(error.value)
    assert "test-llm-key-0123456789" not in str(error.value)


@pytest.mark.parametrize(
    "body",
    [
        b'{"choices": []}',
        b'{"nope": 1}',
        b'{"choices": [{"message": {"content": ""}}]}',
        b'["not", "an", "object"]',
    ],
)
async def test_adapter_rejects_malformed_upstream_response(body: bytes) -> None:
    _OpenAIHandler.requests = []
    _OpenAIHandler.status = 200
    _OpenAIHandler.body_override = body
    with _fake_openai() as base_url:
        adapter = DirectGenerationAdapter(settings_for(base_url))
        with pytest.raises((DomainError, ExternalServiceUnavailable)):
            await adapter.generate(sample_request())


def test_unimplemented_backend_reports_clearly() -> None:
    """第二后端的切换点存在但未接入时必须明确报错，不静默降级到别的后端。"""
    with pytest.raises(DomainError, match="尚未接入"):
        create_generation_adapter(
            settings=settings_for("http://127.0.0.1:1", generation_backend="seatunnel-cli")
        )
