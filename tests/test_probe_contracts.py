"""探查器契约测试（T-11 / T-12 / T-17）。

这组测试**不连数据库**，只断言「程序生成的 SQL 与出口策略」本身是否守规矩。
对拉不到镜像的数据源（Oracle / SQL Server / Hive / HDFS / ClickHouse），
这是能拿出的最强证据；对能起容器的那几个，它与真库集成测试互补。

三条硬约束：
1. 元数据查询不得出现 `COUNT(*)`（FR-10 禁止无界全表 COUNT）；
2. 样本查询必须有受限行数机制；
3. 标识符注入必须被拒绝，合法的标识符必须加引号。
"""

import pytest

from data_integration_platform.application.profiling import ReadOnlyQuery
from data_integration_platform.domain.enums import ConnectorType
from data_integration_platform.domain.errors import DomainError, PermissionDenied
from data_integration_platform.infrastructure import (
    clickhouse_probe,
    hive_probe,
    mysql_probe,
    oracle_probe,
    postgresql_probe,
    sqlserver_probe,
)
from data_integration_platform.infrastructure.egress import require_allowed_egress
from data_integration_platform.infrastructure.probe_registry import ProbeRegistry

INJECTION = "orders; DROP TABLE users --"
UNSAFE_NAMES = [INJECTION, "../../etc/passwd", "a`b", "a]b", "a b", ""]

# 全部字段级查询：逐个断言不含 COUNT()
_METADATA_QUERIES: list[tuple[str, ReadOnlyQuery]] = [
    ("mysql.table_list", mysql_probe.build_table_list_query(limit=50)),
    ("mysql.columns", mysql_probe.build_column_query(schema_name="ods", table_name="orders")),
    ("mysql.stats", mysql_probe.build_table_stats_query(schema_name="ods", table_name="orders")),
    ("pg.table_list", postgresql_probe.build_table_list_query()),
    ("pg.columns", postgresql_probe.build_column_query()),
    ("pg.stats", postgresql_probe.build_table_stats_query()),
    ("oracle.table_list", oracle_probe.build_table_list_query()),
    ("oracle.columns", oracle_probe.build_column_query()),
    ("oracle.stats", oracle_probe.build_table_stats_query()),
    ("sqlserver.table_list", sqlserver_probe.build_table_list_query()),
    ("sqlserver.columns", sqlserver_probe.build_column_query()),
    ("sqlserver.stats", sqlserver_probe.build_table_stats_query()),
    ("hive.columns", hive_probe.build_column_query(schema_name="ods", table_name="orders")),
    ("hive.stats", hive_probe.build_table_stats_query(schema_name="ods", table_name="orders")),
    ("clickhouse.columns", clickhouse_probe.build_column_query()),
    ("clickhouse.stats", clickhouse_probe.build_table_stats_query()),
]


@pytest.mark.parametrize(
    ("label", "query"), _METADATA_QUERIES, ids=[q[0] for q in _METADATA_QUERIES]
)
def test_metadata_queries_never_count(label: str, query: ReadOnlyQuery) -> None:
    """FR-10：近似统计只能取引擎维护的统计信息，绝不发无界 COUNT。"""
    assert "count(" not in query.sql.casefold(), f"{label} 出现了 COUNT"


@pytest.mark.parametrize(
    ("label", "sql"),
    [
        (
            "mysql",
            mysql_probe.build_sample_query(schema_name="ods", table_name="orders", limit=20).sql,
        ),
        ("pg", postgresql_probe.build_sample_query(schema_name="public", table_name="orders").sql),
        ("oracle", oracle_probe.build_sample_query(owner="ODS", table_name="ORDERS").sql),
        (
            "sqlserver",
            sqlserver_probe.build_sample_query(schema_name="dbo", table_name="orders").sql,
        ),
        ("hive", hive_probe.build_sample_query(schema_name="ods", table_name="orders").sql),
        (
            "clickhouse",
            clickhouse_probe.build_sample_query(database="ods", table_name="orders").sql,
        ),
    ],
)
def test_sample_queries_are_row_limited(label: str, sql: str) -> None:
    """样本必须受限：不同方言用各自的截断语法，但都必须有截断。"""
    folded = sql.casefold()
    assert any(marker in folded for marker in ("limit", "top (", "rownum")), (
        f"{label} 样本无行数限制"
    )


def test_row_limit_is_bound_not_interpolated() -> None:
    """MySQL 的样本行数走绑定变量，而不是拼进 SQL 文本。"""
    query = mysql_probe.build_sample_query(schema_name="ods", table_name="orders", limit=20)
    assert query.sql.endswith("LIMIT %s")
    assert query.params == (20,)


