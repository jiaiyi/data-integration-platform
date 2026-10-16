"""SeaTunnel Zeta 的 REST 执行适配器（T-38）。

**提交契约与文档里写的不一样**，这是实测出来的（别再照文档改回去）：

- 提交是 `POST /submit-job?format=HOCON&jobName=<名字>`，**请求体就是 HOCON 原文**。
  `format` 是**查询参数**，不是请求体字段；文档里那种 `{"config": "...", "configFormat": "HOCON"}`
  的写法在 2.3.13 上会走到 `format=JSON` 的默认分支，在那条分支里引擎自己会 NPE
  （`ConfigShadeUtils.processConfig` 对 `sink` 取空值），返回 500 且消息为空。
- 状态要自己去 `/running-jobs`、`/pending-jobs`、`/finished-jobs` 三个列表里找，
  没有「按 jobId 查状态」的可用接口（`/job-info` 只允许 GET 而它又要读请求体）。
- 取消是 `POST /stop-job`，**JSON 体** `{"jobId": ...}`。
- 引擎给的时间是 `yyyy-MM-dd HH:mm:ss` 且**不带时区**：按配置的 `seatunnel_timezone` 解释，
  否则同一份事实在不同部署下会被读成不同时刻。

平台主动外发一律 `trust_env=False`：不走系统代理，否则配置里的凭据占位符与请求会被交给
未知中间人。
"""

from datetime import UTC, datetime, tzinfo
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import httpx

from data_integration_platform.application.harness.engine import (
    EngineJobState,
    ExecutionEnginePort,
)
from data_integration_platform.config import Settings, get_settings
from data_integration_platform.domain.errors import DomainError, ExternalServiceUnavailable

_SUBMIT_PATH = "/submit-job"
_STOP_PATH = "/stop-job"
_RUNNING_PATH = "/running-jobs"
_PENDING_PATH = "/pending-jobs"
_FINISHED_PATH = "/finished-jobs"

_ENGINE_TIME_FORMAT = "%Y-%m-%d %H:%M:%S"


class SeaTunnelExecutionEngine(ExecutionEnginePort):
    def __init__(
        self,
        *,
        api_url: str,
        timezone: str,
        timeout_seconds: float,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._api_url = api_url.rstrip("/")
        self._timezone = timezone
        self._timeout = timeout_seconds
        self._transport = transport

    async def submit(self, *, config: str, job_name: str) -> str:
        """提交配置；返回引擎的 `jobId`。"""
        payload = await self._request(
            "POST",
            _SUBMIT_PATH,
            params={"format": "HOCON", "jobName": job_name},
            # 请求体是 HOCON 原文，不是 JSON —— 见模块文档。
            content=config.encode("utf-8"),
            headers={"Content-Type": "text/plain; charset=utf-8"},
        )
        job_id = payload.get("jobId")
        if not isinstance(job_id, str) or not job_id:
            # 收下了却没有 id，就没法跟踪这个作业了：这是必须报出来的失败，
            # 而不是「大概提交成功了吧」。
            raise ExternalServiceUnavailable(f"引擎未返回作业 id：{str(payload)[:200]}")
        return job_id

    async def status(self, *, job_id: str) -> EngineJobState | None:
        """在引擎的三个作业列表里找这个 id；找不到返回 `None`（不猜）。"""
        for path in (_RUNNING_PATH, _PENDING_PATH, _FINISHED_PATH):
            entries = await self._request("GET", path)
            if not isinstance(entries, list):
                continue
            for entry in entries:
                if isinstance(entry, dict) and str(entry.get("jobId")) == job_id:
                    return _state(entry, timezone=self._timezone)
        return None

    async def cancel(self, *, job_id: str) -> None:
        await self._request("POST", _STOP_PATH, json={"jobId": job_id})

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, str] | None = None,
        json: dict[str, Any] | None = None,
        content: bytes | None = None,
        headers: dict[str, str] | None = None,
    ) -> Any:
        try:
            async with httpx.AsyncClient(
                base_url=self._api_url,
                timeout=httpx.Timeout(self._timeout),
                transport=self._transport,
                trust_env=False,
            ) as client:
                response = await client.request(
                    method, path, params=params, json=json, content=content, headers=headers
                )
                response.raise_for_status()
                if not response.content:
                    return {}
                return response.json()
        except httpx.HTTPError as exc:
            raise ExternalServiceUnavailable(
                f"引擎请求失败（{method} {path}）：{type(exc).__name__}"
            ) from exc
        except ValueError as exc:
            raise DomainError(f"引擎返回的不是 JSON（{method} {path}）") from exc


def _state(entry: dict[str, Any], *, timezone: str) -> EngineJobState:
    return EngineJobState(
        job_id=str(entry.get("jobId")),
        engine_status=str(entry.get("jobStatus") or entry.get("status") or "UNKNOWN"),
        error_message=_optional_str(entry.get("errorMsg")),
        started_at=_engine_time(entry.get("startTime"), timezone=timezone),
        finished_at=_engine_time(entry.get("finishTime"), timezone=timezone),
    )


def _optional_str(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _engine_time(value: Any, *, timezone: str) -> datetime | None:
    """引擎给的是不带时区的本地时间；按配置的时区解释后转成 UTC。

    解析不了就返回 `None` —— 时间点缺失比一个猜错的时间点安全。
    """
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.strptime(value.strip(), _ENGINE_TIME_FORMAT)
    except ValueError:
        return None
    try:
        zone: tzinfo = ZoneInfo(timezone)
    except (ZoneInfoNotFoundError, ValueError):
        zone = UTC
    return parsed.replace(tzinfo=zone).astimezone(UTC)


def create_execution_engine(*, settings: Settings | None = None) -> ExecutionEnginePort:
    resolved = settings if settings is not None else get_settings()
    return SeaTunnelExecutionEngine(
        api_url=str(resolved.seatunnel_api_url),
        timezone=resolved.seatunnel_timezone,
        timeout_seconds=float(resolved.seatunnel_check_timeout_seconds),
    )
