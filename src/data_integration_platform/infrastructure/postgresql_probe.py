"""PostgreSQL 元数据探查器（T-11）。

asyncpg 是原生异步驱动，因此无需线程卸载。行数取 `pg_class.reltuples`
（优化器统计的近似值），不执行任何 `COUNT(*)`。
"""

from collections.abc import Sequence
from typing import Any

import asyncpg

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
)

DEFAULT_PORT = 5432
DEFAULT_SCHEMA = "public"
_QUOTE = '"'


def build_connection_test_query() -> ReadOnlyQuery:
    return ReadOnlyQuery(sql="SELECT 1")


def build_table_list_query() -> ReadOnlyQuery:
    return ReadOnlyQuery(
        sql=(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema = $1 AND table_type = 'BASE TABLE' "
            "ORDER BY table_name LIMIT $2"
        )
    )


def build_column_query() -> ReadOnlyQuery:
    """字段清单 + 主键一次查完，避免第二次往返。"""
    return ReadOnlyQuery(
        sql=(
            "SELECT c.column_name, c.data_type, c.is_nullable, c.ordinal_position, "
            "c.character_maximum_length, c.numeric_precision, c.numeric_scale, "
            "COALESCE(pk.is_primary_key, false) AS is_primary_key "
            "FROM information_schema.columns c "
            "LEFT JOIN ("
            "  SELECT kcu.column_name, true AS is_primary_key "
            "  FROM information_schema.table_constraints tc "
            "  JOIN information_schema.key_column_usage kcu "
            "    ON kcu.constraint_name = tc.constraint_name "
            "   AND kcu.table_schema = tc.table_schema "
            "  WHERE tc.constraint_type = 'PRIMARY KEY' "
            "    AND tc.table_schema = $1 AND tc.table_name = $2"
            ") pk ON pk.column_name = c.column_name "
            "WHERE c.table_schema = $1 AND c.table_name = $2 "
            "ORDER BY c.ordinal_position"
        )
    )


def build_table_stats_query() -> ReadOnlyQuery:
    """近似行数来自优化器统计，从未 ANALYZE 过时 reltuples 为 -1，调用方按「不可用」处理。"""
    return ReadOnlyQuery(
        sql=(
            "SELECT c.reltuples::bigint AS approx_rows, "
            "pg_total_relation_size(c.oid) AS total_bytes "
            "FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
            "WHERE n.nspname = $1 AND c.relname = $2"
        )
    )


def build_sample_query(*, schema_name: str, table_name: str) -> ReadOnlyQuery:
    target = qualify(
        require_safe_identifier(schema_name, kind="模式"),
        require_safe_identifier(table_name, kind="表"),
        quote=_QUOTE,
    )
    return ReadOnlyQuery(sql=f"SELECT * FROM {target} LIMIT $1")


