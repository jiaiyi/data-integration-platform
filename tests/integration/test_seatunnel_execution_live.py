"""对着真实 SeaTunnel 验证执行适配器（T-38）。

要求 `--profile e2e` 起的 SeaTunnel master 在 `127.0.0.1:8080`；未启动时整组跳过。

覆盖三件事：提交能拿到作业 id、状态能推进到终态、认不出的作业 id 返回 `None`
而不是猜一个状态。用 FakeSource → Console，不碰任何外部数据源。
"""

import asyncio
import socket

import pytest

from data_integration_platform.application.harness.engine import project_status
from data_integration_platform.config import Settings
from data_integration_platform.domain.enums import ExecutionStatus
from data_integration_platform.infrastructure.seatunnel_execution import SeaTunnelExecutionEngine

HOST = "127.0.0.1"
PORT = 8080
# 127.0.0.1 而不是 localhost：Windows 优先解析 IPv6，而 Docker 只发布 IPv4。
URL = f"http://{HOST}:{PORT}"

FAKE_JOB = """env {
  job.mode = "BATCH"
}
source {
  FakeSource {
    plugin_output = "t"
    row.num = 5
    schema = { fields { id = "int" } }
  }
}
sink {
  Console {
    plugin_input = "t"
  }
}
"""


def _reachable(host: str, port: int) -> bool:
    try:
        with socket.create_connection((host, port), timeout=1.5):
            return True
    except OSError:
        return False


requires_seatunnel = pytest.mark.skipif(
    not _reachable(HOST, PORT), reason="SeaTunnel 未启动（docker compose --profile e2e）"
)

pytestmark = pytest.mark.integration


def engine() -> SeaTunnelExecutionEngine:
    return SeaTunnelExecutionEngine(
        api_url=URL,
        timezone="UTC",
        timeout_seconds=30,
    )


@requires_seatunnel
async def test_submit_returns_a_job_id_and_reaches_a_terminal_state() -> None:
    """真实引擎：提交拿得到 id，状态最终能落到终态。"""
    live = engine()

    job_id = await live.submit(config=FAKE_JOB, job_name="dip-live-probe")
    assert job_id

    state = None
    for _ in range(20):
        state = await live.status(job_id=job_id)
        if state is not None and project_status(state) in (
            ExecutionStatus.SUCCEEDED,
            ExecutionStatus.FAILED,
        ):
            break
        await asyncio.sleep(1)

    assert state is not None, "提交过的作业在引擎的列表里查不到"
    assert project_status(state) is ExecutionStatus.SUCCEEDED, state.canonical_payload()
    assert state.started_at is not None
    assert state.finished_at is not None
    assert state.finished_at >= state.started_at


@requires_seatunnel
async def test_unknown_job_id_is_reported_as_unknown_not_guessed() -> None:
    """查不到的作业返回 `None` —— 「不知道」不能被伪装成某个终态（T-38 先对账）。"""
    live = engine()

    assert await live.status(job_id="0000000000000000000") is None
    assert project_status(None) is None


@requires_seatunnel
async def test_invalid_config_is_rejected_with_the_engine_message() -> None:
    """配置不合法时引擎明确报错，而不是收下再失败。"""
    from data_integration_platform.domain.errors import ExternalServiceUnavailable

    live = engine()

    with pytest.raises(ExternalServiceUnavailable):
        await live.submit(config="这不是 HOCON", job_name="dip-live-bad")


@requires_seatunnel
async def test_settings_drive_the_default_engine() -> None:
    """默认构造读配置：引擎地址与时区都来自部署配置，不写死。"""
    from data_integration_platform.infrastructure.seatunnel_execution import (
        create_execution_engine,
    )

    port = create_execution_engine(
        settings=Settings(_env_file=None, seatunnel_api_url=URL, seatunnel_timezone="UTC")
    )

    job_id = await port.submit(config=FAKE_JOB, job_name="dip-live-settings")
    assert job_id
