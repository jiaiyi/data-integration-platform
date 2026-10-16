"""元数据探查端口与 Profile 生命周期（T-10）。

**只读与受限查询是硬约束**（FR-10）：
- 探查 SQL 一律由**程序生成**，绝不允许模型生成（SPEC ADR-07 明确禁止）；
- 禁止无界全表 COUNT —— 近似统计只取元数据系统表或带过滤的受限期查询；
- 样本必须带 `LIMIT`，且不超过平台上限。

**原始取样行只存活于内存**：适配器返回原始行，脱敏统一在 `ProfileService` 完成，
因此「原文不落盘」只有一个写入入口，可被测试直接证明。
"""

import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from data_integration_platform.application.masking import mask_rows
from data_integration_platform.application.secret_governance import as_utc
from data_integration_platform.application.secrets import ConnectionCredentials
from data_integration_platform.config import Settings, get_settings
from data_integration_platform.domain.enums import (
    ConnectionStatus,
    ConnectorType,
    DataEndpointRole,
    DataProfileStatus,
)
from data_integration_platform.domain.errors import DomainError, ResourceNotFound
from data_integration_platform.infrastructure.models import DataConnection, DataProfile


@dataclass(frozen=True, slots=True)
class ReadOnlyQuery:
    """一条**程序生成**的受限期只读查询。

    参数一律走绑定变量（位置参数，三种驱动都能用），不做字符串拼接 ——
    标识符则在 `require_safe_identifier` 校验后按方言加引号，两者都不把用户输入拼进 SQL。
    """

    sql: str
    params: tuple[Any, ...] = ()


@dataclass(frozen=True, slots=True)
class ProbeTarget:
    """一次探查的受管输入；不含凭据，凭据单独传递以免进入日志与指纹。"""

    connector_type: ConnectorType
    host: str | None = None
    port: int | None = None
    database_name: str | None = None
    # 数据源特有选项：文件路径、bucket、对象前缀、REST 地址、region 等。
    options: Mapping[str, str] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class ProbedColumn:
    """字段级探查结果。`nullable` 为 `None` 表示引擎未提供该信息，不用 False 冒充。"""

    name: str
    data_type: str
    ordinal: int
    nullable: bool | None = None
    is_primary_key: bool = False
    statistics: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class ProbeResult:
    """一次表级探查的结果；`sample_rows` 是**原始**取值，仅存活于内存。

    行数有两个来源，刻意分开存放，不互相冒充：
    - `statistics["approx_rows"]`：数据库引擎统计信息里的**近似**值；
    - `row_count`：文件类数据源**整份读取**后得到的**精确**行数；
      只读前缀时留 `None`（部分字节数出来的行数不是真实行数）。
    """

    schema_name: str
    table_name: str
    columns: tuple[ProbedColumn, ...]
    primary_keys: tuple[str, ...] = ()
    statistics: Mapping[str, Any] = field(default_factory=dict)
    sample_rows: tuple[Mapping[str, Any], ...] = ()
    row_count: int | None = None

    def canonical_columns(self) -> list[dict[str, Any]]:
        """落库用的字段清单；顺序稳定，便于比对 Profile 是否变化。"""
        return [
            {
                "name": column.name,
                "data_type": column.data_type,
                "ordinal": column.ordinal,
                "nullable": column.nullable,
                "is_primary_key": column.is_primary_key,
                "statistics": dict(column.statistics),
            }
            for column in self.columns
        ]


class MetadataProbePort(Protocol):
    """元数据探查端口；每种数据源一个 Adapter，新增数据源只加实现与注册表一行。"""

    connector_type: ConnectorType

    async def test_connection(
        self, *, target: ProbeTarget, credentials: ConnectionCredentials
    ) -> None:
        """只读连通性测试；失败抛出信息中不含凭据明文的领域异常。"""
        ...

    async def list_tables(
        self,
        *,
        target: ProbeTarget,
        credentials: ConnectionCredentials,
        schema_name: str | None,
        limit: int,
    ) -> Sequence[str]:
        """枚举可探查对象；只读元数据，不做全表扫描。"""
        ...

    async def probe_table(
        self,
        *,
        target: ProbeTarget,
        credentials: ConnectionCredentials,
        schema_name: str,
        table_name: str,
        sample_limit: int,
    ) -> ProbeResult:
        """探查单个对象的 Schema / 主键 / 近似统计 / 受限原始样本。"""
        ...


class ProbeRegistryPort(Protocol):
    """探查器注册表端口；实现见 `infrastructure/probe_registry.py`。

    应用层只依赖这个查找契约，不依赖任何具体 Adapter（SPEC ADR-07）。
    """

    def get(self, connector_type: ConnectorType) -> MetadataProbePort:
        """按连接器类型取探查器；未接入时抛出领域异常，不返回 None。"""
        ...


