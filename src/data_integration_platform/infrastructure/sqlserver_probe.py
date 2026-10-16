"""SQL Server 元数据探查器（T-11）。

行数取 `sys.partitions` 的分区行数（存储引擎维护的元数据），不是 `COUNT(*)` 扫描。
受限行数用 `OFFSET/FETCH`，并要求 SQL Server 2012+。
"""

from collections.abc import Sequence
from typing import Any

import pytds

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

DEFAULT_PORT = 1433
DEFAULT_SCHEMA = "dbo"
_QUOTE_OPEN = "["
_QUOTE_CLOSE = "]"


def build_connection_test_query() -> ReadOnlyQuery:
    return ReadOnlyQuery(sql="SELECT 1")


def build_table_list_query() -> ReadOnlyQuery:
    return ReadOnlyQuery(
        sql=(
            "SELECT TABLE_NAME FROM INFORMATION_SCHEMA.TABLES "
            "WHERE TABLE_SCHEMA = %s AND TABLE_TYPE = 'BASE TABLE' "
            "ORDER BY TABLE_NAME OFFSET 0 ROWS FETCH NEXT %s ROWS ONLY"
        )
    )


def build_column_query() -> ReadOnlyQuery:
    return ReadOnlyQuery(
        sql=(
            "SELECT COLUMN_NAME, DATA_TYPE, IS_NULLABLE, ORDINAL_POSITION, "
            "CHARACTER_MAXIMUM_LENGTH, NUMERIC_PRECISION, NUMERIC_SCALE "
            "FROM INFORMATION_SCHEMA.COLUMNS "
            "WHERE TABLE_SCHEMA = %s AND TABLE_NAME = %s ORDER BY ORDINAL_POSITION"
        )
    )


def build_primary_key_query() -> ReadOnlyQuery:
    return ReadOnlyQuery(
        sql=(
            "SELECT kcu.COLUMN_NAME FROM INFORMATION_SCHEMA.TABLE_CONSTRAINTS tc "
            "JOIN INFORMATION_SCHEMA.KEY_COLUMN_USAGE kcu "
            "  ON kcu.CONSTRAINT_NAME = tc.CONSTRAINT_NAME "
            " AND kcu.TABLE_SCHEMA = tc.TABLE_SCHEMA "
            "WHERE tc.CONSTRAINT_TYPE = 'PRIMARY KEY' "
            "AND tc.TABLE_SCHEMA = %s AND tc.TABLE_NAME = %s"
        )
    )


def build_table_stats_query() -> ReadOnlyQuery:
    """分区行数：索引 0/1 分别对应堆与聚集索引，即「整表行数」。"""
    return ReadOnlyQuery(
        sql=(
            "SELECT SUM(p.rows) AS approx_rows FROM sys.partitions p "
            "WHERE p.object_id = OBJECT_ID(%s) AND p.index_id IN (0, 1)"
        )
    )


def build_sample_query(*, schema_name: str, table_name: str) -> ReadOnlyQuery:
    target = qualify(
        require_safe_identifier(schema_name, kind="模式"),
        require_safe_identifier(table_name, kind="表"),
        quote=_QUOTE_OPEN,
        quote_close=_QUOTE_CLOSE,
    )
    # pytds 在客户端把参数替换为字面量；该值是平台自身上限的整数，不来自外部输入。
    return ReadOnlyQuery(sql=f"SELECT TOP (%s) * FROM {target}")


