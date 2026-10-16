"""连接登记、连通性测试与元数据探查的接口契约（T-10 / T-14 / T-16）。

包含一条**回归测试**：`create_profile` 一度在 `session.begin()` 之前先做授权查询，
而 `AsyncSession` 首次查询会自动开启事务，于是请求直接 500
（`A transaction is already begun on this Session`）。授权与前置校验必须在事务之内。
"""

import uuid
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from data_integration_platform.api.routes import connections as connections_routes
from data_integration_platform.domain.enums import (
    ConnectionStatus,
    ConnectorType,
    DataEndpointRole,
    ProjectRole,
)
from data_integration_platform.infrastructure.models import (
    DataConnection,
    Project,
    ProjectMembership,
    User,
)
from tests.conftest import TEST_BOOTSTRAP_TOKEN

MYSQL_PASSWORD = "s3cret-mysql-password"


async def bootstrap_admin(client: AsyncClient) -> tuple[str, str]:
    response = await client.post(
        "/api/v1/bootstrap",
        json={
            "admin_subject": "zhangsan",
            "admin_display_name": "张三",
            "project_name": "验证项目",
        },
        headers={"X-Bootstrap-Token": TEST_BOOTSTRAP_TOKEN},
    )
    assert response.status_code == 201, response.text
    return response.json()["user"]["id"], response.json()["project"]["id"]


def mysql_payload(project_id: str) -> dict[str, Any]:
    return {
        "project_id": project_id,
        "name": "mysql-source",
        "connector_type": ConnectorType.MYSQL.value,
        "endpoint_role": DataEndpointRole.SOURCE.value,
        "host": "127.0.0.1",
        "port": 13306,
        "database_name": "source_db",
        "options": {},
        "credentials": {"username": "dip_reader", "password": MYSQL_PASSWORD},
    }


@pytest.fixture
def dispatched(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, tuple[Any, ...]]]:
    """拦下 Celery 投递；单测不依赖 Redis 与 Worker。"""
    recorded: list[tuple[str, tuple[Any, ...]]] = []

    async def fake_dispatch(delay: Any, *args: Any) -> None:
        recorded.append((getattr(delay, "__qualname__", repr(delay)), args))

    monkeypatch.setattr(connections_routes, "dispatch", fake_dispatch)
    return recorded


# ------------------------------------------------------------------ 连接登记
async def test_create_connection_stores_reference_not_plaintext(
    client: AsyncClient, dispatched: list[Any]
) -> None:
    """FR-16：凭据只存密文与引用标识；响应与日志都不含明文。"""
    admin, project = await bootstrap_admin(client)
    response = await client.post(
        "/api/v1/connections", json=mysql_payload(project), headers={"X-Actor-Id": admin}
    )
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["secret_ref"].startswith("fernet://")
    assert MYSQL_PASSWORD not in response.text
    assert body["status"] == ConnectionStatus.UNTESTED.value
    assert "credentials" not in body
    assert "password" not in response.text


@pytest.mark.parametrize(
    ("connector_type", "options", "expected"),
    [
        (ConnectorType.S3_MINIO, {}, "bucket"),
        (ConnectorType.REST_API, {}, "url"),
        (ConnectorType.HDFS, {}, "base_path"),
    ],
)
async def test_create_connection_requires_connector_options(
    client: AsyncClient,
    dispatched: list[Any],
    connector_type: ConnectorType,
    options: dict[str, str],
    expected: str,
) -> None:
    """登记期就把必填项问清楚，不让错误拖到 Worker 里才暴露。"""
    admin, project = await bootstrap_admin(client)
    payload = mysql_payload(project)
    payload["connector_type"] = connector_type.value
    payload["options"] = options
    response = await client.post("/api/v1/connections", json=payload, headers={"X-Actor-Id": admin})
    assert response.status_code == 400
    assert expected in response.json()["detail"]


async def test_create_connection_rejects_duplicate_name(
    client: AsyncClient, dispatched: list[Any]
) -> None:
    admin, project = await bootstrap_admin(client)
    payload = mysql_payload(project)
    first = await client.post("/api/v1/connections", json=payload, headers={"X-Actor-Id": admin})
    second = await client.post("/api/v1/connections", json=payload, headers={"X-Actor-Id": admin})
    assert first.status_code == 201
    assert second.status_code == 409


async def test_cross_project_connection_creation_is_denied(
    client: AsyncClient, dispatched: list[Any]
) -> None:
    await bootstrap_admin(client)
    outsider = "99999999-9999-9999-9999-999999999999"
    response = await client.post(
        "/api/v1/connections",
        json=mysql_payload(str(uuid.uuid4())),
        headers={"X-Actor-Id": outsider},
    )
    assert response.status_code == 403


async def test_update_connection_resets_untested_status(
    client: AsyncClient, dispatched: list[Any]
) -> None:
    """配置一变，之前的连通性结论就不再成立。"""
    admin, project = await bootstrap_admin(client)
    created = await client.post(
        "/api/v1/connections", json=mysql_payload(project), headers={"X-Actor-Id": admin}
    )
    connection_id = created.json()["id"]

    updated = await client.put(
        f"/api/v1/connections/{connection_id}",
        json={"host": "10.0.0.9", "port": 3307},
        headers={"X-Actor-Id": admin},
    )
    assert updated.status_code == 200
    assert updated.json()["host"] == "10.0.0.9"
    assert updated.json()["status"] == ConnectionStatus.UNTESTED.value


