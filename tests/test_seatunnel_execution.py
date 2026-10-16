"""SeaTunnel 执行适配器的请求契约（T-38）。

这里钉的是**从真实引擎实测出来的**契约，不是文档里的：
提交走 `POST /submit-job?format=HOCON&jobName=...`，请求体是 HOCON 原文。
文档里那种把配置塞进 JSON 字段的写法在 2.3.13 上会落到引擎自己会 NPE 的分支，
返回 500 且消息为空 —— 所以这条测试值得有，改错了不会有人立刻发现。
"""

import httpx
import pytest

from data_integration_platform.application.harness.engine import (
    EngineJobState,
    project_status,
)
from data_integration_platform.domain.enums import ExecutionStatus
from data_integration_platform.domain.errors import DomainError, ExternalServiceUnavailable
from data_integration_platform.infrastructure.seatunnel_execution import SeaTunnelExecutionEngine

HOCON = 'env {\n  job.mode = "BATCH"\n}\n'


class Recorder:
    """记录请求并按脚本回话；顺带断言「请求长什么样」。"""

    def __init__(self, responses: dict[str, httpx.Response]) -> None:
        self.responses = responses
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        key = f"{request.method} {request.url.path}"
        return self.responses.get(key, httpx.Response(404, json={"status": "fail"}))


def engine(recorder: Recorder) -> SeaTunnelExecutionEngine:
    return SeaTunnelExecutionEngine(
        api_url="http://127.0.0.1:8080",
        timezone="Asia/Shanghai",
        timeout_seconds=10,
        transport=httpx.MockTransport(recorder),
    )


# ------------------------------------------------------------------ 提交
async def test_submit_sends_hocon_in_the_body_and_format_in_the_query() -> None:
    """`format` 是**查询参数**、请求体是 HOCON 原文 —— 这是最容易照着文档写错的一处。"""
    recorder = Recorder(
        {"POST /submit-job": httpx.Response(200, json={"jobId": "123", "jobName": "dip-1"})}
    )

    job_id = await engine(recorder).submit(config=HOCON, job_name="dip-1")

    request = recorder.requests[0]
    assert job_id == "123"
    assert request.url.params["format"] == "HOCON"
    assert request.url.params["jobName"] == "dip-1"
    assert request.content.decode() == HOCON
    assert "json" not in request.headers.get("content-type", "")


async def test_submit_without_a_job_id_is_a_failure_not_a_success() -> None:
    """收下了却没有 id 就没法跟踪这个作业：必须报失败，不能当成「大概成了」。"""
    recorder = Recorder({"POST /submit-job": httpx.Response(200, json={"jobName": "dip-1"})})

    with pytest.raises(ExternalServiceUnavailable, match="未返回作业 id"):
        await engine(recorder).submit(config=HOCON, job_name="dip-1")


# ------------------------------------------------------------------ 状态
async def test_status_finds_the_job_in_the_finished_list() -> None:
    recorder = Recorder(
        {
            "GET /running-jobs": httpx.Response(200, json=[]),
            "GET /pending-jobs": httpx.Response(200, json=[]),
            "GET /finished-jobs": httpx.Response(
                200,
                json=[
                    {
                        "jobId": "123",
                        "jobStatus": "FINISHED",
                        "startTime": "2026-10-16 09:09:29",
                        "finishTime": "2026-10-16 09:09:31",
                    }
                ],
            ),
        }
    )

    state = await engine(recorder).status(job_id="123")

    assert state is not None
    assert state.engine_status == "FINISHED"
    assert project_status(state) is ExecutionStatus.SUCCEEDED


async def test_status_of_an_unlisted_job_is_unknown() -> None:
    """三个列表都没有它 → `None`。引擎的历史有保留期，查不到才是常态而非异常。"""
    recorder = Recorder(
        {
            "GET /running-jobs": httpx.Response(200, json=[]),
            "GET /pending-jobs": httpx.Response(200, json=[]),
            "GET /finished-jobs": httpx.Response(200, json=[]),
        }
    )

    assert await engine(recorder).status(job_id="不存在") is None


