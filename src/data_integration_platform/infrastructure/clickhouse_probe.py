"""ClickHouse 元数据探查器（T-12 / GAP-01）。

需求基线 §2.8.1 要求接入 ClickHouse，参考实现没有，属必须补齐的真实缺口。

探测口径严格按决策 **D-Q1**：只覆盖基线四项（Schema、字段类型、近似统计、脱敏样本）。
压缩编码、TTL、分区键、跳数索引等属展示型元数据，列 P2 不取 —— 探查的唯一目的是
给生成阶段足够上下文，多取字段只会拖长实现而不增加可讲的技术点。

**只走系统表**：全部查询落在 `system.tables` / `system.columns`，
不碰 `system.parts`（重）也不做任何 `COUNT(*)`。
"""

from collections.abc import Sequence
from typing import Any

import clickhouse_connect

from data_integration_platform.application.profiling import (
    ProbedColumn,
    ProbeResult,
    ProbeTarget,
    ReadOnlyQuery,
)
from data_integration_platform.application.secrets import ConnectionCredentials
from data_integration_platform.config import get_settings
from data_integration_platform.domain.enums import ConnectorType
from data_integration_platform.domain.errors import DomainError, ExternalServiceUnavailable
from data_integration_platform.infrastructure.probe_support import (
    cell_to_plain,
    describe_failure,
    qualify,
    require_password,
    require_safe_identifier,
    require_username,
    row_get,
    run_blocking,
)

# clickhouse-connect 默认走 HTTP 接口；连接上配置 port 可切到原生协议端口。
DEFAULT_PORT = 8123
DEFAULT_DATABASE = "default"
_QUOTE = "`"
_PARAM_DATABASE = "{database:String}"
_PARAM_TABLE = "{table:String}"
_PARAM_LIMIT = "{row_limit:UInt32}"


def build_connection_test_query() -> ReadOnlyQuery:
    return ReadOnlyQuery(sql="SELECT 1")


def build_table_list_query() -> ReadOnlyQuery:
    return ReadOnlyQuery(
        sql=(
            "SELECT name FROM system.tables "
            f"WHERE database = {_PARAM_DATABASE} "
            f"ORDER BY name LIMIT {_PARAM_LIMIT}"
        )
    )


def build_column_query() -> ReadOnlyQuery:
    """字段清单。

    `is_in_primary_key` 是 ClickHouse 暴露主键归属的稳定字段；ClickHouse 的主键是
    `ORDER BY` 表达式而非约束，因此这里只按列粒度标注，不伪造约束语义。
    """
    return ReadOnlyQuery(
        sql=(
            "SELECT name, type, position, is_in_primary_key, comment "
            "FROM system.columns "
            f"WHERE database = {_PARAM_DATABASE} AND table = {_PARAM_TABLE} "
            "ORDER BY position"
        )
    )


def build_table_stats_query() -> ReadOnlyQuery:
    """表级近似统计。

    `total_rows` / `total_bytes` 是引擎维护的估算值，非 MergeTree 引擎可能为 NULL ——
    为空时留空，不填 0 冒充真实行数。
    """
    return ReadOnlyQuery(
        sql=(
            "SELECT engine, total_rows, total_bytes, primary_key, comment "
            "FROM system.tables "
            f"WHERE database = {_PARAM_DATABASE} AND name = {_PARAM_TABLE}"
        )
    )


def build_sample_query(*, database: str, table_name: str) -> ReadOnlyQuery:
    target = qualify(
        require_safe_identifier(database, kind="库"),
        require_safe_identifier(table_name, kind="表"),
        quote=_QUOTE,
    )
    return ReadOnlyQuery(sql=f"SELECT * FROM {target} LIMIT {_PARAM_LIMIT}")


