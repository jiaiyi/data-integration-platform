"""HDFS 元数据探查器（T-11，超集清单）。

走 **WebHDFS REST**（httpx），不额外引入 HDFS 客户端依赖。探查单位是**文件**：
`schema_name` 指目录，`table_name` 指该目录下的文件名，字段清单复用
`infrastructure/file_formats.py` 的格式推断。

读取**有界**：`OPEN` 带 `offset`/`length`，最多读 `remote_file_sample_bytes`。
结构信息在文件尾部的格式（Parquet/ORC/Excel/PDF）在文件超过上限时**明确报错**，
不做「读一半装作读全了」的降级。
"""

import json
from collections.abc import Mapping, Sequence
from typing import Any

import httpx

from data_integration_platform.application.profiling import ProbeResult, ProbeTarget
from data_integration_platform.application.secrets import ConnectionCredentials
from data_integration_platform.config import get_settings
from data_integration_platform.domain.enums import ConnectorType
from data_integration_platform.domain.errors import (
    DomainError,
    ExternalServiceUnavailable,
    ResourceNotFound,
)
from data_integration_platform.infrastructure.probe_support import (
    describe_failure,
    optional_secret,
    require_relative_path,
    require_username,
)
from data_integration_platform.infrastructure.remote_probe import (
    build_remote_probe_result,
    plan_remote_read,
)

DEFAULT_PORT = 9870
DEFAULT_SCHEME = "http"


def _as_mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


class HdfsMetadataProbe:
    connector_type = ConnectorType.HDFS

    async def test_connection(
        self, *, target: ProbeTarget, credentials: ConnectionCredentials
    ) -> None:
        base = self._resolve_base(target, None)
        await self._request(target, credentials, base, {"op": "GETFILESTATUS"})

    async def list_tables(
        self,
        *,
        target: ProbeTarget,
        credentials: ConnectionCredentials,
        schema_name: str | None,
        limit: int,
    ) -> Sequence[str]:
        base = self._resolve_base(target, schema_name)
        payload = await self._request(target, credentials, base, {"op": "LISTSTATUS"})
        statuses = _as_mapping(payload.get("FileStatuses")).get("FileStatus")
        if not isinstance(statuses, list):
            return []
        # HDFS 的探查单位是文件；目录通过 `schema_name`（或连接上的 base_path）指定。
        files = [
            str(_as_mapping(status).get("pathSuffix") or "")
            for status in statuses
            if str(_as_mapping(status).get("type")) == "FILE"
        ]
        return [name for name in files if name][:limit]

    async def probe_table(
        self,
        *,
        target: ProbeTarget,
        credentials: ConnectionCredentials,
        schema_name: str,
        table_name: str,
        sample_limit: int,
    ) -> ProbeResult:
        base = self._resolve_base(target, schema_name)
        relative = require_relative_path(table_name, kind="HDFS 文件名")
        path = f"{base.rstrip('/')}/{relative}"
        status = _as_mapping(
            (await self._request(target, credentials, path, {"op": "GETFILESTATUS"})).get(
                "FileStatus"
            )
        )
        if str(status.get("type")) != "FILE":
            raise DomainError(f"探查目标不是文件：{path}")
        length = int(status.get("length") or 0)
        plan = plan_remote_read(
            object_name=relative,
            size_bytes=length,
            cap_bytes=get_settings().remote_file_sample_bytes,
        )
        data = await self._read(target, credentials, path, plan.read_length)
        return build_remote_probe_result(
            plan=plan,
            data=data,
            schema_name=base,
            object_name=relative,
            size_bytes=length,
            sample_limit=sample_limit,
            statistics={"modified": str(status.get("modificationTime") or "")},
        )

    def _resolve_base(self, target: ProbeTarget, schema_name: str | None) -> str:
        base = (schema_name or target.options.get("base_path") or "").strip()
        if not base:
            raise DomainError("HDFS 探查需要指定目录（base_path）")
        return base if base.startswith("/") else f"/{base}"

    async def _read(
        self,
        target: ProbeTarget,
        credentials: ConnectionCredentials,
        path: str,
        length: int,
    ) -> bytes:
        """前缀范围读取：`offset=0&length=N`，绝不整份下载。"""
        params = {"op": "OPEN", "offset": 0, "length": length}
        return await self._request_bytes(target, credentials, path, params)

    async def _request(
        self,
        target: ProbeTarget,
        credentials: ConnectionCredentials,
        path: str,
        params: Mapping[str, Any],
    ) -> dict[str, Any]:
        payload = await self._call(target, credentials, path, params)
        try:
            parsed = json.loads(payload.decode("utf-8"))
        except (UnicodeDecodeError, ValueError) as exc:
            raise ExternalServiceUnavailable("WebHDFS 返回了无法解析的响应") from exc
        return parsed if isinstance(parsed, dict) else {}

    async def _request_bytes(
        self,
        target: ProbeTarget,
        credentials: ConnectionCredentials,
        path: str,
        params: Mapping[str, Any],
    ) -> bytes:
        return await self._call(target, credentials, path, params)

    async def _call(
        self,
        target: ProbeTarget,
        credentials: ConnectionCredentials,
        path: str,
        params: Mapping[str, Any],
    ) -> bytes:
        settings = get_settings()
        query: dict[str, Any] = dict(params)
        query["user.name"] = require_username(credentials, connector=self.connector_type.value)
        delegation = optional_secret(credentials.auth_header_value)
        if delegation:
            query["delegation"] = delegation
        scheme = target.options.get("scheme") or DEFAULT_SCHEME
        host = target.host or "localhost"
        port = target.port or DEFAULT_PORT
        url = f"{scheme}://{host}:{port}/webhdfs/v1{path}"
        try:
            async with httpx.AsyncClient(
                timeout=settings.probe_statement_timeout_seconds,
                # 不走系统代理：delegation token 不能被交给未知中间人。
                trust_env=False,
            ) as client:
                response = await client.get(url, params=query)
        except httpx.HTTPError as exc:
            raise ExternalServiceUnavailable(
                describe_failure(exc, context=f"{self.connector_type.value} 请求")
            ) from exc
        if response.status_code == 404:
            raise ResourceNotFound(f"HDFS 路径不存在：{path}")
        if response.status_code >= 400:
            raise ExternalServiceUnavailable(f"WebHDFS 返回 {response.status_code}（{path}）")
        return response.content
