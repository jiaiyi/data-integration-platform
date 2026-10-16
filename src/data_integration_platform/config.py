"""供应商中立的配置与生产边界启动期校验（SPEC T-02）。

配置只表达「平台边界」：身份模式、状态存储、观测开关、Harness 预算与文件上限。
非法的生产组合必须在**启动期**失败，不允许等到第一个写操作才暴露。
"""

from functools import lru_cache
from pathlib import Path
from typing import Literal, Self

from cryptography.fernet import Fernet
from pydantic import AnyHttpUrl, Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from data_integration_platform.domain.supervision import RuntimeBudget

PRODUCTION_ENVIRONMENTS = frozenset({"prod", "production"})


def _parse_host_list(raw: str) -> frozenset[str]:
    """解析逗号分隔的主机白名单；大小写不敏感，空串得到空集合（= 全部拒绝）。"""
    return frozenset(host.strip().casefold() for host in raw.split(",") if host.strip())


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
    # 一律写 127.0.0.1 而不是 localhost：Windows 上 `localhost` 会优先解析到 IPv6 `::1`，
    # 而 Docker 默认只发布 IPv4，于是每次连接都要先等 `::1` 失败再回退 ——
    # 实测 Redis 走 localhost 耗时 5.03s、走 127.0.0.1 只要 0.02s，且无超时的客户端会直接挂住。
    database_url: str = "postgresql+asyncpg://dip:dip@127.0.0.1:5432/dip"
    checkpoint_database_url: str = (
        "postgresql://dip_checkpoint:dip_checkpoint@127.0.0.1:5432/dip_checkpoint"
    )
    # 与 infra/compose.yaml 的端口发布保持一致：宿主 6379 常被本机原生 Redis 占用，
    # 那种情况下 Docker 不报错、只是端口发布静默失效，连上去的是另一个 Redis。
    redis_url: str = "redis://127.0.0.1:16379/0"

    # ---- 凭据存储（T-14）----
    secret_provider: Literal["fernet-db", "vault-kv-v2"] = "fernet-db"
    secret_master_key: SecretStr | None = None
    vault_address: AnyHttpUrl | None = None
    vault_token: SecretStr | None = None
    vault_namespace: str | None = Field(default=None, max_length=255)
    vault_kv_mount: str = Field(default="secret", min_length=1, max_length=128)
    vault_path_prefix: str = Field(default="dip/connections", min_length=1, max_length=384)
    vault_timeout_seconds: float = Field(default=10.0, gt=0, le=120)
    vault_verify_tls: bool = True
    # 轮换回退保留期：激活后多久之内仍可回到旧凭据（FR-20）。
    secret_rotation_rollback_days: int = Field(default=7, ge=1, le=365)

    # ---- 凭据到期告警（T-15 / FR-22）----
    credential_alert_scan_interval_seconds: int = Field(default=3600, ge=60, le=86_400)
    credential_alert_warning_cooldown_hours: int = Field(default=24, ge=1, le=720)
    credential_alert_overdue_cooldown_hours: int = Field(default=6, ge=1, le=720)
    credential_alert_webhook_url: AnyHttpUrl | None = None
    # 配置后按 HMAC-SHA256 对请求体签名（X-DIP-Signature），供接收端验真。
    credential_alert_webhook_signing_secret: SecretStr | None = None
    credential_alert_webhook_timeout_seconds: float = Field(default=10.0, gt=0, le=120)
    # 平台主动外发（Webhook）的出口白名单；留空表示全部拒绝（失败关闭）。
    credential_alert_webhook_allowed_hosts: str = ""

    # ---- 元数据探查（T-10 ~ T-13）----
    # Profile 是「有时效的事实」，不是永久 Schema（FR-13）。
    profile_ttl_seconds: int = Field(default=86_400, ge=300, le=604_800)
    # 受限查询：不允许无界全表 COUNT，采样行数有默认值与硬上限（FR-10）。
    probe_sample_limit_default: int = Field(default=20, ge=1, le=100)
    probe_sample_limit_max: int = Field(default=100, ge=1, le=1000)
    probe_statement_timeout_seconds: int = Field(default=15, ge=1, le=300)

    # ---- 远端文件探查（T-16 GAP-02 对象存储 / HDFS）----
    # 只做前缀范围采样，严禁全量下载（决策 D-Q3）：对象可达 GB 级，
    # 全量下载会打爆控制面内存与临时盘，属正确性问题而非性能优化。
    remote_file_sample_bytes: int = Field(
        default=8 * 1024 * 1024, ge=64 * 1024, le=64 * 1024 * 1024
    )

    # ---- REST API 数据源（T-17 GAP-03）----
    # 出口主机白名单：逗号分隔；留空表示全部拒绝（失败关闭，不是全部放行）。
    rest_api_allowed_hosts: str = ""
    rest_api_timeout_seconds: float = Field(default=15.0, gt=0, le=120)
    rest_api_max_sample_bytes: int = Field(default=4 * 1024 * 1024, ge=1024, le=64 * 1024 * 1024)

    # ---- 生成与模型（T-18 / FR-24 ~ FR-33）----
    # 生成后端：只改变「候选如何产生」，不影响门禁、版本、审批与 Harness 行为（FR-28）。
    generation_backend: Literal["direct", "seatunnel-cli"] = "direct"
    llm_base_url: AnyHttpUrl | None = None
    llm_api_key: SecretStr | None = None
    llm_model: str | None = Field(default=None, max_length=255)
    llm_request_timeout_seconds: float = Field(default=180, gt=0, le=600)
    # 候选总量上限 = 首次 1 份 + 修复次数。PRD 决策 Q1 定档为 3 份，且三类失败共享同一计数。
    max_enterprise_repair_attempts: int = Field(default=2, ge=0, le=10)
    # 逐行 LLM Transform：默认**严格关闭**；开启需独立密钥与出口白名单（FR-33 / 决策 Q7）。
    row_llm_transform_enabled: bool = False
    row_llm_api_key: SecretStr | None = None
    row_llm_allowed_api_hosts: str = ""
    row_llm_max_rows_per_second: int = Field(default=10, ge=1, le=100)

    # ---- 数据面引擎（T-21）----
    seatunnel_api_url: AnyHttpUrl = Field(default=AnyHttpUrl("http://127.0.0.1:8080"))
    # 引擎版本同时是工具清单里 `seatunnel.*` 的版本：写死会让「清单说 2.3.13、实际跑别的版本」
    # 这种不一致再也查不出来（T-24 的版本不匹配检查就失效了）。
    seatunnel_version: str = Field(default="2.3.13", min_length=1, max_length=64)
    seatunnel_timezone: str = Field(default="UTC", min_length=1, max_length=64)
    seatunnel_check_timeout_seconds: int = Field(default=60, ge=5, le=600)

    # ---- 制品存储（T-22）----
    artifact_root: Path = Path("var/artifacts")

    # ---- 调度（T-23）----
    # 失火宽限：触发时刻过去超过这个秒数才算失火，按策略处理。默认值取参考实现口径
    # （PRD §5.3 决策 Q5：这类秒数属可调实现参数，验收只看「失火按策略处理」这个行为）。
    schedule_misfire_grace_seconds: int = Field(default=120, ge=1, le=86_400)

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
    # 分离签名（T-28）：私钥只给签发方（API / 授权服务），执行方只挂公钥映射。
    # 两者都是 base64url 的裸 32 字节；公钥映射写成 `key_id:密钥,key_id:密钥`
    # 以便轮换期间旧令牌仍可验签。
    harness_signing_key_id: str = Field(default="harness-local-1", min_length=1, max_length=128)
    harness_signing_private_key: SecretStr | None = None
    harness_signing_public_keys: str = ""
    # 事务 Outbox（T-30）：租约到期后命令重新可领；重试到上限即判失败，不再无限重投。
    harness_outbox_lease_seconds: int = Field(default=60, ge=5, le=3600)
    harness_outbox_max_attempts: int = Field(default=5, ge=1, le=100)
    # 定时扫描（T-39）。这几个秒数属**可调实现参数**（PRD 决策 Q5），
    # 验收只看「按间隔触发」这个行为，不核具体秒数。
    worker_heartbeat_interval_seconds: int = Field(default=10, ge=1, le=3600)
    schedule_scan_interval_seconds: int = Field(default=30, ge=5, le=3600)
    harness_outbox_scan_interval_seconds: int = Field(default=2, ge=1, le=300)
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

    def harness_public_key_ids(self) -> frozenset[str]:
        """公钥映射里的 key_id 集合；格式错误的条目直接跳过（启动期另有校验拦下）。"""
        return frozenset(
            key_id.strip()
            for item in self.harness_signing_public_keys.split(",")
            if (key_id := item.partition(":")[0].strip()) and ":" in item
        )

    def allowed_rest_api_hosts(self) -> frozenset[str]:
        """解析出口主机白名单；大小写不敏感，空配置表示不放行任何主机。"""
        return _parse_host_list(self.rest_api_allowed_hosts)

    def allowed_webhook_hosts(self) -> frozenset[str]:
        """Webhook 外发白名单；与 REST 数据源分开配置，两者都失败关闭。"""
        return _parse_host_list(self.credential_alert_webhook_allowed_hosts)

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
        self._require_credential_governance_boundaries()
        self._require_generation_boundaries()
        self._require_harness_signing_boundaries()
        return self

    def _require_harness_signing_boundaries(self) -> None:
        """签名材料的部署边界（T-28 / FR-45）。

        生产环境必须有公钥映射：没有公钥就无法验签，任何写操作都该被拒（T-32），
        那等于整个平台不可用 —— 与其等到第一个写请求才炸，不如启动期就拒绝。

        **不要求私钥**：执行方（Worker）按设计只挂公钥，要求它配私钥会把
        「Producer 才能签发」这条边界直接抹掉。
        """
        raw = self.harness_signing_public_keys.strip()
        if not raw:
            if self.is_production:
                raise ValueError("生产环境必须配置 DIP_HARNESS_SIGNING_PUBLIC_KEYS")
            return
        for item in (part.strip() for part in raw.split(",")):
            if not item:
                continue
            key_id, separator, value = item.partition(":")
            if not separator or not key_id or not value:
                raise ValueError(
                    f"DIP_HARNESS_SIGNING_PUBLIC_KEYS 的条目应为 `key_id:base64url`：{item!r}"
                )
        private_key = self.harness_signing_private_key
        if private_key is not None and len(private_key.get_secret_value()) < 16:
            raise ValueError("签名私钥不像是 base64url 的 32 字节裸密钥，请检查配置")

    def _require_generation_boundaries(self) -> None:
        """逐行 LLM Transform 的开启边界（FR-33 / 决策 Q7）。

        模型能力默认**严格关闭**；一旦开启，就必须具备独立密钥与出口主机白名单 ——
        这两条是「开启有边界」的实质内容，不是可选装饰。
        """
        key = self.row_llm_api_key
        if self.row_llm_transform_enabled and key is None:
            raise ValueError("启用逐行 LLM Transform 必须配置 DIP_ROW_LLM_API_KEY")
        if self.row_llm_transform_enabled and not _parse_host_list(self.row_llm_allowed_api_hosts):
            raise ValueError(
                "启用逐行 LLM Transform 必须配置 DIP_ROW_LLM_ALLOWED_API_HOSTS"
                "（出口白名单为空表示拒绝全部外发）"
            )
        if key is not None and len(key.get_secret_value().encode("utf-8")) < 16:
            raise ValueError("逐行 LLM Transform 的密钥过短，请使用独立且足够强的密钥")
        # 模型未配置不在这里报错：生成能力未就绪要**明确报告**而不是启动即挂（PRD §5.3）。

    def _require_credential_governance_boundaries(self) -> None:
        """凭据治理的部署边界：存储必须可用，外发告警必须受控。

        本地允许「未就绪启动」——`/health` 会明确报告未就绪（PRD §5.3 不静默失败），
        写凭据时失败关闭；生产则必须在启动期就拒绝。
        """
        webhook = self.credential_alert_webhook_url
        if (
            webhook is not None
            and self.is_production
            and self.credential_alert_webhook_signing_secret is None
        ):
            # 生产环境下未签名的外发告警无法被接收端验真，按失败关闭处理。
            raise ValueError(
                "生产环境配置凭据告警 Webhook 必须同时配置"
                " DIP_CREDENTIAL_ALERT_WEBHOOK_SIGNING_SECRET"
            )
        if webhook is not None and not self.allowed_webhook_hosts():
            raise ValueError(
                "配置了凭据告警 Webhook 就必须在 "
                "DIP_CREDENTIAL_ALERT_WEBHOOK_ALLOWED_HOSTS 中放行其主机"
            )
        if self.secret_provider == "vault-kv-v2":
            if self.vault_address is None:
                raise ValueError("secret_provider=vault-kv-v2 必须配置 DIP_VAULT_ADDRESS")
            if self.vault_token is None:
                raise ValueError("secret_provider=vault-kv-v2 必须配置 DIP_VAULT_TOKEN")
            return

        key = self.secret_master_key
        if key is None:
            if self.is_production:
                raise ValueError("生产环境使用 fernet-db 必须配置 DIP_SECRET_MASTER_KEY")
            return
        try:
            Fernet(key.get_secret_value().encode("utf-8"))
        except (ValueError, TypeError) as exc:
            raise ValueError(
                "DIP_SECRET_MASTER_KEY 不是合法的 Fernet 密钥"
                '（生成：python -c "from cryptography.fernet import Fernet;'
                ' print(Fernet.generate_key().decode())"）'
            ) from exc


@lru_cache
def get_settings() -> Settings:
    """进程级单例配置；测试可通过 `get_settings.cache_clear()` 重置。"""
    return Settings()