class ClickHouseMetadataProbe:
    connector_type = ConnectorType.CLICKHOUSE

    async def test_connection(
        self, *, target: ProbeTarget, credentials: ConnectionCredentials
    ) -> None:
        await self._query(
            target=target, credentials=credentials, query=build_connection_test_query()
        )

    async def list_tables(
        self,
        *,
        target: ProbeTarget,
        credentials: ConnectionCredentials,
        schema_name: str | None,
        limit: int,
    ) -> Sequence[str]:
        database = _resolve_database(target, schema_name)
        rows = await self._query(
            target=target,
            credentials=credentials,
            query=build_table_list_query(),
            parameters={"database": database, "row_limit": limit},
        )
        return [str(row_get(row, "name")) for row in rows]

    async def probe_table(
        self,
        *,
        target: ProbeTarget,
        credentials: ConnectionCredentials,
        schema_name: str,
        table_name: str,
        sample_limit: int,
    ) -> ProbeResult:
        database = _resolve_database(target, schema_name)
        require_safe_identifier(table_name, kind="表")
        column_rows = await self._query(
            target=target,
            credentials=credentials,
            query=build_column_query(),
            parameters={"database": database, "table": table_name},
        )
        if not column_rows:
            raise DomainError(f"表不存在或不可见：{database}.{table_name}")
        columns, primary_keys = _to_columns(column_rows)
        stats_rows = await self._query(
            target=target,
            credentials=credentials,
            query=build_table_stats_query(),
            parameters={"database": database, "table": table_name},
        )
        sample_rows = await self._query(
            target=target,
            credentials=credentials,
            query=build_sample_query(database=database, table_name=table_name),
            parameters={"row_limit": sample_limit},
        )
        return ProbeResult(
            schema_name=database,
            table_name=table_name,
            columns=columns,
            primary_keys=primary_keys,
            statistics=_table_stats(stats_rows),
            sample_rows=tuple(
                {key: cell_to_plain(value) for key, value in row.items()} for row in sample_rows
            ),
        )

    async def _query(
        self,
        *,
        target: ProbeTarget,
        credentials: ConnectionCredentials,
        query: ReadOnlyQuery,
        parameters: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        return await run_blocking(
            self._query_blocking,
            target=target,
            credentials=credentials,
            query=query,
            parameters=parameters or {},
        )

    def _query_blocking(
        self,
        *,
        target: ProbeTarget,
        credentials: ConnectionCredentials,
        query: ReadOnlyQuery,
        parameters: dict[str, Any],
    ) -> list[dict[str, Any]]:
        timeout = get_settings().probe_statement_timeout_seconds
        try:
            client = clickhouse_connect.get_client(
                host=target.host or "localhost",
                port=target.port or DEFAULT_PORT,
                username=require_username(credentials, connector=self.connector_type.value),
                password=require_password(credentials, connector=self.connector_type.value),
                database=target.database_name or DEFAULT_DATABASE,
                connect_timeout=timeout,
                send_receive_timeout=timeout,
            )
        except Exception as exc:  # clickhouse-connect 的失败类型不稳定，统一收敛
            raise ExternalServiceUnavailable(
                describe_failure(exc, context=f"{self.connector_type.value} 连接")
            ) from exc
        try:
            result = client.query(query.sql, parameters=parameters)
            names = list(result.column_names)
            return [dict(zip(names, row, strict=True)) for row in result.result_rows]
        except Exception as exc:
            raise ExternalServiceUnavailable(
                describe_failure(exc, context=f"{self.connector_type.value} 探查")
            ) from exc
        finally:
            client.close()


def _resolve_database(target: ProbeTarget, schema_name: str | None) -> str:
    database = schema_name or target.database_name or DEFAULT_DATABASE
    return require_safe_identifier(database, kind="库")


def _to_columns(
    rows: Sequence[dict[str, Any]],
) -> tuple[tuple[ProbedColumn, ...], tuple[str, ...]]:
    columns: list[ProbedColumn] = []
    primary_keys: list[str] = []
    for index, row in enumerate(rows, start=1):
        name = str(row_get(row, "name"))
        is_primary_key = bool(row_get(row, "is_in_primary_key"))
        if is_primary_key:
            primary_keys.append(name)
        statistics: dict[str, Any] = {}
        if row_get(row, "comment"):
            statistics["comment"] = str(row_get(row, "comment"))
        columns.append(
            ProbedColumn(
                name=name,
                data_type=str(row_get(row, "type") or "UNKNOWN"),
                ordinal=int(row_get(row, "position") or index),
                # ClickHouse 的列可空性由类型表达（Nullable(T)），无法从系统表直接得到布尔值，
                # 因此按类型前缀推断，推断不出时留 None 而不是断言可空。
                nullable=_clickhouse_nullable(row_get(row, "type")),
                is_primary_key=is_primary_key,
                statistics=statistics,
            )
        )
    return tuple(columns), tuple(primary_keys)


def _clickhouse_nullable(data_type: Any) -> bool | None:
    if not isinstance(data_type, str):
        return None
    return data_type.startswith("Nullable(")


def _table_stats(rows: Sequence[dict[str, Any]]) -> dict[str, Any]:
    if not rows:
        return {}
    row = rows[0]
    stats: dict[str, Any] = {}
    for key in ("total_rows", "total_bytes"):
        value = row.get(key)
        if value is not None:
            stats[key] = int(value)
    for key in ("engine", "primary_key", "comment"):
        if row.get(key):
            stats[key] = str(row[key])
    return stats
