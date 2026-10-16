"""配置边界启动期校验（SPEC T-02）。

证据要求：**非法组合启动即失败** —— 不允许等到第一个写操作才暴露。
"""

import pytest
from pydantic import SecretStr, ValidationError

from data_integration_platform.config import Settings

STRONG_SECRET = "s" * 32


def build(**overrides: object) -> Settings:
    return Settings(_env_file=None, **overrides)


@pytest.mark.parametrize("environment", ["prod", "production", "PRODUCTION"])
def test_production_rejects_dev_header_identity(environment: str) -> None:
    """FR-01：生产模式禁止本地身份头，禁止客户端自行伪造身份。"""
    with pytest.raises(ValidationError, match="trusted-gateway"):
        build(environment=environment, auth_mode="dev-header")


def test_trusted_gateway_requires_shared_secret() -> None:
    with pytest.raises(ValidationError, match="DIP_GATEWAY_HMAC_SECRET"):
        build(auth_mode="trusted-gateway")


def test_trusted_gateway_rejects_short_shared_secret() -> None:
    """HMAC 的安全性直接受共享密钥熵约束，弱密钥不允许进入可复制的部署配置。"""
    with pytest.raises(ValidationError, match="至少需要 32 字节"):
        build(auth_mode="trusted-gateway", gateway_hmac_secret=SecretStr("short"))


def test_trusted_gateway_accepts_strong_secret() -> None:
    settings = build(auth_mode="trusted-gateway", gateway_hmac_secret=SecretStr(STRONG_SECRET))
    assert settings.auth_mode == "trusted-gateway"


def test_bootstrap_requires_token_when_enabled() -> None:
    """FR-05：启用一次性初始化接口就必须配 Token。"""
    with pytest.raises(ValidationError, match="DIP_BOOTSTRAP_TOKEN"):
        build(bootstrap_enabled=True)


def test_bootstrap_rejects_short_token() -> None:
    with pytest.raises(ValidationError, match="至少需要 32 字节"):
        build(bootstrap_enabled=True, bootstrap_token=SecretStr("short"))


def test_local_development_defaults_are_valid() -> None:
    settings = build()
    assert settings.is_production is False
    assert settings.auth_mode == "dev-header"
    assert settings.bootstrap_enabled is False


def test_harness_runtime_budget_matches_frozen_limits() -> None:
    """PRD §5.1 的单次运行硬上限必须原样进入冻结预算。"""
    budget = build().harness_runtime_budget()
    assert budget.max_records == 10_000_000
    assert budget.max_bytes == 10 * 1024**3
    assert budget.timeout_seconds == 3600
    assert budget.max_output_ratio == 2.0
    assert budget.max_reject_rate == 0.5
    assert budget.minimum_quality_rows == 100
    assert budget.warning_ratio == 0.8


def test_protocol_ttls_are_independent() -> None:
    """PRD §5.2：准备单 24 小时与 Capability 300 秒是两个相互独立的时效。"""
    settings = build()
    assert settings.harness_preparation_ttl_seconds == 86_400
    assert settings.harness_capability_ttl_seconds == 300
    assert settings.harness_capability_ttl_seconds < settings.harness_preparation_ttl_seconds
