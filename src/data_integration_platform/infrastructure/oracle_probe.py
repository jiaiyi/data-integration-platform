"""Oracle 元数据探查器（T-11）。

行数取 `all_tables.num_rows`（最近一次统计采集的值），**不执行任何 COUNT(*)`**；
从未采集过统计时为 NULL，此时留空而不是填 0。

受限行数统一用 `ROWNUM` 而非 `FETCH FIRST`，避免对数据库版本（12c+）作假设。
"""

from collections.abc import Sequence
from typing import Any

import oracledb

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
    run_blocking,
)

DEFAULT_PORT = 1521
_QUOTE = '"'


def build_connection_test_query() -> ReadOnlyQuery:
    return ReadOnlyQuery(sql="SELECT 1 FROM DUAL")


def build_table_list_query() -> ReadOnlyQuery:
    return ReadOnlyQuery(
        sql=(
            "SELECT * FROM ("
            "  SELECT table_name FROM all_tables WHERE owner = :1 ORDER BY table_name"
            ") WHERE ROWNUM <= :2"
        )
    )


def build_column_query() -> ReadOnlyQuery:
    return ReadOnlyQuery(
        sql=(
            "SELECT column_name, data_type, data_length, data_precision, data_scale, "
            "nullable, column_id FROM all_tab_columns "
            "WHERE owner = :1 AND table_name = :2 ORDER BY column_id"
        )
    )


def build_primary_key_query() -> ReadOnlyQuery:
    return ReadOnlyQuery(
        sql=(
            "SELECT cc.column_name FROM all_constraints c "
            "JOIN all_cons_columns cc "
            "  ON cc.owner = c.owner AND cc.constraint_name = c.constraint_name "
            "WHERE c.constraint_type = 'P' AND c.owner = :1 AND c.table_name = :2"
        )
    )


def build_table_stats_query() -> ReadOnlyQuery:
    return ReadOnlyQuery(
        sql=(
            "SELECT num_rows, blocks, last_analyzed FROM all_tables "
            "WHERE owner = :1 AND table_name = :2"
        )
    )


def build_sample_query(*, owner: str, table_name: str) -> ReadOnlyQuery:
    target = qualify(
        require_safe_identifier(owner, kind="模式"),
        require_safe_identifier(table_name, kind="表"),
        quote=_QUOTE,
    )
    # ROWNUM 必须在子查询里先取，否则是「先排序再截断」以外的语义。
    return ReadOnlyQuery(sql=f"SELECT * FROM {target} WHERE ROWNUM <= :1")


class OracleMetadataProbe:
    connector_type = ConnectorType.ORACLE

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
        owner = _resolve_owner(schema_name)
        rows = await self._fetch(
            target=target,
            credentials=credentials,
            query=build_table_list_query(),
            args=(owner, limit),
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
        owner = _resolve_owner(schema_name)
        require_safe_identifier(table_name, kind="表")
        column_rows = await self._fetch(
            target=target,
            credentials=credentials,
            query=build_column_query(),
            args=(owner, table_name),
        )
        if not column_rows:
            raise DomainError(f"表不存在或不可见：{owner}.{table_name}")
        primary_keys = await self._fetch(
            target=target,
            credentials=credentials,
            query=build_primary_key_query(),
            args=(owner, table_name),
        )
        pk_names = tuple(str(row["column_name"]) for row in primary_keys)
        columns = _to_columns(column_rows, pk_names)
        stats_rows = await self._fetch(
            target=target,
            credentials=credentials,
            query=build_table_stats_query(),
            args=(owner, table_name),
        )
        sample_rows = await self._fetch(
            target=target,
            credentials=credentials,
            query=build_sample_query(owner=owner, table_name=table_name),
            args=(sample_limit,),
        )
        return ProbeResult(
            schema_name=owner,
            table_name=table_name,
            columns=columns,
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
            raise DomainError("Oracle 连接需要在连接配置上填写服务名（Service Name）")
        timeout = get_settings().probe_statement_timeout_seconds
        try:
            connection = oracledb.connect(
                user=require_username(credentials, connector=self.connector_type.value),
                password=require_password(credentials, connector=self.connector_type.value),
                host=target.host or "localhost",
                port=target.port or DEFAULT_PORT,
                service_name=target.database_name,
                tcp_connect_timeout=timeout,
            )
        except oracledb.Error as exc:
            raise ExternalServiceUnavailable(
                describe_failure(exc, context=f"{self.connector_type.value} 连接")
            ) from exc
        try:
            # call_timeout 是连接级属性（毫秒），在这里设置即可约束本连接的所有语句。
            connection.call_timeout = timeout * 1000
            with connection.cursor() as cursor:
                cursor.execute(query.sql, args or None)
                if cursor.description is None:
                    return []
                names = [str(column[0]).casefold() for column in cursor.description]
                return [dict(zip(names, row, strict=True)) for row in cursor.fetchall()]
        except oracledb.Error as exc:
            raise ExternalServiceUnavailable(
                describe_failure(exc, context=f"{self.connector_type.value} 探查")
            ) from exc
        finally:
            connection.close()


def _resolve_owner(schema_name: str | None) -> str:
    if not schema_name:
        raise DomainError("Oracle 探查需要指定模式（Owner）名")
    # Oracle 标识符默认大写；不强制转换，交给调用方给出实际大小写。
    return require_safe_identifier(schema_name, kind="模式")


def _to_columns(
    rows: Sequence[dict[str, Any]], primary_keys: Sequence[str]
) -> tuple[ProbedColumn, ...]:
    pk_set = set(primary_keys)
    columns: list[ProbedColumn] = []
    for index, row in enumerate(rows, start=1):
        name = str(row["column_name"])
        columns.append(
            ProbedColumn(
                name=name,
                data_type=_format_oracle_type(row),
                ordinal=int(row.get("column_id") or index),
                nullable=str(row.get("nullable") or "").upper() == "Y",
                is_primary_key=name in pk_set,
            )
        )
    return tuple(columns)


def _format_oracle_type(row: dict[str, Any]) -> str:
    base = str(row.get("data_type") or "UNKNOWN")
    precision = row.get("data_precision")
    scale = row.get("data_scale")
    if precision is not None and "NUMBER" in base.upper():
        return base if scale is None else f"{base}({int(precision)},{int(scale)})"
    if base.upper() in {"VARCHAR2", "NVARCHAR2", "CHAR", "RAW"} and row.get("data_length"):
        return f"{base}({int(row['data_length'])})"
    return base


def _table_stats(rows: Sequence[dict[str, Any]]) -> dict[str, Any]:
    if not rows:
        return {}
    row = rows[0]
    stats: dict[str, Any] = {}
    num_rows = row.get("num_rows")
    # 未采集统计时 num_rows 为 NULL：留空，不填 0。
    if num_rows is not None:
        stats["approx_rows"] = int(num_rows)
    if row.get("blocks") is not None:
        stats["blocks"] = int(row["blocks"])
    if row.get("last_analyzed") is not None:
        stats["last_analyzed"] = str(row["last_analyzed"])
    return stats
