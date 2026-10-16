"""探查器对真实数据库/对象存储的集成验证（T-11 / T-12 / T-16）。

覆盖 PRD §9.2 的验收口径：
- MySQL 只读连通性测试成功，且失败信息与响应里都不含密码明文；
- 探查只执行受限期查询，不执行无界全表 COUNT；
- Profile 样本以掩码形式落库，**原始值不出现在持久化结果里**；
- S3/MinIO 能枚举对象、读元数据、前缀范围采样解析出结构；
- 超过前缀采样上限的尾部结构格式**明确报错**，不静默降级。

对拉不到镜像的数据源（Oracle / SQL Server / Hive / HDFS / ClickHouse），
真库验证做不了，由 `tests/test_probe_contracts.py` 的 SQL 契约测试覆盖。
"""

import io
import socket
import uuid
from collections.abc import AsyncIterator
from typing import Any

import pytest
from pydantic import SecretStr
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from data_integration_platform.application.probing import ProbingService
from data_integration_platform.application.profiling import ProbeTarget, ProfileService
from data_integration_platform.application.secrets import ConnectionCredentials
from data_integration_platform.config import get_settings
from data_integration_platform.domain.enums import (
    ConnectionStatus,
    ConnectorType,
    DataEndpointRole,
    DataProfileStatus,
)
from data_integration_platform.domain.errors import DomainError
from data_integration_platform.infrastructure.models import (
    DataConnection,
    DataProfile,
    Project,
)
from data_integration_platform.infrastructure.probe_registry import ProbeRegistry
from data_integration_platform.infrastructure.secret_factory import FernetDbSecretProvider

# 用 127.0.0.1 而不是 localhost：Windows 上 localhost 会优先解析到 IPv6 `::1`，
# 而 Docker 只发布 IPv4，asyncpg 会一直挂到超时（实测踩过）。
PG = {"host": "127.0.0.1", "port": 5432, "database": "dip"}
CONTROL_DB = f"postgresql+asyncpg://dip:dip@{PG['host']}:{PG['port']}/dip"
MYSQL = {"host": "127.0.0.1", "port": 13306, "database": "source_db"}
MINIO = {"host": "127.0.0.1", "port": 9000, "bucket": "dip-integration"}
MINIO_ACCESS_KEY = "dip_minio"
MINIO_SECRET_KEY = "dip_minio_e2e"

RAW_EMAIL = "zhangsan@example.com"
RAW_PHONE = "13800001234"


def _reachable(host: str, port: int) -> bool:
    try:
        with socket.create_connection((host, port), timeout=1.5):
            return True
    except OSError:
        return False


requires_control_db = pytest.mark.skipif(
    not _reachable("localhost", 5432), reason="控制面 PostgreSQL 未启动"
)
requires_mysql = pytest.mark.skipif(
    not _reachable("localhost", 13306), reason="MySQL e2e 未启动（--profile e2e）"
)
requires_minio = pytest.mark.skipif(
    not _reachable("localhost", 9000), reason="MinIO 未启动（--profile e2e）"
)

pytestmark = pytest.mark.integration


@pytest.fixture
async def control_session() -> AsyncIterator[AsyncSession]:
    """控制面库会话。

    测试自身**不回滚也别提交**：结束时整体丢弃事务，dev 库里不留测试残留。
    """
    engine = create_async_engine(CONTROL_DB, poolclass=NullPool)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        yield session
        await session.rollback()
    await engine.dispose()


def mysql_credentials() -> ConnectionCredentials:
    return ConnectionCredentials(username="dip_reader", password=SecretStr("dip_reader_e2e"))


def mysql_target() -> ProbeTarget:
    return ProbeTarget(
        connector_type=ConnectorType.MYSQL,
        host=MYSQL["host"],
        port=MYSQL["port"],
        database_name=MYSQL["database"],
    )


def registry() -> ProbeRegistry:
    return ProbeRegistry()


# ================================================================ MySQL 真库
@requires_mysql
async def test_mysql_connection_test_succeeds() -> None:
    await (
        registry()
        .get(ConnectorType.MYSQL)
        .test_connection(target=mysql_target(), credentials=mysql_credentials())
    )