# ------------------------------------------------------------ 连通性测试
async def test_test_connection_marks_testing_and_dispatches(
    client: AsyncClient, dispatched: list[Any]
) -> None:
    admin, project = await bootstrap_admin(client)
    created = await client.post(
        "/api/v1/connections", json=mysql_payload(project), headers={"X-Actor-Id": admin}
    )
    connection_id = created.json()["id"]

    response = await client.post(
        f"/api/v1/connections/{connection_id}/tests", headers={"X-Actor-Id": admin}
    )
    assert response.status_code == 200
    # API 不阻塞等待外部数据库，只落「测试中」事实并投递任务。
    assert response.json()["status"] == ConnectionStatus.TESTING.value
    assert len(dispatched) == 1


# ------------------------------------------------------------ 元数据探查
async def test_create_profile_accepts_ready_connection(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    dispatched: list[Any],
) -> None:
    """回归测试：授权查询若发生在 `session.begin()` 之前，请求会 500。"""
    admin, project = await bootstrap_admin(client)
    created = await client.post(
        "/api/v1/connections", json=mysql_payload(project), headers={"X-Actor-Id": admin}
    )
    connection_id = uuid.UUID(created.json()["id"])
    await _mark_ready(session_factory, connection_id)

    response = await client.post(
        f"/api/v1/connections/{connection_id}/profiles",
        json={
            "schema_name": "source_db",
            "table_name": "orders",
            "endpoint_role": DataEndpointRole.SOURCE.value,
            "sample_limit": 20,
        },
        headers={"X-Actor-Id": admin},
    )
    assert response.status_code == 202, response.text
    assert response.json()["status"] == "PENDING"
    assert len(dispatched) == 1


async def test_create_profile_rejects_untested_connection(
    client: AsyncClient, dispatched: list[Any]
) -> None:
    """连接未就绪时不允许探查：先测试，再探查。"""
    admin, project = await bootstrap_admin(client)
    created = await client.post(
        "/api/v1/connections", json=mysql_payload(project), headers={"X-Actor-Id": admin}
    )
    connection_id = created.json()["id"]

    response = await client.post(
        f"/api/v1/connections/{connection_id}/profiles",
        json={
            "schema_name": "source_db",
            "table_name": "orders",
            "endpoint_role": DataEndpointRole.SOURCE.value,
        },
        headers={"X-Actor-Id": admin},
    )
    assert response.status_code == 400
    assert "未就绪" in response.json()["detail"]
    assert dispatched == []


async def _mark_ready(
    session_factory: async_sessionmaker[AsyncSession], connection_id: uuid.UUID
) -> None:
    async with session_factory() as session:
        async with session.begin():
            connection = await session.get(DataConnection, connection_id)
            assert connection is not None
            connection.status = ConnectionStatus.READY


# ------------------------------------------------------------ 文件资产
async def test_upload_file_asset_rejects_oversized_file(
    client: AsyncClient, dispatched: list[Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """FR-09：超过单文件上限直接拒绝，不落盘。"""
    admin, project = await bootstrap_admin(client)
    monkeypatch.setenv("DIP_FILE_ASSET_MAX_BYTES", "1024")
    from data_integration_platform.config import get_settings

    get_settings.cache_clear()
    try:
        response = await client.post(
            "/api/v1/file-assets",
            data={"project_id": project, "endpoint_role": DataEndpointRole.SOURCE.value},
            files={"file": ("orders.csv", b"id\n" + b"1\n" * 2000, "text/csv")},
            headers={"X-Actor-Id": admin},
        )
    finally:
        get_settings.cache_clear()
    assert response.status_code == 400
    assert "上限" in response.json()["detail"]


async def test_upload_file_asset_rejects_unsupported_format(
    client: AsyncClient, dispatched: list[Any]
) -> None:
    admin, project = await bootstrap_admin(client)
    response = await client.post(
        "/api/v1/file-assets",
        data={"project_id": project, "endpoint_role": DataEndpointRole.SOURCE.value},
        files={"file": ("orders.docx", b"not really a docx", "application/octet-stream")},
        headers={"X-Actor-Id": admin},
    )
    assert response.status_code == 400
    assert "后缀" in response.json()["detail"]


async def test_upload_file_asset_records_row_count_and_checksum(
    client: AsyncClient, dispatched: list[Any]
) -> None:
    admin, project = await bootstrap_admin(client)
    payload = b"id,email\n1,a@b.com\n2,c@d.com\n"
    response = await client.post(
        "/api/v1/file-assets",
        data={"project_id": project, "endpoint_role": DataEndpointRole.SOURCE.value},
        files={"file": ("orders.csv", payload, "text/csv")},
        headers={"X-Actor-Id": admin},
    )
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["format"] == "CSV"
    assert body["row_count"] == 2
    assert body["size_bytes"] == len(payload)
    assert len(body["checksum_sha256"]) == 64


async def test_file_asset_member_sees_own_project_only(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    dispatched: list[Any],
) -> None:
    admin, project = await bootstrap_admin(client)
    uploaded = await client.post(
        "/api/v1/file-assets",
        data={"project_id": project, "endpoint_role": DataEndpointRole.SOURCE.value},
        files={"file": ("orders.csv", b"id\n1\n", "text/csv")},
        headers={"X-Actor-Id": admin},
    )
    asset_id = uploaded.json()["id"]

    outsider = await _create_outsider(session_factory)
    response = await client.get(f"/api/v1/file-assets/{asset_id}", headers={"X-Actor-Id": outsider})
    assert response.status_code == 403


async def _create_outsider(session_factory: async_sessionmaker[AsyncSession]) -> str:
    async with session_factory() as session:
        async with session.begin():
            project = Project(name=f"other-{uuid.uuid4().hex[:8]}")
            session.add(project)
            await session.flush()
            user = User(subject="outsider", display_name="外部人员")
            session.add(user)
            await session.flush()
            session.add(
                ProjectMembership(project_id=project.id, user_id=user.id, role=ProjectRole.VIEWER)
            )
            return str(user.id)
