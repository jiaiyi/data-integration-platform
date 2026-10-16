"""REST API 数据源探查器（T-17 / GAP-03）。

需求基线 §2.8.1 要求接入 REST API，参考实现没有，属必须补齐的真实缺口。

按决策 **D-Q2** 的口径：
- **出口主机白名单**：不在名单内一律拒绝；名单为空表示**全部拒绝**（失败关闭，不是全放行）；
- **请求头鉴权**：头名是普通配置，头值以 Secret 引用管理，并挡 CR/LF 注入；
- 单次采样 + 响应结构推断；分页与嵌套 JSON 展平列 P2，本期不做。

REST 没有「表」概念，因此以单一虚拟表 `response` 表达响应结构；
非对象/对象数组的响应按无结构处理，**不产出样本**（长文本的值级检测不可靠）。
"""

from collections.abc import Sequence

import httpx

from data_integration_platform.application.profiling import (
    ProbedColumn,
    ProbeResult,
    ProbeTarget,
)
from data_integration_platform.application.secrets import ConnectionCredentials
from data_integration_platform.config import get_settings
from data_integration_platform.domain.enums import ConnectorType, FileAssetFormat
from data_integration_platform.domain.errors import DomainError, ExternalServiceUnavailable
from data_integration_platform.infrastructure.egress import require_allowed_egress
from data_integration_platform.infrastructure.file_formats import inspect_file
from data_integration_platform.infrastructure.probe_support import (
    cell_to_plain,
    describe_failure,
    require_safe_header,
)

VIRTUAL_TABLE = "response"
DEFAULT_METHOD = "GET"


class RestApiMetadataProbe:
    connector_type = ConnectorType.REST_API

    async def test_connection(
        self, *, target: ProbeTarget, credentials: ConnectionCredentials
    ) -> None:
        await self._fetch(target=target, credentials=credentials)

    async def list_tables(
        self,
        *,
        target: ProbeTarget,
        credentials: ConnectionCredentials,
        schema_name: str | None,
        limit: int,
    ) -> Sequence[str]:
        del schema_name, credentials, limit
        # REST 没有表概念：以单一虚拟表表达「该端点的响应结构」。
        return [VIRTUAL_TABLE]

    async def probe_table(
        self,
        *,
        target: ProbeTarget,
        credentials: ConnectionCredentials,
        schema_name: str,
        table_name: str,
        sample_limit: int,
    ) -> ProbeResult:
        if table_name != VIRTUAL_TABLE:
            raise DomainError(f"REST API 数据源只暴露虚拟表 {VIRTUAL_TABLE}")
        body = await self._fetch(target=target, credentials=credentials)
        schema = schema_name or VIRTUAL_TABLE
        try:
            inspection = inspect_file(
                body, file_format=FileAssetFormat.JSON, sample_limit=sample_limit
            )
        except DomainError as exc:
            # 响应不是对象或对象数组（纯文本、标量、非 JSON）：按无结构响应处理。
            return ProbeResult(
                schema_name=schema,
                table_name=VIRTUAL_TABLE,
                columns=(ProbedColumn(name="content", data_type="TEXT", ordinal=1),),
                primary_keys=(),
                statistics={
                    "representation": "rest-unstructured",
                    "reason": str(exc),
                    "response_bytes": len(body),
                    "primary_key_supported": False,
                },
                sample_rows=(),
            )
        return ProbeResult(
            schema_name=schema,
            table_name=VIRTUAL_TABLE,
            columns=tuple(inspection.columns),
            primary_keys=(),
            statistics={
                **inspection.statistics,
                "response_bytes": len(body),
                "primary_key_supported": False,
            },
            sample_rows=tuple(
                {key: cell_to_plain(value) for key, value in row.items()}
                for row in inspection.sample_rows
            ),
        )

    async def _fetch(self, *, target: ProbeTarget, credentials: ConnectionCredentials) -> bytes:
        settings = get_settings()
        url = (target.options.get("url") or "").strip()
        if not url:
            raise DomainError("REST API 数据源需要在连接选项里配置 url")
        require_allowed_egress(url, settings.allowed_rest_api_hosts())

        headers: dict[str, str] = {}
        if credentials.auth_header_name and credentials.auth_header_value is not None:
            name, value = require_safe_header(
                credentials.auth_header_name, credentials.auth_header_value.get_secret_value()
            )
            headers[name] = value

        method = (target.options.get("method") or DEFAULT_METHOD).upper()
        cap = settings.rest_api_max_sample_bytes
        try:
            async with httpx.AsyncClient(
                timeout=settings.rest_api_timeout_seconds,
                follow_redirects=False,
                # 不走系统代理：鉴权头是凭据，不能被交给未知中间人。
                trust_env=False,
            ) as client:
                async with client.stream(method, url, headers=headers) as response:
                    if response.status_code >= 400:
                        raise ExternalServiceUnavailable(
                            f"REST API 返回 {response.status_code}（{response.request.url.host}）"
                        )
                    return await _read_capped(response, cap)
        except httpx.HTTPError as exc:
            raise ExternalServiceUnavailable(
                describe_failure(exc, context="REST API 请求")
            ) from exc


async def _read_capped(response: httpx.Response, cap: int) -> bytes:
    """读取响应体但**不超过上限**：达到上限即停止拉取，不把整个响应灌进内存。"""
    chunks: list[bytes] = []
    total = 0
    async for chunk in response.aiter_bytes():
        chunks.append(chunk)
        total += len(chunk)
        if total >= cap:
            break
    return b"".join(chunks)[:cap]
