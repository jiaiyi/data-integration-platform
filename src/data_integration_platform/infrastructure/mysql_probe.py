"""MySQL 协议元数据探查器（T-11）。

Doris 复用本实现：它以 MySQL 协议暴露 `information_schema`，基线只要求
「Schema / 字段类型 / 近似统计 / 脱敏样本」，本实现已满足该口径（GAP-05 判定无缺口）。

**没有一条查询是全表扫描**：元数据全部取自 `information_schema`，
行数取 InnoDB 统计信息里的近似值 `table_rows`，样本带绑定参数 `LIMIT`。
"""

from collections.abc import Sequence
from typing import Any

import pymysql
from pymysql.cursors import DictCursor

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

DEFAULT_PORT = 3306
_QUOTE = "`"


def build_connection_test_query() -> ReadOnlyQuery:
    """只读连通性测试：`SELECT 1` 不触碰任何业务表。"""
    return ReadOnlyQuery(sql="SELECT 1")


def build_table_list_query(*, limit: int) -> ReadOnlyQuery:
    return ReadOnlyQuery(
        sql=(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema = %s AND table_type IN ('BASE TABLE', 'VIEW') "
            "ORDER BY table_name LIMIT %s"
        ),
        params=(limit,),
    )


def build_column_query(*, schema_name: str, table_name: str) -> ReadOnlyQuery:
    """字段清单：`column_key = 'PRI'` 同时给出主键，避免为拿主键再查一次。"""
    return ReadOnlyQuery(
        sql=(
            "SELECT column_name, data_type, column_type, is_nullable, column_key, "
            "ordinal_position, column_comment FROM information_schema.columns "
            "WHERE table_schema = %s AND table_name = %s ORDER BY ordinal_position"
        ),
        params=(schema_name, table_name),
    )


def build_table_stats_query(*, schema_name: str, table_name: str) -> ReadOnlyQuery:
    """表级近似统计。

    `table_rows` 来自 InnoDB 统计信息，是**近似值**且可能为 NULL ——
    这正是 FR-10 的落点：要行数就读统计信息，绝不发 `SELECT COUNT(*)`。
    """
    return ReadOnlyQuery(
        sql=(
            "SELECT table_rows, data_length, index_length, table_comment, engine "
            "FROM information_schema.tables WHERE table_schema = %s AND table_name = %s"
        ),
        params=(schema_name, table_name),
    )


def build_sample_query(*, schema_name: str, table_name: str, limit: int) -> ReadOnlyQuery:
    """受限样本：标识符校验后加引号，行数走绑定变量。"""
    target = qualify(
        require_safe_identifier(schema_name, kind="模式"),
        require_safe_identifier(table_name, kind="表"),
        quote=_QUOTE,
    )
    return ReadOnlyQuery(sql=f"SELECT * FROM {target} LIMIT %s", params=(limit,))


def _single_table_stats(rows: Sequence[dict[str, Any]]) -> dict[str, Any]:
    """把表级统计收敛成稳定字段；`table_rows` 不可用时留空而不是填 0。"""
    if not rows:
        return {}
    row = rows[0]
    stats: dict[str, Any] = {}
    if row_get(row, "table_rows") is not None:
        stats["approx_rows"] = int(row_get(row, "table_rows"))
    for key in ("data_length", "index_length"):
        if row.get(key) is not None:
            stats[key] = int(row[key])
    for key in ("engine", "table_comment"):
        if row.get(key):
            stats[key] = str(row[key])
    return stats