def require_profile_usable(profile: DataProfile, *, now: datetime | None = None) -> None:
    """Profile 是**有时效的事实**，不是永久 Schema（FR-13）。

    非就绪或已过期的 Profile 不可用于生成；这里必须拒绝，不能降级使用旧结构。
    """
    moment = now or datetime.now(UTC)
    if profile.status is not DataProfileStatus.READY:
        raise DomainError(f"Profile 不可用：当前状态 {profile.status.value}")
    if profile.expires_at is None:
        raise DomainError("Profile 缺少有效期，按不可用处理")
    # 时间口径先归一化：SQLite 这类后端读回的 `DateTime(timezone=True)` 不带 tzinfo，
    # 直接与 aware 的 now 比较会抛 TypeError（见 HANDOVER §5 的 as_utc 约定）。
    if as_utc(profile.expires_at) <= moment:
        raise DomainError("Profile 已过期，请重新发起元数据探查")


class ProfileService:
    """Profile 的受理、执行与落库；脱敏在此完成，是唯一的样本写入入口。"""

    async def request(
        self,
        session: AsyncSession,
        *,
        connection_id: uuid.UUID,
        actor_id: uuid.UUID,
        schema_name: str,
        table_name: str,
        endpoint_role: DataEndpointRole,
    ) -> DataProfile:
        """受理一次探查；只落 PENDING 事实，真正的只读查询交给 Worker。"""
        connection = await session.get(DataConnection, connection_id)
        if connection is None:
            raise ResourceNotFound("数据连接不存在")
        profile = DataProfile(
            connection_id=connection_id,
            schema_name=schema_name,
            table_name=table_name,
            endpoint_role=endpoint_role,
            status=DataProfileStatus.PENDING,
            requested_by=actor_id,
        )
        session.add(profile)
        await session.flush()
        return profile

    async def mark_running(self, session: AsyncSession, *, profile_id: uuid.UUID) -> DataProfile:
        profile = await self._require(session, profile_id)
        profile.status = DataProfileStatus.RUNNING
        return profile

    async def complete(
        self,
        session: AsyncSession,
        *,
        profile_id: uuid.UUID,
        result: ProbeResult,
        sample_limit: int | None = None,
    ) -> DataProfile:
        """落库探查结果：字段清单 + 主键 + 近似统计 + **脱敏后**样本。"""
        profile = await self._require(session, profile_id)
        settings = get_settings()
        limit = resolve_sample_limit(sample_limit, settings)
        columns = result.canonical_columns()
        profile.columns = columns
        profile.primary_keys = list(result.primary_keys)
        profile.statistics = dict(result.statistics)
        if result.row_count is not None:
            # 精确行数只在整份读取时才有；与近似统计分开存放。
            profile.statistics["row_count"] = result.row_count
        profile.masked_sample = list(
            mask_rows(
                columns=[column.name for column in result.columns],
                rows=result.sample_rows[:limit],
            )
        )
        profile.sample_size = len(profile.masked_sample)
        profile.expires_at = datetime.now(UTC) + timedelta(seconds=settings.profile_ttl_seconds)
        profile.status = DataProfileStatus.READY
        profile.error = None
        return profile

    async def fail(
        self, session: AsyncSession, *, profile_id: uuid.UUID, error: str
    ) -> DataProfile:
        """记录失败原因；调用方必须保证 `error` 已经过脱敏、不含凭据与原始取值。"""
        profile = await self._require(session, profile_id)
        profile.status = DataProfileStatus.FAILED
        profile.error = error[:2000]
        return profile

    async def list_for_connection(
        self, session: AsyncSession, *, connection_id: uuid.UUID
    ) -> Sequence[DataProfile]:
        return (
            await session.scalars(
                select(DataProfile)
                .where(DataProfile.connection_id == connection_id)
                .order_by(DataProfile.created_at.desc())
            )
        ).all()

    async def _require(self, session: AsyncSession, profile_id: uuid.UUID) -> DataProfile:
        profile = await session.get(DataProfile, profile_id)
        if profile is None:
            raise ResourceNotFound("Profile 不存在")
        return profile


def resolve_sample_limit(sample_limit: int | None, settings: Settings) -> int:
    """把调用方给的上限收敛到平台区间；越界一律夹紧，不放行超大采样。"""
    if sample_limit is None:
        return settings.probe_sample_limit_default
    return max(1, min(sample_limit, settings.probe_sample_limit_max))


def connection_is_probeable(connection: DataConnection) -> None:
    """探查前置条件：连接必须已通过连通性测试。"""
    if connection.status is not ConnectionStatus.READY:
        raise DomainError(f"连接未就绪（当前状态 {connection.status.value}），请先执行连通性测试")
