"""工具契约清单（T-24 / FR-41）。

平台里**一切副作用都由清单内的工具执行**。清单条目声明版本、效果、输入 Schema、
超时、记录/字节上限、幂等性与补偿工具；三种情况在这里就拒绝：

1. **未注册的工具名** —— 没有清单就没有效果分类，PDP 也就无从判断风险；
2. **版本不匹配** —— 声明 `2.3.13` 却拿到别的版本，说明契约与实现对不上；
3. **输入越界** —— 字段缺失、多出未声明字段、或声明的量级超过清单上限。

为什么输入校验放在这里而不是等执行器报错：执行器报错时副作用可能已经发生了一半。
**输入 Schema 由 pydantic 输入模型派生**（`model_json_schema()`），
不引入 JSON Schema 校验库 —— 一份来源两处使用，避免模型与 Schema 漂移。
"""

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from data_integration_platform.domain.errors import DomainError
from data_integration_platform.domain.harness import ToolEffect, ToolManifest

# 清单里的记录/字节上限是**工具自己**能承受的量级，与平台运行预算（`RuntimeBudget`）
# 是两道独立的闸：平台预算管「这次任务允许动多少数据」，清单上限管
# 「这个工具本身设计上能吃多少」。两者都要过。
_RECEIPT_SCHEMA: Mapping[str, Any] = {
    "type": "object",
    "required": ["accepted"],
    "properties": {"accepted": {"type": "boolean"}, "reference": {"type": ["string", "null"]}},
}


class _ToolInput(BaseModel):
    """工具输入模型的公共约束：**多一个字段就拒**，不做静默丢弃。"""

    model_config = ConfigDict(extra="forbid")


# ------------------------------------------------------------------ 输入契约
class ProbeTableInput(_ToolInput):
    connection_id: str = Field(min_length=1, max_length=64)
    schema_name: str = Field(min_length=1, max_length=255)
    table_name: str = Field(min_length=1, max_length=255)
    sample_limit: int = Field(ge=1, le=100)


class SubmitJobInput(_ToolInput):
    """提交作业；配置内容本身随制品走，这里只带定位与摘要。"""

    preparation_id: str = Field(min_length=1, max_length=64)
    artifact_sha256: str = Field(min_length=64, max_length=64)
    job_name: str = Field(min_length=1, max_length=255)
    config_format: Literal["HOCON", "JSON"] = "HOCON"


class CancelJobInput(_ToolInput):
    job_id: str = Field(min_length=1, max_length=128)
    reason: str = Field(min_length=1, max_length=500)


class CreateShadowTableInput(_ToolInput):
    target_table: str = Field(min_length=1, max_length=512)
    shadow_table: str = Field(min_length=1, max_length=512)
    columns: list[str] = Field(min_length=1, max_length=4096)


class AtomicSwapInput(_ToolInput):
    target_table: str = Field(min_length=1, max_length=512)
    shadow_table: str = Field(min_length=1, max_length=512)


class DropTableInput(_ToolInput):
    """删除影子表；只允许删影子表名（由调用方保证），因此带上来源准备单以便追溯。"""

    table: str = Field(min_length=1, max_length=512)
    preparation_id: str = Field(min_length=1, max_length=64)


class RollbackRotationInput(_ToolInput):
    connection_id: str = Field(min_length=1, max_length=64)
    rollback_request_id: str = Field(min_length=1, max_length=64)


class SecretUseInput(_ToolInput):
    """使用既有凭据；`purpose` 会进证据账本，事后能回答「谁为了什么读了这把密钥」。"""

    connection_id: str = Field(min_length=1, max_length=64)
    purpose: str = Field(min_length=1, max_length=255)


class SecretRotateInput(_ToolInput):
    connection_id: str = Field(min_length=1, max_length=64)
    rotation_request_id: str = Field(min_length=1, max_length=64)


class SecretMigrateInput(_ToolInput):
    """换存放位置，不换凭据内容；源凭据保留，所以没有声明补偿工具。"""

    connection_id: str = Field(min_length=1, max_length=64)
    migration_request_id: str = Field(min_length=1, max_length=64)


class SecretDestroyInput(_ToolInput):
    connection_id: str = Field(min_length=1, max_length=64)
    destruction_request_id: str = Field(min_length=1, max_length=64)
    confirmed_name: str = Field(min_length=1, max_length=255)


class DeliverWebhookInput(_ToolInput):
    url: str = Field(min_length=1, max_length=2048)
    delivery_id: str = Field(min_length=1, max_length=128)
    payload: dict[str, Any] = Field(default_factory=dict)


