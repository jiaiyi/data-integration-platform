"""工具契约清单（T-24 / FR-41）。

要守的证据是**三种拒绝**：未注册的工具名、版本不匹配、输入越界。
另外守住目录自身的自洽性 —— 声明的补偿工具必须真在目录里，
每个效果类别至少有一个工具（否则 PDP 会对整整一类效果无话可说）。
"""

import pytest
from pydantic import BaseModel, ConfigDict, Field

from data_integration_platform.application.harness.tools import (
    ToolRegistry,
    ToolSpec,
    _manifest,
    build_tool_registry,
)
from data_integration_platform.domain.errors import DomainError
from data_integration_platform.domain.harness import ToolEffect, ToolManifest

ENGINE_VERSION = "2.3.13"


@pytest.fixture
def registry() -> ToolRegistry:
    return build_tool_registry(seatunnel_version=ENGINE_VERSION)


class SampleInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=32)
    limit: int = Field(ge=1, le=10)


def spec(
    *,
    name: str = "demo.tool",
    version: str = "1.0.0",
    effect: ToolEffect = ToolEffect.WRITE,
    records: int = 100,
    bytes_: int = 1024,
    compensation_tool: str | None = None,
) -> ToolSpec:
    return _manifest(
        name=name,
        version=version,
        effect=effect,
        timeout_seconds=30,
        records=records,
        bytes_=bytes_,
        idempotent=True,
        model=SampleInput,
        compensation_tool=compensation_tool,
    )


# ------------------------------------------------------------------ 目录自洽
def test_catalog_covers_every_effect_class(registry: ToolRegistry) -> None:
    """五类效果各自都要有工具：缺一类，PDP 就有整整一类动作无从判定。"""
    covered = {spec.manifest.effect for spec in registry.specs()}

    assert covered == set(ToolEffect)


def test_catalog_compensations_are_registered(registry: ToolRegistry) -> None:
    """目录自己声明的补偿工具必须真在目录里。"""
    for spec_ in registry.specs():
        if spec_.manifest.compensation_tool is not None:
            assert spec_.manifest.compensation_tool in registry.names()


def test_dangling_compensation_is_rejected_at_construction() -> None:
    """声明一个不存在的补偿工具，等于在清单上写一句空话 —— 构造期就该炸。"""
    with pytest.raises(DomainError, match="补偿工具未注册"):
        ToolRegistry([spec(compensation_tool="demo.missing")])


def test_duplicate_registration_is_rejected() -> None:
    with pytest.raises(DomainError, match="重复登记"):
        ToolRegistry([spec(), spec()])


# ------------------------------------------------------------------ 三种拒绝
def test_unknown_tool_is_rejected(registry: ToolRegistry) -> None:
    with pytest.raises(DomainError, match=r"工具未注册：demo\.tool@1\.0\.0"):
        registry.resolve(name="demo.tool", version="1.0.0")


def test_known_tool_with_wrong_version_is_rejected(registry: ToolRegistry) -> None:
    """版本不匹配与完全没注册要能分清：前者说明契约与实现对不上。"""
    with pytest.raises(DomainError, match=r"工具版本未注册：seatunnel\.submit_job@9\.9\.9"):
        registry.resolve(name="seatunnel.submit_job", version="9.9.9")


def test_missing_input_field_is_rejected(registry: ToolRegistry) -> None:
    resolved = registry.resolve(name="metadata.probe_table", version="1.0.0")

    with pytest.raises(DomainError, match="不符合契约"):
        resolved.parse({"connection_id": "c1", "schema_name": "s", "table_name": "t"})


def test_extra_input_field_is_rejected(registry: ToolRegistry) -> None:
    """多带字段一律拒，不做静默丢弃 —— 丢弃会让人以为它生效了。"""
    resolved = registry.resolve(name="metadata.probe_table", version="1.0.0")

    with pytest.raises(DomainError, match="sneaky"):
        resolved.parse(
            {
                "connection_id": "c1",
                "schema_name": "s",
                "table_name": "t",
                "sample_limit": 5,
                "sneaky": "extra",
            }
        )


def test_out_of_range_input_is_rejected(registry: ToolRegistry) -> None:
    resolved = registry.resolve(name="metadata.probe_table", version="1.0.0")

    with pytest.raises(DomainError, match="sample_limit"):
        resolved.parse(
            {"connection_id": "c1", "schema_name": "s", "table_name": "t", "sample_limit": 0}
        )


def test_input_within_limits_parses(registry: ToolRegistry) -> None:
    resolved = registry.resolve(name="metadata.probe_table", version="1.0.0")

    parsed = resolved.parse(
        {"connection_id": "c1", "schema_name": "s", "table_name": "t", "sample_limit": 5}
    )

    assert parsed.sample_limit == 5


# ------------------------------------------------------------------ 上限
def test_requested_volume_above_manifest_ceiling_is_rejected(registry: ToolRegistry) -> None:
    resolved = registry.resolve(name="seatunnel.submit_job", version=ENGINE_VERSION)

    with pytest.raises(DomainError, match="记录上限"):
        registry.require_within_limits(resolved, records=10_000_001)
    with pytest.raises(DomainError, match="字节上限"):
        registry.require_within_limits(resolved, bytes_=10 * 1024**3 + 1)


def test_exactly_at_the_ceiling_is_allowed(registry: ToolRegistry) -> None:
    """上限是「不超过」而不是「小于」：正好顶格要放行，否则等于偷偷调小。"""
    resolved = registry.resolve(name="seatunnel.submit_job", version=ENGINE_VERSION)

    registry.require_within_limits(resolved, records=10_000_000, bytes_=10 * 1024**3)


# ------------------------------------------------------------------ 契约一致性
def test_input_schema_is_derived_from_the_input_model(registry: ToolRegistry) -> None:
    """Schema 与校验模型必须同源，否则会出现「声明的和实际校验的不是一回事」。"""
    resolved = registry.resolve(name="seatunnel.submit_job", version=ENGINE_VERSION)

    assert resolved.manifest.input_schema == resolved.input_model.model_json_schema()
    assert "preparation_id" in resolved.manifest.input_schema["properties"]


def test_engine_tool_versions_follow_the_engine_build() -> None:
    """引擎工具的版本随部署走：清单里写死会造成「契约说 2.3.13、实际跑别的版本」。"""
    registry = build_tool_registry(seatunnel_version="2.3.14")

    assert (
        registry.resolve(name="seatunnel.submit_job", version="2.3.14").manifest.version == "2.3.14"
    )
    with pytest.raises(DomainError):
        registry.resolve(name="seatunnel.submit_job", version=ENGINE_VERSION)


def test_tool_effects_must_not_contradict_within_a_name() -> None:
    """同一工具名的所有版本效果必须一致，否则「这个工具危险吗」没有确定答案。"""
    with pytest.raises(DomainError, match="效果不一致"):
        ToolRegistry(
            [
                spec(name="demo.tool", version="1.0.0", effect=ToolEffect.READ_ONLY),
                spec(name="demo.tool", version="2.0.0", effect=ToolEffect.DESTRUCTIVE),
            ]
        )


def test_read_only_tool_needs_no_compensation(registry: ToolRegistry) -> None:
    manifest: ToolManifest = registry.resolve(name="metadata.probe_table", version="1.0.0").manifest

    assert manifest.effect is ToolEffect.READ_ONLY
    assert manifest.compensation_tool is None