@requires_mysql
async def test_mysql_bad_credentials_fail_without_leaking_password() -> None:
    """FR-08：凭据错误时失败原因只说明原因，**不含密码明文**。"""
    wrong = ConnectionCredentials(username="dip_reader", password=SecretStr("wrong-password-xyz"))
    with pytest.raises(DomainError) as error:
        await (
            registry()
            .get(ConnectorType.MYSQL)
            .test_connection(target=mysql_target(), credentials=wrong)
        )
    assert "wrong-password-xyz" not in str(error.value)


@requires_mysql
async def test_mysql_list_tables() -> None:
    tables = (
        await registry()
        .get(ConnectorType.MYSQL)
        .list_tables(
            target=mysql_target(),
            credentials=mysql_credentials(),
            schema_name="source_db",
            limit=50,
        )
    )
    assert {"orders", "users"} <= set(tables)


@requires_mysql
async def test_mysql_probe_table_returns_schema_keys_and_stats() -> None:
    result = (
        await registry()
        .get(ConnectorType.MYSQL)
        .probe_table(
            target=mysql_target(),
            credentials=mysql_credentials(),
            schema_name="source_db",
            table_name="orders",
            sample_limit=5,
        )
    )
    assert result.primary_keys == ("id",)
    types = {column.name: column.data_type for column in result.columns}
    assert types["amount"].startswith("decimal")
    assert types["email"].startswith("varchar")
    assert types["updated_at"].startswith("datetime")
    # 近似统计来自 information_schema，不是 COUNT(*)。
    assert "approx_rows" in result.statistics
    assert len(result.sample_rows) <= 5


@requires_mysql
async def test_mysql_probe_rejects_unknown_table() -> None:
    with pytest.raises(DomainError, match="不存在或不可见"):
        await (
            registry()
            .get(ConnectorType.MYSQL)
            .probe_table(
                target=mysql_target(),
                credentials=mysql_credentials(),
                schema_name="source_db",
                table_name="no_such_table",
                sample_limit=5,
            )
        )


# ================================================ 端到端：真库探查 + 脱敏落库
@requires_mysql
@requires_control_db
async def test_profile_persists_only_masked_values(control_session: AsyncSession) -> None:
    """FR-10 / FR-12 / FR-13 的组合证据。

    走**完整链路**：控制面建连接 → 建 Profile → 真实 MySQL 探查 → 脱敏落库，
    然后断言原始邮箱/手机号**没有出现在持久化结果里**，而掩码形态在里面。
    """
    project = Project(name=f"it-{uuid.uuid4().hex[:12]}")
    control_session.add(project)
    await control_session.flush()

    secrets = FernetDbSecretProvider(
        control_session,
        # 集成测试自备主密钥：不依赖开发者的 .env 是否配了密钥。
        SecretStr(_fernet_key()),
    )
    stored = await secrets.store(
        project_id=project.id, name="mysql-orders", credentials=mysql_credentials()
    )
    connection = DataConnection(
        project_id=project.id,
        name="mysql-orders",
        connector_type=ConnectorType.MYSQL,
        endpoint_role=DataEndpointRole.SOURCE,
        status=ConnectionStatus.READY,
        host=MYSQL["host"],
        port=MYSQL["port"],
        database_name=MYSQL["database"],
        options={},
        secret_ref=stored.secret_ref,
        created_by=uuid.uuid4(),
    )
    control_session.add(connection)
    await control_session.flush()

    profiles = ProfileService()
    profile = await profiles.request(
        control_session,
        connection_id=connection.id,
        actor_id=connection.created_by,
        schema_name="source_db",
        table_name="orders",
        endpoint_role=DataEndpointRole.SOURCE,
    )
    focus = profile.id

    outcome = await ProbingService(registry=registry(), secret_provider=secrets).run_profile(
        control_session, profile_id=focus, sample_limit=20
    )
    assert outcome.succeeded, outcome.reason

    reloaded = await control_session.get(DataProfile, focus)
    assert reloaded is not None
    assert reloaded.status is DataProfileStatus.READY
    assert reloaded.expires_at is not None
    assert reloaded.sample_size > 0

    # 原始值不得出现在持久化结果中（G-07）。
    persisted = repr(reloaded.masked_sample) + repr(reloaded.columns)
    assert RAW_EMAIL not in persisted
    assert RAW_PHONE not in persisted
    # 但掩码形态要在，样本才具备诊断价值。
    assert any(
        str(row.get("email", "")).startswith("z***@ex***.com") for row in reloaded.masked_sample
    )

    # 超出 TTL 的 Profile 不可用于生成（FR-13）。
    from datetime import UTC, datetime, timedelta

    from data_integration_platform.application.profiling import require_profile_usable

    require_profile_usable(reloaded)
    reloaded.expires_at = datetime.now(UTC) - timedelta(seconds=1)
    with pytest.raises(DomainError, match="已过期"):
        require_profile_usable(reloaded)