class SqlServerMetadataProbe:
    connector_type = ConnectorType.SQL_SERVER

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
        return [str(row_get(row, "TABLE_NAME")) for row in rows]

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
        pk_rows = await self._fetch(
            target=target,
            credentials=credentials,
            query=build_primary_key_query(),
            args=(schema, table_name),
        )
        pk_names = tuple(_pk_value(row) for row in pk_rows)
        stats_rows = await self._fetch(
            target=target,
            credentials=credentials,
            query=build_table_stats_query(),
            args=(f"{schema}.{table_name}",),
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
            columns=_to_columns(column_rows, pk_names),
            primary_keys=pk_names,
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
        return await run_blocking(
            self._fetch_blocking,
            target=target,
            credentials=credentials,
            query=query,
            args=args,
        )

    def _fetch_blocking(
        self,
        *,
        target: ProbeTarget,
        credentials: ConnectionCredentials,
        query: ReadOnlyQuery,
        args: tuple[Any, ...],
    ) -> list[dict[str, Any]]:
        if not target.database_name:
            raise DomainError("SQL Server 连接需要填写数据库名")
        timeout = get_settings().probe_statement_timeout_seconds
        try:
            connection = pytds.connect(
                server=target.host or "localhost",
                port=target.port or DEFAULT_PORT,
                database=target.database_name,
                user=require_username(credentials, connector=self.connector_type.value),
                password=require_password(credentials, connector=self.connector_type.value),
                timeout=timeout,
                login_timeout=timeout,
            )
        except Exception as exc:  # pytds 抛出的异常类型不稳定，统一收敛
            raise ExternalServiceUnavailable(
                describe_failure(exc, context=f"{self.connector_type.value} 连接")
            ) from exc
        try:
            with connection.cursor() as cursor:
                cursor.execute(query.sql, list(args))
                if cursor.description is None:
                    return []
                names = [str(column[0]).casefold() for column in cursor.description]
                return [dict(zip(names, row, strict=True)) for row in cursor.fetchall()]
        except Exception as exc:
            raise ExternalServiceUnavailable(
                describe_failure(exc, context=f"{self.connector_type.value} 探查")
            ) from exc
        finally:
            connection.close()


def _resolve_schema(schema_name: str | None) -> str:
    return require_safe_identifier(schema_name or DEFAULT_SCHEMA, kind="模式")


def _pk_value(row: dict[str, Any]) -> str:
    return str(row_get(row, "COLUMN_NAME") or row_get(row, "column_name") or "")


def _to_columns(
    rows: Sequence[dict[str, Any]], primary_keys: Sequence[str]
) -> tuple[ProbedColumn, ...]:
    pk_set = set(primary_keys)
    columns: list[ProbedColumn] = []
    for index, row in enumerate(rows, start=1):
        name = str(row_get(row, "COLUMN_NAME") or row_get(row, "column_name"))
        columns.append(
            ProbedColumn(
                name=name,
                data_type=_format_sqlserver_type(row),
                ordinal=int(
                    row_get(row, "ORDINAL_POSITION") or row_get(row, "ordinal_position") or index
                ),
                nullable=str(
                    row_get(row, "IS_NULLABLE") or row_get(row, "is_nullable") or ""
                ).upper()
                == "YES",
                is_primary_key=name in pk_set,
            )
        )
    return tuple(columns)


def _format_sqlserver_type(row: dict[str, Any]) -> str:
    base = str(row_get(row, "DATA_TYPE") or row_get(row, "data_type") or "UNKNOWN")
    length = row_get(row, "CHARACTER_MAXIMUM_LENGTH") or row_get(row, "character_maximum_length")
    precision = row_get(row, "NUMERIC_PRECISION") or row_get(row, "numeric_precision")
    scale = row_get(row, "NUMERIC_SCALE") or row_get(row, "numeric_scale")
    if length is not None:
        return f"{base}({'max' if int(length) < 0 else int(length)})"
    if precision is not None and base in {"decimal", "numeric"}:
        return f"{base}({int(precision)},{int(scale or 0)})"
    return base


def _table_stats(rows: Sequence[dict[str, Any]]) -> dict[str, Any]:
    if not rows:
        return {}
    value = rows[0].get("approx_rows") or rows[0].get("APPROX_ROWS")
    return {"approx_rows": int(value)} if value is not None else {}