class MySqlMetadataProbe:
    """MySQL 协议探查器；`connector_type` 由注册表注入以区分 MYSQL 与 DORIS。"""

    def __init__(self, connector_type: ConnectorType = ConnectorType.MYSQL) -> None:
        self.connector_type = connector_type

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
        schema = self._resolve_schema(target, schema_name)
        rows = await self._fetch(
            target=target,
            credentials=credentials,
            query=build_table_list_query(limit=limit),
            args=(schema, limit),
        )
        return [str(row_get(row, "table_name")) for row in rows]

    async def probe_table(
        self,
        *,
        target: ProbeTarget,
        credentials: ConnectionCredentials,
        schema_name: str,
        table_name: str,
        sample_limit: int,
    ) -> ProbeResult:
        schema = self._resolve_schema(target, schema_name)
        column_rows = await self._fetch(
            target=target,
            credentials=credentials,
            query=build_column_query(schema_name=schema, table_name=table_name),
            args=(schema, table_name),
        )
        if not column_rows:
            raise DomainError(f"表不存在或不可见：{schema}.{table_name}")
        columns, primary_keys = _to_columns(column_rows)
        stats_rows = await self._fetch(
            target=target,
            credentials=credentials,
            query=build_table_stats_query(schema_name=schema, table_name=table_name),
            args=(schema, table_name),
        )
        sample_rows = await self._fetch(
            target=target,
            credentials=credentials,
            query=build_sample_query(schema_name=schema, table_name=table_name, limit=sample_limit),
            args=(sample_limit,),
        )
        return ProbeResult(
            schema_name=schema,
            table_name=table_name,
            columns=columns,
            primary_keys=primary_keys,
            statistics=_single_table_stats(stats_rows),
            sample_rows=tuple(
                {key: cell_to_plain(value) for key, value in row.items()} for row in sample_rows
            ),
        )

    def _resolve_schema(self, target: ProbeTarget, schema_name: str | None) -> str:
        schema = schema_name or target.database_name
        if not schema:
            raise DomainError("MySQL 探查需要指定模式名或在连接上配置库名")
        return require_safe_identifier(schema, kind="模式")

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
        settings = get_settings()
        timeout = settings.probe_statement_timeout_seconds
        try:
            connection = pymysql.connect(
                host=target.host or "localhost",
                port=target.port or DEFAULT_PORT,
                user=require_username(credentials, connector=self.connector_type.value),
                password=require_password(credentials, connector=self.connector_type.value),
                database=target.database_name,
                connect_timeout=timeout,
                read_timeout=timeout,
                charset="utf8mb4",
                cursorclass=DictCursor,
            )
        except pymysql.MySQLError as exc:
            # 连不上/认证失败都归为外部依赖失败；失败原因已擦除凭据。
            raise ExternalServiceUnavailable(
                describe_failure(exc, context=f"{self.connector_type.value} 连接")
            ) from exc
        try:
            with connection.cursor() as cursor:
                cursor.execute(query.sql, args or None)
                if cursor.description is None:
                    return []
                return [dict(row) for row in cursor.fetchall()]
        except pymysql.MySQLError as exc:
            raise ExternalServiceUnavailable(
                describe_failure(exc, context=f"{self.connector_type.value} 探查")
            ) from exc
        finally:
            connection.close()


def _to_columns(
    rows: Sequence[dict[str, Any]],
) -> tuple[tuple[ProbedColumn, ...], tuple[str, ...]]:
    """把 `information_schema.columns` 结果映射为字段清单与主键。"""
    columns: list[ProbedColumn] = []
    primary_keys: list[str] = []
    for index, row in enumerate(rows, start=1):
        name = str(row_get(row, "column_name"))
        is_primary_key = str(row_get(row, "column_key") or "").upper() == "PRI"
        if is_primary_key:
            primary_keys.append(name)
        statistics: dict[str, Any] = {}
        if row_get(row, "column_comment"):
            statistics["comment"] = str(row_get(row, "column_comment"))
        columns.append(
            ProbedColumn(
                name=name,
                # column_type 比 data_type 信息更全（decimal(12,2) 比 decimal 有用）。
                data_type=str(
                    row_get(row, "column_type") or row_get(row, "data_type") or "UNKNOWN"
                ),
                ordinal=int(row_get(row, "ordinal_position") or index),
                nullable=str(row_get(row, "is_nullable") or "").upper() == "YES",
                is_primary_key=is_primary_key,
                statistics=statistics,
            )
        )
    return tuple(columns), tuple(primary_keys)