class PostgreSqlMetadataProbe:
    connector_type = ConnectorType.POSTGRESQL

    async def test_connection(
        self, *, target: ProbeTarget, credentials: ConnectionCredentials
    ) -> None:
        await self._fetch(
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
        schema = _resolve_schema(schema_name)
        rows = await self._fetch(
            target=target,
            credentials=credentials,
            query=build_table_list_query(),
            args=(schema, limit),
        )
        return [str(row["table_name"]) for row in rows]

    async def probe_table(
        self,
        *,
        target: ProbeTarget,
        credentials: ConnectionCredentials,
        schema_name: str,
        table_name: str,
        sample_limit: int,
    ) -> ProbeResult:
        schema = _resolve_schema(schema_name)
        require_safe_identifier(table_name, kind="表")
        column_rows = await self._fetch(
            target=target,
            credentials=credentials,
            query=build_column_query(),
            args=(schema, table_name),
        )
        if not column_rows:
            raise DomainError(f"表不存在或不可见：{schema}.{table_name}")
        columns, primary_keys = _to_columns(column_rows)
        stats_rows = await self._fetch(
            target=target,
            credentials=credentials,
            query=build_table_stats_query(),
            args=(schema, table_name),
        )
        sample_rows = await self._fetch(
            target=target,
            credentials=credentials,
            query=build_sample_query(schema_name=schema, table_name=table_name),
            args=(sample_limit,),
        )
        return ProbeResult(
            schema_name=schema,
            table_name=table_name,
            columns=columns,
            primary_keys=primary_keys,
            statistics=_table_stats(stats_rows),
            sample_rows=tuple(
                {key: cell_to_plain(value) for key, value in row.items()} for row in sample_rows
            ),
        )

    async def _fetch(
        self,
        *,
        target: ProbeTarget,
        credentials: ConnectionCredentials,
        query: ReadOnlyQuery,
        args: tuple[Any, ...] = (),
    ) -> list[dict[str, Any]]:
        settings = get_settings()
        timeout = settings.probe_statement_timeout_seconds
        try:
            connection = await asyncpg.connect(
                host=target.host or "localhost",
                port=target.port or DEFAULT_PORT,
                user=require_username(credentials, connector=self.connector_type.value),
                password=require_password(credentials, connector=self.connector_type.value),
                database=target.database_name,
                timeout=timeout,
                command_timeout=timeout,
            )
        except (asyncpg.PostgresError, OSError, TimeoutError) as exc:
            raise ExternalServiceUnavailable(
                describe_failure(exc, context=f"{self.connector_type.value} 连接")
            ) from exc
        try:
            records = await connection.fetch(query.sql, *args)
        except (asyncpg.PostgresError, TimeoutError) as exc:
            raise ExternalServiceUnavailable(
                describe_failure(exc, context=f"{self.connector_type.value} 探查")
            ) from exc
        finally:
            await connection.close()
        return [dict(record) for record in records]


def _resolve_schema(schema_name: str | None) -> str:
    """未指定模式时取 PostgreSQL 惯例的 `public`，并同样做标识符校验。"""
    return require_safe_identifier(schema_name or DEFAULT_SCHEMA, kind="模式")


def _format_pg_type(row: Any) -> str:
    """把类型与长度/精度拼成可读类型串，例如 `character varying(191)`、`numeric(12,2)`。"""
    base = str(row.get("data_type") or "UNKNOWN")
    length = row.get("character_maximum_length")
    precision = row.get("numeric_precision")
    scale = row.get("numeric_scale")
    if length is not None:
        return f"{base}({int(length)})"
    if precision is not None and "numeric" in base:
        return f"{base}({int(precision)},{int(scale or 0)})"
    return base


def _to_columns(
    rows: Sequence[Any],
) -> tuple[tuple[ProbedColumn, ...], tuple[str, ...]]:
    columns: list[ProbedColumn] = []
    primary_keys: list[str] = []
    for index, row in enumerate(rows, start=1):
        name = str(row["column_name"])
        is_primary_key = bool(row.get("is_primary_key"))
        if is_primary_key:
            primary_keys.append(name)
        columns.append(
            ProbedColumn(
                name=name,
                data_type=_format_pg_type(row),
                ordinal=int(row.get("ordinal_position") or index),
                nullable=str(row.get("is_nullable") or "").upper() == "YES",
                is_primary_key=is_primary_key,
            )
        )
    return tuple(columns), tuple(primary_keys)


def _table_stats(rows: Sequence[Any]) -> dict[str, Any]:
    if not rows:
        return {}
    row = rows[0]
    stats: dict[str, Any] = {}
    approx = row.get("approx_rows")
    # reltuples 为 -1/0 表示统计信息不可用，此时留空而不是填 0 冒充真实行数。
    if approx is not None and int(approx) > 0:
        stats["approx_rows"] = int(approx)
    if row.get("total_bytes") is not None:
        stats["total_bytes"] = int(row["total_bytes"])
    return stats


__all__ = [
    "PostgreSqlMetadataProbe",
    "build_column_query",
    "build_connection_test_query",
    "build_sample_query",
    "build_table_list_query",
    "build_table_stats_query",
]
