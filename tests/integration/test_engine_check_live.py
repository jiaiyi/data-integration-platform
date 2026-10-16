"""对真实 SeaTunnel 的引擎检查验证（T-21 / FR-35）。

要求 `--profile e2e` 起的 SeaTunnel master 在 `127.0.0.1:8080`；未启动时整组跳过。
覆盖三件事：真引擎接受合法配置、真引擎**自己**报出缺 sink、引擎不可达时结论落在
「引擎不可用」上（FR-35 的验收口径）。

`--check` 的边界也在这里钉住：SeaTunnel 2.3.13 的 CLI `--check` 对作业配置是空操作，
所以本层走的是 `/overview` + `/encrypt-config`，不声称等价于 `--check`。
"""

import socket

import pytest

from data_integration_platform.application.policy import GateKind
from data_integration_platform.config import Settings
from data_integration_platform.infrastructure.seatunnel_engine_check import SeaTunnelEngineCheck

SEATUNNEL_HOST = "127.0.0.1"
SEATUNNEL_PORT = 8080
# 用 127.0.0.1 而不是 localhost：Windows 上 localhost 优先解析到 IPv6 `::1`，
# 而 Docker 只发布 IPv4（见 README 的本机联调坑）。
SEATUNNEL_URL = f"http://{SEATUNNEL_HOST}:{SEATUNNEL_PORT}"

VALID_HOCON = """env {
  job.mode = "BATCH"
}
source {
  FakeSource {
    plugin_output = "t"
    row.num = 3
    schema = { fields { id = "int" } }
  }
}
sink {
  Console {
    plugin_input = "t"
  }
}
"""

MISSING_SINK_HOCON = """env {
  job.mode = "BATCH"
}
source {
  FakeSource {
    plugin_output = "t"
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
    not _reachable(SEATUNNEL_HOST, SEATUNNEL_PORT),
    reason="SeaTunnel 未启动（docker compose --profile e2e）",
)

pytestmark = pytest.mark.integration


def live_check(url: str = SEATUNNEL_URL) -> SeaTunnelEngineCheck:
    return SeaTunnelEngineCheck(
        Settings(_env_file=None, seatunnel_api_url=url, seatunnel_check_timeout_seconds=15)
    )


@requires_seatunnel
async def test_real_engine_accepts_a_well_formed_config() -> None:
    finding = await live_check().check(hocon=VALID_HOCON)

    assert finding.kind is GateKind.ENGINE_CHECK
    assert finding.passed, finding.detail
    # 结论里必须有引擎自述，证明这份事实来自真实引擎而不是平台自己写的常量。
    assert "2.3.13" in finding.detail


@requires_seatunnel
async def test_real_engine_reports_the_missing_sink_itself() -> None:
    """缺 sink 由引擎报出，平台只负责转达 —— 这是「真实引擎检查」与自说自话的分界。"""
    finding = await live_check().check(hocon=MISSING_SINK_HOCON)

    assert not finding.passed
    assert "Miss <Sink> config" in finding.detail


@requires_seatunnel
async def test_hocon_syntax_error_fails_without_asking_the_engine() -> None:
    finding = await live_check().check(hocon='env { job.mode = "BATCH"\nsource { FakeSource {\n')

    assert not finding.passed
    assert "无法被解析" in finding.detail


async def test_unreachable_engine_blocks_freeze() -> None:
    """引擎不可用时冻结必须被阻止（FR-35）；这一条不需要引擎在跑，用死端口即可。"""
    finding = await live_check("http://127.0.0.1:9").check(hocon=VALID_HOCON)

    assert not finding.passed
    assert "引擎不可用" in finding.detail