def test_identifiers_are_quoted_per_dialect() -> None:
    assert (
        "`ods`.`orders`"
        in mysql_probe.build_sample_query(schema_name="ods", table_name="orders", limit=1).sql
    )
    assert (
        '"public"."orders"'
        in postgresql_probe.build_sample_query(schema_name="public", table_name="orders").sql
    )
    assert (
        "[dbo].[orders]"
        in sqlserver_probe.build_sample_query(schema_name="dbo", table_name="orders").sql
    )
    assert (
        "`ods`.`orders`"
        in clickhouse_probe.build_sample_query(database="ods", table_name="orders").sql
    )


@pytest.mark.parametrize("unsafe", UNSAFE_NAMES)
@pytest.mark.parametrize(
    "builder",
    [
        lambda name: mysql_probe.build_sample_query(schema_name="ods", table_name=name, limit=1),
        lambda name: postgresql_probe.build_sample_query(schema_name="public", table_name=name),
        lambda name: oracle_probe.build_sample_query(owner="ODS", table_name=name),
        lambda name: sqlserver_probe.build_sample_query(schema_name="dbo", table_name=name),
        lambda name: hive_probe.build_sample_query(schema_name="ods", table_name=name),
        lambda name: clickhouse_probe.build_sample_query(database="ods", table_name=name),
    ],
)
def test_identifier_injection_is_rejected(builder: object, unsafe: str) -> None:
    """表名来自请求，会被拼进元数据查询：注入必须在这里被挡住，不能靠上游「应该校验过」。"""
    with pytest.raises(DomainError, match="不合法"):
        builder(unsafe)  # type: ignore[operator]


@pytest.mark.parametrize("unsafe", UNSAFE_NAMES)
def test_schema_injection_is_rejected(unsafe: str) -> None:
    with pytest.raises(DomainError, match="不合法"):
        mysql_probe.build_sample_query(schema_name=unsafe, table_name="orders", limit=1)
    with pytest.raises(DomainError, match="不合法"):
        postgresql_probe.build_sample_query(schema_name=unsafe, table_name="orders")


def test_hive_table_list_has_no_limit_clause() -> None:
    """`SHOW TABLES` 不支持 LIMIT：这一点必须被显式承认，由调用方按上限截断。"""
    query = hive_probe.build_table_list_query(schema_name="ods")
    assert "limit" not in query.sql.casefold()
    assert query.sql.startswith("SHOW TABLES IN")


# ------------------------------------------------------------------ 注册表
def test_registry_covers_every_connector_type() -> None:
    """PRD §8.4：基线清单与超集清单都纳入验收，任一类型不可用即视为未通过。"""
    registry = ProbeRegistry()
    assert registry.supported() == frozenset(ConnectorType)


def test_registry_never_silently_degrades() -> None:
    """未接入的连接器必须明确报错，不得静默降级或伪装成功（FR-07 验收口径）。"""
    registry = ProbeRegistry()
    for connector_type in ConnectorType:
        probe = registry.get(connector_type)
        assert probe.connector_type is connector_type


def test_doris_reuses_mysql_protocol_but_is_distinct_in_registry() -> None:
    registry = ProbeRegistry()
    mysql = registry.get(ConnectorType.MYSQL)
    doris = registry.get(ConnectorType.DORIS)
    assert type(mysql) is type(doris)
    assert mysql is not doris
    assert mysql.connector_type is ConnectorType.MYSQL
    assert doris.connector_type is ConnectorType.DORIS


# ------------------------------------------------------------- 出口白名单
def test_empty_allowlist_denies_everything() -> None:
    """失败关闭：白名单为空表示全部拒绝，而不是全部放行。"""
    with pytest.raises(PermissionDenied, match="白名单为空"):
        require_allowed_egress("https://api.example.com/orders", frozenset())


def test_allowed_host_passes() -> None:
    require_allowed_egress("https://api.example.com/orders", frozenset({"api.example.com"}))


def test_host_port_form_is_supported() -> None:
    require_allowed_egress("http://10.0.0.5:8080/orders", frozenset({"10.0.0.5:8080"}))


@pytest.mark.parametrize(
    "url",
    [
        "https://evil.example.net/orders",
        "https://api.example.com.evil.net/orders",
        "https://127.0.0.1/orders",
    ],
)
def test_non_allowlisted_host_is_denied(url: str) -> None:
    with pytest.raises(PermissionDenied, match="不在出口白名单"):
        require_allowed_egress(url, frozenset({"api.example.com"}))


@pytest.mark.parametrize("url", ["file:///etc/passwd", "ftp://api.example.com/x", "not-a-url"])
def test_non_http_scheme_is_rejected(url: str) -> None:
    with pytest.raises(DomainError):
        require_allowed_egress(url, frozenset({"api.example.com"}))