def _fernet_key() -> str:
    from cryptography.fernet import Fernet

    return Fernet.generate_key().decode()


# ============================================================= PostgreSQL 真库
@requires_control_db
async def test_postgresql_probe_reads_own_metadata() -> None:
    """用控制面自己的库验证 PostgreSQL 探查器：表结构、主键、近似行数。"""
    target = ProbeTarget(
        connector_type=ConnectorType.POSTGRESQL,
        host=PG["host"],
        port=PG["port"],
        database_name=PG["database"],
    )
    credentials = ConnectionCredentials(username="dip", password=SecretStr("dip"))
    await (
        registry()
        .get(ConnectorType.POSTGRESQL)
        .test_connection(target=target, credentials=credentials)
    )
    result = (
        await registry()
        .get(ConnectorType.POSTGRESQL)
        .probe_table(
            target=target,
            credentials=credentials,
            schema_name="public",
            table_name="users",
            sample_limit=5,
        )
    )
    assert result.primary_keys == ("id",)
    names = {column.name for column in result.columns}
    assert {"id", "subject", "display_name", "status", "created_at"} <= names
    nullable = {column.name: column.nullable for column in result.columns}
    assert nullable["subject"] is False


# ================================================================ MinIO 真库
def _minio_client() -> Any:
    from minio import Minio

    return Minio(
        endpoint=f"{MINIO['host']}:{MINIO['port']}",
        access_key=MINIO_ACCESS_KEY,
        secret_key=MINIO_SECRET_KEY,
        secure=False,
    )


def _ensure_bucket() -> None:
    client = _minio_client()
    if not client.bucket_exists(MINIO["bucket"]):
        client.make_bucket(MINIO["bucket"])


def _put_object(name: str, payload: bytes) -> None:
    _minio_client().put_object(MINIO["bucket"], name, io.BytesIO(payload), length=len(payload))


@pytest.fixture
def small_remote_cap(monkeypatch: pytest.MonkeyPatch) -> int:
    """把远端采样上限压到 64 KiB。

    这样「读前缀」与「超限拒绝」两条路径都能用很小的对象触发，
    不必造一个真的超过 8 MiB 的对象；测的是同一段策略代码。
    """
    cap = 64 * 1024
    monkeypatch.setenv("DIP_REMOTE_FILE_SAMPLE_BYTES", str(cap))
    get_settings.cache_clear()
    yield cap
    get_settings.cache_clear()


def minio_credentials() -> ConnectionCredentials:
    return ConnectionCredentials(
        access_key=MINIO_ACCESS_KEY, secret_key=SecretStr(MINIO_SECRET_KEY)
    )


def minio_target() -> ProbeTarget:
    return ProbeTarget(
        connector_type=ConnectorType.S3_MINIO,
        host=MINIO["host"],
        port=MINIO["port"],
        options={"bucket": MINIO["bucket"], "secure": "false"},
    )


CSV_PAYLOAD = (
    "id,email,amount\n" + "".join(f"{i},user{i}@example.com,{i}.50\n" for i in range(1, 21))
).encode()


@requires_minio
async def test_s3_lists_objects_under_prefix() -> None:
    _ensure_bucket()
    _put_object("orders/orders.csv", CSV_PAYLOAD)
    probe = registry().get(ConnectorType.S3_MINIO)
    names = await probe.list_tables(
        target=minio_target(), credentials=minio_credentials(), schema_name=None, limit=50
    )
    assert "orders/orders.csv" in names


