"""供应商中立的配置与生产边界启动期校验（SPEC T-02）。

配置只表达「平台边界」：身份模式、状态存储、观测开关、Harness 预算与文件上限。
非法的生产组合必须在**启动期**失败，不允许等到第一个写操作才暴露。
"""

from functools import lru_cache
from pathlib import Path
from typing import Literal, Self

from pydantic import AnyHttpUrl, Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from data_integration_platform.domain.supervision import RuntimeBudget

PRODUCTION_ENVIRONMENTS = frozenset({"prod", "production"})


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_prefix="DIP_",
        env_ignore_empty=True,
        extra="ignore",
    )

    # ---- 基础 ----
    app_name: str = "数据集成平台控制面"
    environment: str = "local"
    api_prefix: str = "/api/v1"

    # ---- 身份边界 ----
    auth_mode: Literal["dev-header", "trusted-gateway"] = "dev-header"
    gateway_hmac_secret: SecretStr | None = None
    gateway_max_clock_skew_seconds: int = Field(default=60, ge=5, le=300)
    bootstrap_enabled: bool = False
    bootstrap_token: SecretStr | None = None

    # ---- 状态与消息 ----
    database_url: str = "postgresql+asyncpg://dip:dip@localhost:5432/dip"
    checkpoint_database_url: str = (
        "postgresql://dip_checkpoint:dip_checkpoint@localhost:5432/dip_checkpoint"
    )
    redis_url: str = "redis://localhost:6379/0"

    # ---- 可观测 ----
    otel_enabled: bool = False
    otel_service_name: str = Field(
        default="data-integration-platform", min_length=1, max_length=255
    )
    otel_traces_endpoint: AnyHttpUrl | None = None
    otel_export_timeout_seconds: float = Field(default=10.0, gt=0, le=120)
    otel_trace_sample_ratio: float = Field(default=1.0, ge=0, le=1)

    # ---- Harness 协议与预算 ----
    harness_policy_bundle_version: str = Field(
        default="harness-v1-20261016", min_length=1, max_length=128
    )
    harness_preparation_ttl_seconds: int = Field(default=86_400, ge=300, le=604_800)
    harness_capability_ttl_seconds: int = Field(default=300, ge=30, le=900)
    harness_runtime_max_records: int = Field(default=10_000_000, ge=1, le=10_000_000_000)
    harness_runtime_max_bytes: int = Field(default=10 * 1024**3, ge=1024, le=10 * 1024**4)
    harness_runtime_timeout_seconds: int = Field(default=3600, ge=60, le=604_800)
    harness_runtime_max_output_ratio: float = Field(default=2.0, ge=1.0, le=1000.0)
    harness_runtime_max_reject_rate: float = Field(default=0.5, ge=0.0, le=1.0)
    harness_runtime_minimum_quality_rows: int = Field(default=100, ge=1, le=10_000_000_000)
    harness_runtime_warning_ratio: float = Field(default=0.8, gt=0.0, lt=1.0)

    # ---- 受管文件资产 ----
    file_asset_root: Path = Path("var/file-assets")
    file_asset_max_bytes: int = Field(default=25 * 1024 * 1024, ge=1024, le=1024**3)
    file_asset_max_rows: int = Field(default=100_000, ge=1, le=10_000_000)

    @property
    def is_production(self) -> bool:
        return self.environment.strip().casefold() in PRODUCTION_ENVIRONMENTS

    def harness_runtime_budget(self) -> RuntimeBudget:
        """把平台硬上限转换为冻结运行预算；所有执行入口复用同一构造口径。"""
        return RuntimeBudget(
            max_records=self.harness_runtime_max_records,
            max_bytes=self.harness_runtime_max_bytes,
            timeout_seconds=self.harness_runtime_timeout_seconds,
            max_output_ratio=self.harness_runtime_max_output_ratio,
            max_reject_rate=self.harness_runtime_max_reject_rate,
            minimum_quality_rows=self.harness_runtime_minimum_quality_rows,
            warning_ratio=self.harness_runtime_warning_ratio,
        )

    @model_validator(mode="after")
    def require_production_boundaries(self) -> Self:
        """生产环境必须使用可信网关身份，且共享密钥与 Bootstrap Token 具备足够熵。"""
        if self.is_production and self.auth_mode != "trusted-gateway":
            raise ValueError("生产环境必须设置 DIP_AUTH_MODE=trusted-gateway")
        secret = self.gateway_hmac_secret
        if self.auth_mode == "trusted-gateway":
            if secret is None:
                raise ValueError("可信网关认证必须配置 DIP_GATEWAY_HMAC_SECRET")
            # HMAC 的安全性直接受共享密钥熵约束，弱密钥不允许进入可复制的部署配置。
            if len(secret.get_secret_value().encode("utf-8")) < 32:
                raise ValueError("可信网关 HMAC 共享密钥至少需要 32 字节")
        if self.bootstrap_enabled and self.bootstrap_token is None:
            raise ValueError("启用首次初始化接口必须配置 DIP_BOOTSTRAP_TOKEN")
        token = self.bootstrap_token
        if token is not None and len(token.get_secret_value().encode("utf-8")) < 32:
            raise ValueError("首次初始化 Token 至少需要 32 字节")
        return self


@lru_cache
def get_settings() -> Settings:
    """进程级单例配置；测试可通过 `get_settings.cache_clear()` 重置。"""
    return Settings()