# ------------------------------------------------------------------ 注册表
@dataclass(frozen=True, slots=True)
class ToolSpec:
    """清单条目 + 它的输入模型；两者绑在一起，避免「校验用 A、声明用 B」。"""

    manifest: ToolManifest
    input_model: type[BaseModel]

    def parse(self, payload: Mapping[str, Any]) -> BaseModel:
        try:
            return self.input_model.model_validate(dict(payload))
        except ValidationError as exc:
            first = exc.errors()[0]
            location = ".".join(str(part) for part in first["loc"]) or "<root>"
            raise DomainError(
                f"工具 {self.manifest.name} 的输入不符合契约：{location}: {first['msg']}"
            ) from exc


class ToolRegistry:
    """工具清单的唯一查询点；未注册与版本不匹配都在这里被拒。"""

    def __init__(self, specs: Iterable[ToolSpec]) -> None:
        self._specs: dict[tuple[str, str], ToolSpec] = {}
        for spec in specs:
            key = (spec.manifest.name, spec.manifest.version)
            if key in self._specs:
                raise DomainError(f"工具清单重复登记：{spec.manifest.name}@{spec.manifest.version}")
            self._specs[key] = spec
        self._require_compensations_registered()
        self._require_consistent_effects()

    def _require_consistent_effects(self) -> None:
        """同一工具名的所有版本效果必须一致。

        否则「这个工具危不危险」就没有确定答案：PDP 拿到 `tool_name` 与 `effect`，
        两者对不上时无论怎么判都能被另一版本推翻。
        """
        by_name: dict[str, ToolEffect] = {}
        for (name, version), spec in self._specs.items():
            known = by_name.setdefault(name, spec.manifest.effect)
            if known is not spec.manifest.effect:
                raise DomainError(
                    f"工具 {name} 的多个版本效果不一致：{known.value} 与 "
                    f"{spec.manifest.effect.value}（版本 {version}）"
                )

    def _require_compensations_registered(self) -> None:
        """声明的补偿工具必须真在清单里 —— 否则「可回滚」只是清单上的一句空话。

        这条在构造期检查：等到真出事再发现补偿工具不存在，就来不及了。
        """
        registered = {name for name, _ in self._specs}
        dangling = sorted(
            {
                f"{spec.manifest.name} → {spec.manifest.compensation_tool}"
                for spec in self._specs.values()
                if spec.manifest.compensation_tool is not None
                and spec.manifest.compensation_tool not in registered
            }
        )
        if dangling:
            raise DomainError(f"清单声明的补偿工具未注册：{'、'.join(dangling)}")

    def resolve(self, *, name: str, version: str) -> ToolSpec:
        """按名字与版本取清单条目；两者任一不匹配都拒绝，不做「取最新版」的兜底。"""
        spec = self._specs.get((name, version))
        if spec is None:
            version = f"{name}@{version}"
            if any(known_name == name for known_name, _ in self._specs):
                raise DomainError(f"工具版本未注册：{version}")
            raise DomainError(f"工具未注册：{version}")
        return spec

    def require_within_limits(
        self,
        spec: ToolSpec,
        *,
        records: int | None = None,
        bytes_: int | None = None,
    ) -> None:
        """清单上限是工具自身的承受力；请求量超过它就直接拒，不指望执行器熔断。"""
        manifest = spec.manifest
        if records is not None and records > manifest.max_records:
            raise DomainError(
                f"工具 {manifest.name} 的记录上限为 {manifest.max_records}，本次请求 {records}"
            )
        if bytes_ is not None and bytes_ > manifest.max_bytes:
            raise DomainError(
                f"工具 {manifest.name} 的字节上限为 {manifest.max_bytes}，本次请求 {bytes_}"
            )

    def names(self) -> frozenset[str]:
        return frozenset(name for name, _ in self._specs)

    def specs(self) -> tuple[ToolSpec, ...]:
        """全部清单条目，按名字与版本排序；供盘点与自检使用。"""
        return tuple(self._specs[key] for key in sorted(self._specs))


def _manifest(
    *,
    name: str,
    version: str,
    effect: ToolEffect,
    timeout_seconds: int,
    records: int,
    bytes_: int,
    idempotent: bool,
    model: type[BaseModel],
    compensation_tool: str | None = None,
) -> ToolSpec:
    """用平台硬上限做清单默认值；确有更小承受力的工具在下面显式覆盖。"""
    return ToolSpec(
        manifest=ToolManifest(
            name=name,
            version=version,
            effect=effect,
            timeout_seconds=timeout_seconds,
            max_records=records,
            max_bytes=bytes_,
            idempotent=idempotent,
            input_schema=model.model_json_schema(),
            # 输出是「收到没有」的回执，不是业务数据；回执本身不构成影响面，
            # 因此这里用稳定但宽松的回执 Schema，不为每个工具编造字段。
            output_schema=dict(_RECEIPT_SCHEMA),
            compensation_tool=compensation_tool,
        ),
        input_model=model,
    )