@requires_minio
async def test_s3_probes_text_object_with_bounded_prefix_read(small_remote_cap: int) -> None:
    """对象大于采样上限时只读前缀：结构能推断，但**行数留空**而不是拿部分字节冒充。"""
    _ensure_bucket()
    big = ("id,email\n" + "".join(f"{i},user{i}@example.com\n" for i in range(1, 20_001))).encode()
    assert len(big) > small_remote_cap
    _put_object("big/orders_big.csv", big)
    probe = registry().get(ConnectorType.S3_MINIO)
    result = await probe.probe_table(
        target=minio_target(),
        credentials=minio_credentials(),
        schema_name=MINIO["bucket"],
        table_name="big/orders_big.csv",
        sample_limit=3,
    )
    assert [column.name for column in result.columns] == ["id", "email"]
    assert result.statistics["object_bytes"] == len(big)
    assert result.statistics["partial_read"] is True
    assert result.statistics["sampled_bytes"] <= small_remote_cap
    # 部分读取不给出真实行数 —— 留空比给一个错的数字负责。
    assert result.row_count is None
    assert len(result.sample_rows) == 3


@requires_minio
async def test_s3_reads_small_object_fully_and_reports_rows() -> None:
    """对象小于采样上限时整份读取，行数才是真实值。"""
    _ensure_bucket()
    _put_object("small/orders_small.csv", CSV_PAYLOAD)
    probe = registry().get(ConnectorType.S3_MINIO)
    result = await probe.probe_table(
        target=minio_target(),
        credentials=minio_credentials(),
        schema_name=MINIO["bucket"],
        table_name="small/orders_small.csv",
        sample_limit=3,
    )
    assert "partial_read" not in result.statistics
    assert result.statistics["object_bytes"] == len(CSV_PAYLOAD)
    assert result.row_count == 20
    assert len(result.sample_rows) == 3


@requires_minio
async def test_s3_rejects_footer_based_format_above_cap(small_remote_cap: int) -> None:
    """尾部结构格式超过上限时明确报错，不静默降级（正确性问题，不是性能优化）。"""
    import pyarrow as pa
    import pyarrow.parquet as pa_parquet

    _ensure_bucket()
    table = pa.table({"id": pa.array(range(20_000), type=pa.int64())})
    buffer = io.BytesIO()
    pa_parquet.write_table(table, buffer, compression="NONE")
    payload = buffer.getvalue()
    assert len(payload) > small_remote_cap, "样本需要比上限更大"
    _put_object("big/orders.parquet", payload)

    probe = registry().get(ConnectorType.S3_MINIO)
    with pytest.raises(DomainError, match="结构信息位于文件尾部"):
        await probe.probe_table(
            target=minio_target(),
            credentials=minio_credentials(),
            schema_name=MINIO["bucket"],
            table_name="big/orders.parquet",
            sample_limit=3,
        )


@requires_minio
async def test_s3_probes_small_parquet_from_metadata() -> None:
    """小对象可整份读取：Parquet 的表结构来自元数据，行数来自 footer。"""
    import pyarrow as pa
    import pyarrow.parquet as pa_parquet

    _ensure_bucket()
    table = pa.table(
        {
            "id": pa.array(range(50), type=pa.int64()),
            "email": pa.array([f"u{i}@x.com" for i in range(50)]),
        }
    )
    buffer = io.BytesIO()
    pa_parquet.write_table(table, buffer)
    _put_object("small/orders.parquet", buffer.getvalue())

    probe = registry().get(ConnectorType.S3_MINIO)
    result = await probe.probe_table(
        target=minio_target(),
        credentials=minio_credentials(),
        schema_name=MINIO["bucket"],
        table_name="small/orders.parquet",
        sample_limit=3,
    )
    assert [column.name for column in result.columns] == ["id", "email"]
    assert result.statistics["object_bytes"] == len(buffer.getvalue())
    assert len(result.sample_rows) == 3


@requires_minio
async def test_s3_rejects_unknown_object() -> None:
    _ensure_bucket()
    probe = registry().get(ConnectorType.S3_MINIO)
    with pytest.raises(DomainError, match="不存在"):
        await probe.probe_table(
            target=minio_target(),
            credentials=minio_credentials(),
            schema_name=MINIO["bucket"],
            table_name="nope/missing.csv",
            sample_limit=3,
        )


@requires_minio
async def test_s3_rejects_bad_credentials_without_leaking_secret() -> None:
    _ensure_bucket()
    probe = registry().get(ConnectorType.S3_MINIO)
    with pytest.raises(DomainError) as error:
        await probe.list_tables(
            target=minio_target(),
            credentials=ConnectionCredentials(
                access_key="dip_minio", secret_key=SecretStr("not-the-secret-value")
            ),
            schema_name=None,
            limit=10,
        )
    assert "not-the-secret-value" not in str(error.value)