async def test_status_keeps_the_engine_error_message() -> None:
    recorder = Recorder(
        {
            "GET /running-jobs": httpx.Response(200, json=[]),
            "GET /pending-jobs": httpx.Response(200, json=[]),
            "GET /finished-jobs": httpx.Response(
                200,
                json=[
                    {
                        "jobId": "123",
                        "jobStatus": "FAILED",
                        "errorMsg": "Table not found: dw.orders_wide",
                    }
                ],
            ),
        }
    )

    state = await engine(recorder).status(job_id="123")

    assert state is not None
    assert project_status(state) is ExecutionStatus.FAILED
    assert state.error_message == "Table not found: dw.orders_wide"


async def test_engine_time_is_interpreted_in_the_configured_timezone() -> None:
    """引擎给的是不带时区的本地时间；不按配置解释的话，同一份事实会被读成不同时刻。"""
    recorder = Recorder(
        {
            "GET /running-jobs": httpx.Response(200, json=[]),
            "GET /pending-jobs": httpx.Response(200, json=[]),
            "GET /finished-jobs": httpx.Response(
                200,
                json=[{"jobId": "1", "jobStatus": "FINISHED", "startTime": "2026-10-16 02:00:00"}],
            ),
        }
    )

    state = await engine(recorder).status(job_id="1")

    assert state is not None and state.started_at is not None
    # Asia/Shanghai 的 02:00 == UTC 的前一天 18:00
    assert state.started_at.isoformat() == "2026-10-15T18:00:00+00:00"


async def test_unparsable_engine_time_is_left_unknown() -> None:
    """时间解析不了就留空 —— 一个猜错的时间点比没有时间点更糟。"""
    recorder = Recorder(
        {
            "GET /running-jobs": httpx.Response(200, json=[]),
            "GET /pending-jobs": httpx.Response(200, json=[]),
            "GET /finished-jobs": httpx.Response(
                200, json=[{"jobId": "1", "jobStatus": "FINISHED", "startTime": "刚刚"}]
            ),
        }
    )

    state = await engine(recorder).status(job_id="1")

    assert state is not None and state.started_at is None


# ------------------------------------------------------------------ 取消与错误
async def test_cancel_posts_the_job_id_as_json() -> None:
    recorder = Recorder({"POST /stop-job": httpx.Response(200, json={"jobId": "123"})})

    await engine(recorder).cancel(job_id="123")

    request = recorder.requests[0]
    assert request.url.path == "/stop-job"
    assert b'"jobId":"123"' in request.content


async def test_engine_errors_become_unavailable() -> None:
    recorder = Recorder({"POST /submit-job": httpx.Response(500, json={"status": "fail"})})

    with pytest.raises(ExternalServiceUnavailable, match="引擎请求失败"):
        await engine(recorder).submit(config=HOCON, job_name="dip-1")


async def test_non_json_response_is_reported() -> None:
    recorder = Recorder({"GET /running-jobs": httpx.Response(200, text="<html>不认识的页面")})

    with pytest.raises(DomainError, match="不是 JSON"):
        await engine(recorder).status(job_id="1")


# ------------------------------------------------------------------ 状态投影
@pytest.mark.parametrize(
    ("engine_status", "expected"),
    [
        ("CREATED", ExecutionStatus.SUBMITTED),
        ("SCHEDULED", ExecutionStatus.SUBMITTED),
        ("RUNNING", ExecutionStatus.RUNNING),
        ("CANCELLING", ExecutionStatus.CANCELLING),
        ("CANCELED", ExecutionStatus.CANCELLED),
        ("FAILED", ExecutionStatus.FAILED),
        ("FINISHED", ExecutionStatus.SUCCEEDED),
    ],
)
def test_engine_status_projects_onto_control_plane_status(
    engine_status: str, expected: ExecutionStatus
) -> None:
    state = EngineJobState(job_id="1", engine_status=engine_status)

    assert project_status(state) is expected


def test_unrecognised_engine_status_stays_unknown() -> None:
    """认不出来的状态**不猜**：落到 None，让调用方去对账（T-38「状态未知先对账」）。"""
    state = EngineJobState(job_id="1", engine_status="某种新状态")

    assert project_status(state) is None


def test_engine_status_matching_is_case_insensitive() -> None:
    assert project_status(EngineJobState(job_id="1", engine_status="running")) is (
        ExecutionStatus.RUNNING
    )