def build_tool_registry(*, seatunnel_version: str = "2.3.13") -> ToolRegistry:
    """平台工具目录；每个效果类别至少一个工具，PDP 才可能对五类效果都有判定。

    SeaTunnel 版本随部署变化，因此由调用方传入（它同时是生成契约里的引擎版本）。
    """
    generic_records = 1_000_000
    generic_bytes = 1024**3
    return ToolRegistry(
        [
            _manifest(
                name="metadata.probe_table",
                version="1.0.0",
                effect=ToolEffect.READ_ONLY,
                timeout_seconds=120,
                records=100,
                bytes_=64 * 1024 * 1024,
                idempotent=True,
                model=ProbeTableInput,
            ),
            _manifest(
                name="seatunnel.submit_job",
                version=seatunnel_version,
                effect=ToolEffect.WRITE,
                timeout_seconds=1800,
                records=10_000_000,
                bytes_=10 * 1024**3,
                # 提交本身不幂等：靠 Commit 阶段的能力令牌单次消费来保证只提交一次，
                # 而不是指望引擎去重。补偿工具负责收尾。
                idempotent=False,
                model=SubmitJobInput,
                compensation_tool="seatunnel.cancel_job",
            ),
            _manifest(
                name="seatunnel.cancel_job",
                version=seatunnel_version,
                effect=ToolEffect.WRITE,
                timeout_seconds=300,
                records=1,
                bytes_=1024**2,
                idempotent=True,
                model=CancelJobInput,
            ),
            _manifest(
                name="doris.create_shadow_table",
                version="3.0.8",
                effect=ToolEffect.WRITE,
                timeout_seconds=600,
                records=generic_records,
                bytes_=generic_bytes,
                idempotent=True,
                model=CreateShadowTableInput,
                compensation_tool="doris.drop_shadow_table",
            ),
            _manifest(
                name="doris.drop_shadow_table",
                version="3.0.8",
                effect=ToolEffect.DESTRUCTIVE,
                timeout_seconds=300,
                records=1,
                bytes_=1024**2,
                idempotent=True,
                model=DropTableInput,
            ),
            _manifest(
                name="doris.atomic_swap",
                version="3.0.8",
                effect=ToolEffect.DESTRUCTIVE,
                timeout_seconds=600,
                records=generic_records,
                bytes_=generic_bytes,
                # 换名之后原表就没了；重放会再换一次，所以**不幂等**。
                idempotent=False,
                model=AtomicSwapInput,
            ),
            _manifest(
                name="secret.use",
                version="1.0.0",
                effect=ToolEffect.SECRET_USE,
                timeout_seconds=60,
                records=1,
                bytes_=1024**2,
                idempotent=True,
                model=SecretUseInput,
            ),
            _manifest(
                name="secret.rotate",
                version="1.0.0",
                effect=ToolEffect.SECRET_USE,
                timeout_seconds=300,
                records=1,
                bytes_=1024**2,
                idempotent=False,
                model=SecretRotateInput,
                compensation_tool="secret.rollback_rotation",
            ),
            _manifest(
                name="secret.migrate",
                version="1.0.0",
                effect=ToolEffect.SECRET_USE,
                timeout_seconds=300,
                records=1,
                bytes_=1024**2,
                # 重放会把凭据再写一份到目标 Provider，但不会覆盖连接引用之外的东西；
                # 仍按不幂等声明 —— 「可回退」靠的是源凭据保留，不是靠重放安全。
                idempotent=False,
                model=SecretMigrateInput,
            ),
            _manifest(
                name="secret.rollback_rotation",
                version="1.0.0",
                effect=ToolEffect.SECRET_USE,
                timeout_seconds=300,
                records=1,
                bytes_=1024**2,
                idempotent=True,
                model=RollbackRotationInput,
            ),
            _manifest(
                name="secret.destroy",
                version="1.0.0",
                effect=ToolEffect.DESTRUCTIVE,
                timeout_seconds=300,
                records=1,
                bytes_=1024**2,
                idempotent=False,
                model=SecretDestroyInput,
            ),
            _manifest(
                name="alert.deliver_webhook",
                version="1.0.0",
                effect=ToolEffect.EXTERNAL_EGRESS,
                timeout_seconds=30,
                records=1,
                bytes_=1024**2,
                # 接收端按 `delivery_id` 幂等，所以重投是安全的（与凭据操作刻意相反）。
                idempotent=True,
                model=DeliverWebhookInput,
            ),
        ]
    )
