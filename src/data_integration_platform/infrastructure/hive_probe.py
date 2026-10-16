"""Hive 元数据探查器（T-11，超集清单）。

Hive 的元数据接口与 JDBC 系不同：字段走 `DESCRIBE`，统计走 `DESCRIBE FORMATTED`
里已采集的 `numRows`/`totalSize`。**不执行 `ANALYZE TABLE`** —— 那是写操作，
探查必须保持只读。

Hive 没有主键约束，因此 `primary_keys` 恒为空，并在统计里显式标注这一事实，
而不是让调用方误以为「查到了但没有主键」。
"""

from collections.abc import Sequence
from typing import Any

from pyhive import hive

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
    optional_secret,
    qualify,
    require_safe_identifier,
    require_username,
    row_get,
    run_blocking,
)

DEFAULT_PORT = 10000
_QUOTE = "`"
# DESCRIBE 在分区信息之前会插入分隔行；遇到以 # 开头或空名的行即字段清单结束。
_DESCRIBE_SECTION_MARKER = "#"


def build_connection_test_query() -> ReadOnlyQuery:
    return ReadOnlyQuery(sql="SELECT 1")


def build_table_list_query(*, schema_name: str) -> ReadOnlyQuery:
    """`SHOW TABLES` 不支持 LIMIT，因此限制在调用方取到结果后按平台上限截断。"""
    database = qualify(require_safe_identifier(schema_name, kind="库"), quote=_QUOTE)
    return ReadOnlyQuery(sql=f"SHOW TABLES IN {database}")


def build_column_query(*, schema_name: str, table_name: str) -> ReadOnlyQuery:
    target = qualify(
        require_safe_identifier(schema_name, kind="库"),
        require_safe_identifier(table_name, kind="表"),
        quote=_QUOTE,
    )
    return ReadOnlyQuery(sql=f"DESCRIBE {target}")


def build_table_stats_query(*, schema_name: str, table_name: str) -> ReadOnlyQuery:
    target = qualify(
        require_safe_identifier(schema_name, kind="库"),
        require_safe_identifier(table_name, kind="表"),
        quote=_QUOTE,
    )
    return ReadOnlyQuery(sql=f"DESCRIBE FORMATTED {target}")


def build_sample_query(*, schema_name: str, table_name: str) -> ReadOnlyQuery:
    target = qualify(
        require_safe_identifier(schema_name, kind="库"),
        require_safe_identifier(table_name, kind="表"),
        quote=_QUOTE,
    )
    return ReadOnlyQuery(sql=f"SELECT * FROM {target} LIMIT %s")


class HiveMetadataProbe:
    connector_type = ConnectorType.HIVE

    async def test_connection(
        self, *, target: ProbeTarget, credentials: ConnectionCredentials
    ) -> None:
        await self._fetch(
            target=target,
            credentials=credentials,
            query=build_connection_test_query(),
            args=(),
        )

    async def list_tables(
        self,
        *,
        target: ProbeTarget,
        credentials: ConnectionCredentials,
        schema_name: str | None,
        limit: int,
    ) -> Sequence[str]:
        schema = _resolve_schema(schema_name, target)
        rows = await self._fetch(
            target=target,
            credentials=credentials,
            query=build_table_list_query(schema_name=schema),
            args=(),
        )
        names = [str(next(iter(row.values()))) for row in rows if row]
        # SHOW TABLES 无法带 LIMIT，这里按平台上限截断，避免把超大库一次拉进控制面。
        return names[:limit]

    async def probe_table(
        self,
        *,
        target: ProbeTarget,
        credentials: ConnectionCredentials,
        schema_name: str,
        table_name: str,
        sample_limit: int,
    ) -> ProbeResult:
        schema = _resolve_schema(schema_name, target)
        require_safe_identifier(table_name, kind="表")
        column_rows = await self._fetch(
            target=target,
            credentials=credentials,
            query=build_column_query(schema_name=schema, table_name=table_name),
            args=(),
        )
        columns = _to_columns(column_rows)
        if not columns:
            raise DomainError(f"表不存在或不可见：{schema}.{table_name}")
        stats_rows = await self._fetch(
            target=target,
            credentials=credentials,
            query=build_table_stats_query(schema_name=schema, table_name=table_name),
            args=(),
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
            # Hive 无主键约束；显式留空并标注，避免被当成「有主键但没查到」。
            primary_keys=(),
            statistics={**_table_stats(stats_rows), "primary_key_supported": False},
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
        args: tuple[Any, ...],
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
        timeout = get_settings().probe_statement_timeout_seconds
        password = optional_secret(credentials.password)
        auth = target.options.get("auth") or ("LDAP" if password else "NOSASL")
        try:
            connection = hive.Connection(
                host=target.host or "localhost",
                port=target.port or DEFAULT_PORT,
                username=require_username(credentials, connector=self.connector_type.value),
                password=password,
                database=target.database_name,
                auth=auth,
            )
        except Exception as exc:  # pyhive/thrift 失败类型不稳定，统一收敛
            raise ExternalServiceUnavailable(
                describe_failure(exc, context=f"{self.connector_type.value} 连接")
            ) from exc
        try:
            cursor = connection.cursor()
            try:
                # thrift 的 socket 超时即为语句超时；Hive 无 SQL 级 timeout 语法。
                connection._transport.setTimeout(timeout * 1000)
            except (AttributeError, TypeError):
                # 传输层不暴露 setTimeout 时跳过，不因此让探查失败。
                pass
            cursor.execute(query.sql, args or None)
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


def _resolve_schema(schema_name: str | None, target: ProbeTarget) -> str:
    schema = schema_name or target.database_name
    if not schema:
        raise DomainError("Hive 探查需要指定库名")
    return require_safe_identifier(schema, kind="库")


def _to_columns(rows: Sequence[dict[str, Any]]) -> tuple[ProbedColumn, ...]:
    """解析 `DESCRIBE` 输出；遇到分区信息分隔行即停止。"""
    columns: list[ProbedColumn] = []
    for row in rows:
        name_value = row_get(row, "col_name")
        name = str(name_value).strip() if name_value is not None else ""
        if not name or name.startswith(_DESCRIBE_SECTION_MARKER):
            break
        statistics: dict[str, Any] = {}
        if row_get(row, "comment"):
            statistics["comment"] = str(row_get(row, "comment"))
        columns.append(
            ProbedColumn(
                name=name,
                data_type=str(row_get(row, "data_type") or "UNKNOWN").strip(),
                ordinal=len(columns) + 1,
                # Hive 不强制列非空，可空性无声明；留 None 而不是断言可空。
                nullable=None,
                statistics=statistics,
            )
        )
    return tuple(columns)


def _table_stats(rows: Sequence[dict[str, Any]]) -> dict[str, Any]:
    """从 `DESCRIBE FORMATTED` 提取已采集的行数与体积；未采集则留空。"""
    stats: dict[str, Any] = {}
    for row in rows:
        key = str(row_get(row, "col_name") or "").strip().casefold()
        value = row_get(row, "data_type")
        if value is None:
            continue
        text = str(value).strip()
        if key == "numrows" and text.isdigit():
            stats["approx_rows"] = int(text)
        elif key == "totalsize" and text.isdigit():
            stats["total_bytes"] = int(text)
    return stats
