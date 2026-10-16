"""真实引擎检查的 SeaTunnel 实现（T-21 / FR-35）。

两件都由**引擎自己**回答，平台不代答：
1. `GET /overview` —— 引擎在不在、是哪个版本、有几个 worker；
2. `POST /encrypt-config` —— 让引擎用它自己的配置解析器解析候选配置文档。

**为什么不是 `--check`**：SPEC 写的「引擎 `--check`」在 SeaTunnel 2.3.13 上对作业配置
是空操作（实测语法损坏、插件不存在的配置都返回 0，它校验的是集群配置）；
REST 也没有 dry-run 端点，`/submit-job` 会真的把作业跑起来。冻结之前绝不允许产生副作用，
所以只能走上面这两条无副作用的接口。**代价要说清楚**：`/encrypt-config` 判的是配置文档
结构（缺 source/sink、source 为空这类），**不校验连接器选项**；选项级校验只发生在提交时的
作业解析阶段。这是能力边界，不是可以含糊过去的地方。

与其它外发一致：**不走系统代理**（`trust_env=False`），否则配置里的占位符与鉴权头会被
交给未知中间人。失败信息统一脱敏后再返回，且**永不抛异常** —— 「引擎不可用」必须变成
一条能被冻结流程看见的结论（FR-35：不允许跳过引擎检查直接冻结）。
"""

from collections.abc import Mapping
from typing import Any

import httpx
from pyhocon import ConfigFactory

from data_integration_platform.application.engine_check import EngineCheckPort
from data_integration_platform.application.masking import scrub_secrets
from data_integration_platform.application.policy import GateFinding, GateKind
from data_integration_platform.config import Settings, get_settings

_OVERVIEW_PATH = "/overview"
_VALIDATE_PATH = "/encrypt-config"
# SeaTunnel V2 配置里这三段是「插件列表」，其余是普通对象。
_PLUGIN_SECTIONS = ("source", "transform", "sink")


class SeaTunnelEngineCheck(EngineCheckPort):
    """通过 SeaTunnel REST 接口做真实引擎检查。"""

    def __init__(
        self,
        settings: Settings | None = None,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        # `transport` 是测试缝：与状态机的 `checkpointer` 注入同理，生产留空。
        self._settings = settings
        self._transport = transport

    @property
    def settings(self) -> Settings:
        return self._settings if self._settings is not None else get_settings()

    async def check(self, *, hocon: str) -> GateFinding:
        try:
            document = _config_document(hocon)
        except Exception as exc:  # pyhocon 的解析异常族很杂，边界处统一收口
            return _failed(f"候选配置无法被解析：{_reason(exc)}")

        settings = self.settings
        try:
            async with httpx.AsyncClient(
                base_url=str(settings.seatunnel_api_url).rstrip("/"),
                timeout=httpx.Timeout(settings.seatunnel_check_timeout_seconds),
                transport=self._transport,
                # 平台主动外发一律不走系统代理（见 HANDOVER §5.2）。
                trust_env=False,
            ) as client:
                engine = await self._describe(client)
                rejection = await self._validate(client, document)
        except (httpx.HTTPError, ValueError) as exc:
            return _failed(f"引擎不可用：{_reason(exc)}")

        if rejection is not None:
            return _failed(f"引擎 {engine} 拒绝了该配置：{rejection}")
        return GateFinding(
            kind=GateKind.ENGINE_CHECK,
            passed=True,
            detail=f"引擎 {engine} 已解析并接受该配置文档",
        )

    async def _describe(self, client: httpx.AsyncClient) -> str:
        """引擎自述：版本与 worker 数。这份事实进入结论文本，便于事后对账。"""
        response = await client.get(_OVERVIEW_PATH)
        response.raise_for_status()
        payload = response.json()
        version = payload.get("projectVersion") if isinstance(payload, dict) else None
        workers = payload.get("workers") if isinstance(payload, dict) else None
        return f"SeaTunnel {version}（workers={workers}）"

    async def _validate(self, client: httpx.AsyncClient, document: Mapping[str, Any]) -> str | None:
        """交给引擎解析；通过返回 `None`，被拒返回引擎原话（已脱敏、已截断）。"""
        response = await client.post(_VALIDATE_PATH, json=dict(document))
        if response.status_code < 400:
            return None
        return scrub_secrets(_engine_message(response) or f"HTTP {response.status_code}")


def _config_document(hocon: str) -> dict[str, Any]:
    """把候选 HOCON 转成引擎 `/encrypt-config` 接受的配置文档。

    引擎的 REST 接口只吃 JSON（HOCON 文本会被它直接拒掉），所以先解析再按 SeaTunnel
    V2 的文档形状重排：`env` 原样，`source`/`transform`/`sink` 从「插件名 → 选项」的
    对象转成「插件对象」数组。

    **转换本身不判对错**：缺 source、缺 sink 一律照原样交给引擎，由引擎报
    「Miss <Source> config!」——那才是真实引擎检查，平台不在这里替它下结论。
    """
    parsed = ConfigFactory.parse_string(hocon).as_plain_ordered_dict()
    document: dict[str, Any] = {}
    env = parsed.get("env")
    if isinstance(env, Mapping):
        document["env"] = env
    for section in _PLUGIN_SECTIONS:
        document[section] = _plugins(parsed.get(section))
    return document


def _plugins(block: object) -> list[dict[str, Any]]:
    if not isinstance(block, Mapping):
        return []
    return [{str(name): options} for name, options in block.items()]


def _engine_message(response: httpx.Response) -> str | None:
    try:
        payload = response.json()
    except ValueError:
        return _short(response.text)
    if isinstance(payload, dict):
        message = payload.get("message")
        if isinstance(message, str) and message.strip():
            return _short(message)
    return None


def _failed(detail: str) -> GateFinding:
    return GateFinding(kind=GateKind.ENGINE_CHECK, passed=False, detail=detail)


def _reason(exc: Exception) -> str:
    return scrub_secrets(_short(f"{type(exc).__name__}: {exc}"))


def _short(text: str, *, limit: int = 300) -> str:
    collapsed = " ".join(text.split())
    return collapsed if len(collapsed) <= limit else f"{collapsed[:limit]}…"
