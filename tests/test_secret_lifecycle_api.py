"""凭据全生命周期的状态机与放行条件（T-15 / FR-18 ~ FR-23）。

覆盖四类申请各自「哪些动作被允许、哪些被拒绝」：
- 审批前置状态（未验证不得审批、未激活不得销毁）；
- 申请人自批拦截（SOD-01）与职责槽校验（必须是安全审批人）；
- 销毁的「未被使用」证明；
- 到期告警的等级升级与冷却窗口。
"""

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from data_integration_platform.api.routes import (
    secret_destructions as destructions_routes,
)
from data_integration_platform.api.routes import (
    secret_migrations as migrations_routes,
)
from data_integration_platform.api.routes import (
    secret_rotation_rollbacks as rollbacks_routes,
)
from data_integration_platform.api.routes import (
    secret_rotations as rotations_routes,
)
from data_integration_platform.application.credential_alerts import CredentialAlertService
from data_integration_platform.application.probing import ProbeOutcome
from data_integration_platform.application.secret_destructions import SecretDestructionService
from data_integration_platform.application.secret_migrations import SecretMigrationService
from data_integration_platform.application.secret_rotation_rollbacks import (
    SecretRotationRollbackService,
)
from data_integration_platform.application.secret_rotations import SecretRotationService
from data_integration_platform.domain.enums import (
    ConnectionStatus,
    CredentialAlertLevel,
    CredentialAlertStatus,
    DataEndpointRole,
    SecretDestructionTarget,
    SecretMigrationStatus,
    SecretRotationStatus,
)
from data_integration_platform.infrastructure.models import (
    DataConnection,
    EncryptedSecret,
    SecretMigrationRequest,
    SecretRotationRequest,
)
from tests.conftest import TEST_BOOTSTRAP_TOKEN

PASSWORD_V1 = "first-password"
PASSWORD_V2 = "second-password"


class StubVerifier:
    """连通性验证的替身；只影响轮换验证阶段的结论，不碰真实数据源。"""

    def __init__(self, *, succeeds: bool) -> None:
        self.succeeds = succeeds
        self.calls = 0

    async def verify_credentials(self, *, connection: Any, credentials: Any) -> ProbeOutcome:
        self.calls += 1
        del connection, credentials
        return (
            ProbeOutcome.success()
            if self.succeeds
            else ProbeOutcome.failure("替身验证失败", retryable=False)
        )


@pytest.fixture
def dispatched(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """拦下 Celery 投递；任务体由测试直接调用服务执行。"""
    recorded: list[str] = []

    async def fake_dispatch(delay: Any, *args: Any) -> None:
        # `task.delay` 是 Celery 的绑定方法，任务名在绑定实例上而不是函数上。
        task = getattr(delay, "__self__", None)
        recorded.append(getattr(task, "name", repr(delay)))

    for module in (migrations_routes, rotations_routes, rollbacks_routes, destructions_routes):
        monkeypatch.setattr(module, "dispatch", fake_dispatch)
    return recorded


# ------------------------------------------------------------------ 夹具
async def setup_project(client: AsyncClient) -> tuple[str, str]:
    """bootstrap 得到管理员与项目；再建一个持有安全审批槽的第二人。"""
    boot = await client.post(
        "/api/v1/bootstrap",
        json={
            "admin_subject": "zhangsan",
            "admin_display_name": "张三",
            "project_name": "凭据治理",
        },
        headers={"X-Bootstrap-Token": TEST_BOOTSTRAP_TOKEN},
    )
    admin = boot.json()["user"]["id"]
    project = boot.json()["project"]["id"]
    approver = await client.put(
        f"/api/v1/projects/{project}/members",
        json={"subject": "lisi", "display_name": "李四", "role": "APPROVER"},
        headers={"X-Actor-Id": admin},
    )
    approver_id = approver.json()["user_id"]
    granted = await client.post(
        f"/api/v1/projects/{project}/role-grants",
        json={"user_id": approver_id, "role": "SECURITY_APPROVER"},
        headers={"X-Actor-Id": admin},
    )
    assert granted.status_code == 201, granted.text
    return admin, project


async def setup_connection(client: AsyncClient, admin: str, project: str) -> str:
    created = await client.post(
        "/api/v1/connections",
        json={
            "project_id": project,
            "name": "mysql-source",
            "connector_type": "MYSQL",
            "endpoint_role": DataEndpointRole.SOURCE.value,
            "host": "127.0.0.1",
            "port": 13306,
            "database_name": "source_db",
            "options": {},
            "credentials": {"username": "dip_reader", "password": PASSWORD_V1},
        },
        headers={"X-Actor-Id": admin},
    )
    assert created.status_code == 201, created.text
    return created.json()["id"]


async def actor_for_approver(client: AsyncClient, project: str, admin: str) -> str:
    members = await client.get(f"/api/v1/projects/{project}/members", headers={"X-Actor-Id": admin})
    return next(item["user_id"] for item in members.json() if item["user_id"] != admin)


async def connection_row(
    session_factory: async_sessionmaker[AsyncSession], connection_id: str
) -> DataConnection:
    async with session_factory() as session:
        row = await session.get(DataConnection, uuid.UUID(connection_id))
        assert row is not None
        return row


async def rotate_through_activation(
    session_factory: async_sessionmaker[AsyncSession],
    client: AsyncClient,
    *,
    connection_id: str,
    admin: str,
    approver: str,
    new_password: str = PASSWORD_V2,
    verifier_succeeds: bool = True,
) -> str:
    """跑完一次完整轮换并返回轮换申请 ID。"""
    created = await client.post(
        "/api/v1/secret-rotations",
        json={
            "connection_id": connection_id,
            "new_credentials": {"username": "dip_reader", "password": new_password},
            "reason": "定期轮换",
        },
        headers={"X-Actor-Id": admin},
    )
    assert created.status_code == 201, created.text
    request_id = uuid.UUID(created.json()["id"])

    async with session_factory() as session:
        async with session.begin():
            await SecretRotationService().validate(
                session,
                request_id=request_id,
                connectivity=StubVerifier(succeeds=verifier_succeeds),
            )
    decided = await client.post(
        f"/api/v1/secret-rotations/{request_id}/decisions",
        json={"decision": "APPROVE", "reason": "同意"},
        headers={"X-Actor-Id": approver},
    )
    assert decided.status_code == 200, decided.text
    async with session_factory() as session:
        async with session.begin():
            await SecretRotationService().activate(session, request_id=request_id)
    return str(request_id)


# ------------------------------------------------------------ 迁移（FR-18）
async def test_migration_requires_independent_security_approval(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    dispatched: list[str],
) -> None:
    admin, project = await setup_project(client)
    approver = await actor_for_approver(client, project, admin)
    connection_id = await setup_connection(client, admin, project)

    created = await client.post(
        "/api/v1/secret-migrations",
        json={"connection_id": connection_id, "target_provider": "vault-kv-v2", "reason": "换存储"},
        headers={"X-Actor-Id": admin},
    )
    assert created.status_code == 201, created.text
    body = created.json()
    assert body["status"] == SecretMigrationStatus.PENDING.value
    assert body["source_provider"] == "fernet-db"
    assert body["source_retained"] is True
    request_id = body["id"]

    # 申请人自批 → SOD-01
    self_approval = await client.post(
        f"/api/v1/secret-migrations/{request_id}/decisions",
        json={"decision": "APPROVE", "reason": "自己批"},
        headers={"X-Actor-Id": admin},
    )
    assert self_approval.status_code == 403
    assert "SOD-01" in self_approval.json()["detail"]

    # 成员但没有安全审批槽 → 403
    unauthorized = await client.post(
        f"/api/v1/secret-migrations/{request_id}/decisions",
        json={"decision": "APPROVE", "reason": "越权"},
        headers={"X-Actor-Id": str(uuid.uuid4())},
    )
    assert unauthorized.status_code == 403

    approved = await client.post(
        f"/api/v1/secret-migrations/{request_id}/decisions",
        json={"decision": "APPROVE", "reason": "同意迁移"},
        headers={"X-Actor-Id": approver},
    )
    assert approved.status_code == 200
    assert approved.json()["status"] == SecretMigrationStatus.APPROVED.value
    assert "execute_secret_migration" in dispatched[-1]

    # 源凭据引用已留档，回退才有依据
    async with session_factory() as session:
        row = await session.get(SecretMigrationRequest, uuid.UUID(request_id))
        assert row is not None
        assert row.source_secret_ref is not None
        assert row.source_secret_ref.startswith("fernet://")


async def test_migration_rejects_same_provider_and_missing_credentials(
    client: AsyncClient, dispatched: list[str]
) -> None:
    admin, project = await setup_project(client)
    connection_id = await setup_connection(client, admin, project)

    same = await client.post(
        "/api/v1/secret-migrations",
        json={"connection_id": connection_id, "target_provider": "fernet-db", "reason": "无意义"},
        headers={"X-Actor-Id": admin},
    )
    assert same.status_code == 400
    assert "一致" in same.json()["detail"]

    empty = await client.post(
        "/api/v1/connections",
        json={
            "project_id": project,
            "name": "no-secret",
            "connector_type": "MYSQL",
            "endpoint_role": DataEndpointRole.SOURCE.value,
            "host": "127.0.0.1",
            "database_name": "source_db",
        },
        headers={"X-Actor-Id": admin},
    )
    no_secret = await client.post(
        "/api/v1/secret-migrations",
        json={
            "connection_id": empty.json()["id"],
            "target_provider": "vault-kv-v2",
            "reason": "没有凭据",
        },
        headers={"X-Actor-Id": admin},
    )
    assert no_secret.status_code == 400
    assert "没有受管凭据" in no_secret.json()["detail"]


async def test_migration_execution_fails_closed_when_target_provider_not_ready(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    dispatched: list[str],
) -> None:
    """目标 Provider 未就绪时执行必须落 FAILED 并说明原因，而不是「成功但什么都没做」。"""
    admin, project = await setup_project(client)
    approver = await actor_for_approver(client, project, admin)
    connection_id = await setup_connection(client, admin, project)
    created = await client.post(
        "/api/v1/secret-migrations",
        json={"connection_id": connection_id, "target_provider": "vault-kv-v2", "reason": "换存储"},
        headers={"X-Actor-Id": admin},
    )
    request_id = uuid.UUID(created.json()["id"])
    await client.post(
        f"/api/v1/secret-migrations/{request_id}/decisions",
        json={"decision": "APPROVE", "reason": "同意"},
        headers={"X-Actor-Id": approver},
    )

    async with session_factory() as session:
        async with session.begin():
            outcome = await SecretMigrationService().execute(session, request_id=request_id)
            assert outcome.succeeded is False
            row = await session.get(SecretMigrationRequest, request_id)
            assert row is not None
            assert row.status is SecretMigrationStatus.FAILED
            assert row.failure_reason is not None
            assert "未就绪" in row.failure_reason


# ------------------------------------------------------------ 轮换（FR-19）
async def test_rotation_blocks_approval_before_validation(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    dispatched: list[str],
) -> None:
    """**未验证不得审批**：这是 FR-19 的核心前置条件。"""
    admin, project = await setup_project(client)
    approver = await actor_for_approver(client, project, admin)
    connection_id = await setup_connection(client, admin, project)
    created = await client.post(
        "/api/v1/secret-rotations",
        json={
            "connection_id": connection_id,
            "new_credentials": {"username": "dip_reader", "password": PASSWORD_V2},
            "reason": "轮换",
        },
        headers={"X-Actor-Id": admin},
    )
    assert created.status_code == 201, created.text
    assert created.json()["status"] == SecretRotationStatus.VALIDATING.value
    assert created.json()["staged_secret_ref"].startswith("fernet://")
    assert "validate_secret_rotation" in dispatched[-1]
    request_id = created.json()["id"]

    early = await client.post(
        f"/api/v1/secret-rotations/{request_id}/decisions",
        json={"decision": "APPROVE", "reason": "提前批"},
        headers={"X-Actor-Id": approver},
    )
    assert early.status_code == 400
    assert "WAITING_APPROVAL" in early.json()["detail"]


async def test_rotation_failed_validation_never_reaches_approval(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    dispatched: list[str],
) -> None:
    admin, project = await setup_project(client)
    approver = await actor_for_approver(client, project, admin)
    connection_id = await setup_connection(client, admin, project)
    created = await client.post(
        "/api/v1/secret-rotations",
        json={
            "connection_id": connection_id,
            "new_credentials": {"username": "dip_reader", "password": "bad"},
            "reason": "轮换",
        },
        headers={"X-Actor-Id": admin},
    )
    request_id = uuid.UUID(created.json()["id"])
    verifier = StubVerifier(succeeds=False)
    async with session_factory() as session:
        async with session.begin():
            outcome = await SecretRotationService().validate(
                session, request_id=request_id, connectivity=verifier
            )
    assert outcome.succeeded is False
    assert verifier.calls == 1

    async with session_factory() as session:
        row = await session.get(SecretRotationRequest, request_id)
        assert row is not None
        assert row.status is SecretRotationStatus.FAILED
        assert row.validated_at is None

    denied = await client.post(
        f"/api/v1/secret-rotations/{request_id}/decisions",
        json={"decision": "APPROVE", "reason": "硬批"},
        headers={"X-Actor-Id": approver},
    )
    assert denied.status_code == 400


async def test_rotation_activation_swaps_reference_and_opens_rollback_window(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    dispatched: list[str],
) -> None:
    admin, project = await setup_project(client)
    approver = await actor_for_approver(client, project, admin)
    connection_id = await setup_connection(client, admin, project)
    before = await connection_row(session_factory, connection_id)
    original_ref = before.secret_ref

    created = await client.post(
        "/api/v1/secret-rotations",
        json={
            "connection_id": connection_id,
            "new_credentials": {"username": "dip_reader", "password": PASSWORD_V2},
            "reason": "轮换",
        },
        headers={"X-Actor-Id": admin},
    )
    request_id = uuid.UUID(created.json()["id"])
    async with session_factory() as session:
        async with session.begin():
            await SecretRotationService().validate(
                session, request_id=request_id, connectivity=StubVerifier(succeeds=True)
            )
    async with session_factory() as session:
        row = await session.get(SecretRotationRequest, request_id)
        assert row is not None
        assert row.status is SecretRotationStatus.WAITING_APPROVAL
        assert row.validated_at is not None
        staged_ref = row.staged_secret_ref

    self_approval = await client.post(
        f"/api/v1/secret-rotations/{request_id}/decisions",
        json={"decision": "APPROVE", "reason": "自批"},
        headers={"X-Actor-Id": admin},
    )
    assert self_approval.status_code == 403
    assert "SOD-01" in self_approval.json()["detail"]

    approved = await client.post(
        f"/api/v1/secret-rotations/{request_id}/decisions",
        json={"decision": "APPROVE", "reason": "同意轮换"},
        headers={"X-Actor-Id": approver},
    )
    assert approved.status_code == 200
    assert "activate_secret_rotation" in dispatched[-1]

    async with session_factory() as session:
        async with session.begin():
            outcome = await SecretRotationService().activate(session, request_id=request_id)
    assert outcome.succeeded is True

    after = await connection_row(session_factory, connection_id)
    assert after.secret_ref == staged_ref
    assert after.secret_ref != original_ref
    assert after.credential_issued_at is not None

    async with session_factory() as session:
        row = await session.get(SecretRotationRequest, request_id)
        assert row is not None
        assert row.status is SecretRotationStatus.SUCCEEDED
        assert row.rollback_deadline_at is not None
        # 旧凭据引用与旧签发时间都留档，回退才有依据。
        assert row.previous_secret_ref == original_ref
        assert row.previous_credential_issued_at == before.credential_issued_at


# ------------------------------------------------------------ 回退（FR-20）
async def test_rollback_restores_previous_reference_and_issue_time(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    dispatched: list[str],
) -> None:
    admin, project = await setup_project(client)
    approver = await actor_for_approver(client, project, admin)
    connection_id = await setup_connection(client, admin, project)
    original_ref = (await connection_row(session_factory, connection_id)).secret_ref

    rotation_id = await rotate_through_activation(
        session_factory, client, connection_id=connection_id, admin=admin, approver=approver
    )

    created = await client.post(
        "/api/v1/secret-rotation-rollbacks",
        json={"rotation_request_id": rotation_id, "reason": "新凭据有问题"},
        headers={"X-Actor-Id": admin},
    )
    assert created.status_code == 201, created.text
    request_id = uuid.UUID(created.json()["id"])

    self_approval = await client.post(
        f"/api/v1/secret-rotation-rollbacks/{request_id}/decisions",
        json={"decision": "APPROVE", "reason": "自批"},
        headers={"X-Actor-Id": admin},
    )
    assert self_approval.status_code == 403

    approved = await client.post(
        f"/api/v1/secret-rotation-rollbacks/{request_id}/decisions",
        json={"decision": "APPROVE", "reason": "同意回退"},
        headers={"X-Actor-Id": approver},
    )
    assert approved.status_code == 200
    assert "execute_secret_rotation_rollback" in dispatched[-1]

    async with session_factory() as session:
        async with session.begin():
            outcome = await SecretRotationRollbackService().execute(session, request_id=request_id)
    assert outcome.succeeded is True

    restored = await connection_row(session_factory, connection_id)
    assert restored.secret_ref == original_ref

    async with session_factory() as session:
        row = await session.get(SecretRotationRequest, uuid.UUID(rotation_id))
        assert row is not None
        # 签发时间被精确还原，到期告警继续基于真实事实判断。
        assert restored.credential_issued_at == row.previous_credential_issued_at


async def test_rollback_rejected_before_activation(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    dispatched: list[str],
) -> None:
    admin, project = await setup_project(client)
    connection_id = await setup_connection(client, admin, project)
    created = await client.post(
        "/api/v1/secret-rotations",
        json={
            "connection_id": connection_id,
            "new_credentials": {"username": "u", "password": "p"},
            "reason": "轮换",
        },
        headers={"X-Actor-Id": admin},
    )
    rotation_id = created.json()["id"]
    response = await client.post(
        "/api/v1/secret-rotation-rollbacks",
        json={"rotation_request_id": rotation_id, "reason": "还没激活就退"},
        headers={"X-Actor-Id": admin},
    )
    assert response.status_code == 400
    assert "尚未激活" in response.json()["detail"]


async def test_rollback_rejected_after_window_closed(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    dispatched: list[str],
) -> None:
    admin, project = await setup_project(client)
    approver = await actor_for_approver(client, project, admin)
    connection_id = await setup_connection(client, admin, project)
    rotation_id = await rotate_through_activation(
        session_factory, client, connection_id=connection_id, admin=admin, approver=approver
    )

    async with session_factory() as session:
        async with session.begin():
            row = await session.get(SecretRotationRequest, uuid.UUID(rotation_id))
            assert row is not None
            row.rollback_deadline_at = datetime.now(UTC) - timedelta(seconds=1)
    response = await client.post(
        "/api/v1/secret-rotation-rollbacks",
        json={"rotation_request_id": rotation_id, "reason": "过期才退"},
        headers={"X-Actor-Id": admin},
    )
    assert response.status_code == 400
    assert "窗口已过" in response.json()["detail"]


# ------------------------------------------------------------ 销毁（FR-21）
async def test_destruction_rejected_before_rotation_activated(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    dispatched: list[str],
) -> None:
    admin, project = await setup_project(client)
    connection_id = await setup_connection(client, admin, project)
    created = await client.post(
        "/api/v1/secret-rotations",
        json={
            "connection_id": connection_id,
            "new_credentials": {"username": "u", "password": "p"},
            "reason": "轮换",
        },
        headers={"X-Actor-Id": admin},
    )
    response = await client.post(
        "/api/v1/secret-destructions",
        json={
            "connection_id": connection_id,
            "target": SecretDestructionTarget.ROTATION_SOURCE.value,
            "rotation_request_id": created.json()["id"],
            "reason": "提前销毁",
        },
        headers={"X-Actor-Id": admin},
    )
    assert response.status_code == 400
    assert "仍在使用" in response.json()["detail"]


async def test_destruction_of_rotation_source_after_activation(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    dispatched: list[str],
) -> None:
    """轮换成功后销毁旧凭据：这是唯一被允许销毁「源凭据」的时机。"""
    admin, project = await setup_project(client)
    approver = await actor_for_approver(client, project, admin)
    connection_id = await setup_connection(client, admin, project)
    original_ref = (await connection_row(session_factory, connection_id)).secret_ref
    rotation_id = await rotate_through_activation(
        session_factory, client, connection_id=connection_id, admin=admin, approver=approver
    )

    created = await client.post(
        "/api/v1/secret-destructions",
        json={
            "connection_id": connection_id,
            "target": SecretDestructionTarget.ROTATION_SOURCE.value,
            "rotation_request_id": rotation_id,
            "reason": "旧凭据已不再使用",
        },
        headers={"X-Actor-Id": admin},
    )
    assert created.status_code == 201, created.text
    request_id = uuid.UUID(created.json()["id"])

    approved = await client.post(
        f"/api/v1/secret-destructions/{request_id}/decisions",
        json={"decision": "APPROVE", "reason": "同意销毁"},
        headers={"X-Actor-Id": approver},
    )
    assert approved.status_code == 200
    assert "execute_secret_destruction" in dispatched[-1]

    async with session_factory() as session:
        async with session.begin():
            outcome = await SecretDestructionService().execute(session, request_id=request_id)
        assert outcome.succeeded is True
        # 旧凭据的密文行确实消失了。
        remaining = await session.scalar(
            select(EncryptedSecret.id).where(EncryptedSecret.secret_ref == original_ref)
        )
        assert remaining is None


async def test_destruction_refuses_credential_currently_in_use(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    dispatched: list[str],
) -> None:
    """回退之后，旧凭据重新变成「当前在用」，此时不允许销毁它。"""
    admin, project = await setup_project(client)
    approver = await actor_for_approver(client, project, admin)
    connection_id = await setup_connection(client, admin, project)
    rotation_id = await rotate_through_activation(
        session_factory, client, connection_id=connection_id, admin=admin, approver=approver
    )
    # 先回退，让 previous_secret_ref 重新变成在用凭据
    rollback = await client.post(
        "/api/v1/secret-rotation-rollbacks",
        json={"rotation_request_id": rotation_id, "reason": "回退"},
        headers={"X-Actor-Id": admin},
    )
    rollback_id = uuid.UUID(rollback.json()["id"])
    await client.post(
        f"/api/v1/secret-rotation-rollbacks/{rollback_id}/decisions",
        json={"decision": "APPROVE", "reason": "同意"},
        headers={"X-Actor-Id": approver},
    )
    async with session_factory() as session:
        async with session.begin():
            await SecretRotationRollbackService().execute(session, request_id=rollback_id)

    response = await client.post(
        "/api/v1/secret-destructions",
        json={
            "connection_id": connection_id,
            "target": SecretDestructionTarget.ROTATION_SOURCE.value,
            "rotation_request_id": rotation_id,
            "reason": "试图删除正在用的凭据",
        },
        headers={"X-Actor-Id": admin},
    )
    assert response.status_code == 400
    assert "当前使用的凭据" in response.json()["detail"]


async def test_destruction_of_rolled_back_staged_secret(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    dispatched: list[str],
) -> None:
    """已回退的暂存凭据是被允许销毁的另一类目标。"""
    admin, project = await setup_project(client)
    approver = await actor_for_approver(client, project, admin)
    connection_id = await setup_connection(client, admin, project)
    rotation_id = await rotate_through_activation(
        session_factory, client, connection_id=connection_id, admin=admin, approver=approver
    )
    async with session_factory() as session:
        row = await session.get(SecretRotationRequest, uuid.UUID(rotation_id))
        assert row is not None
        staged_ref = row.staged_secret_ref
    assert staged_ref is not None

    rollback = await client.post(
        "/api/v1/secret-rotation-rollbacks",
        json={"rotation_request_id": rotation_id, "reason": "回退"},
        headers={"X-Actor-Id": admin},
    )
    rollback_id = uuid.UUID(rollback.json()["id"])
    await client.post(
        f"/api/v1/secret-rotation-rollbacks/{rollback_id}/decisions",
        json={"decision": "APPROVE", "reason": "同意"},
        headers={"X-Actor-Id": approver},
    )
    async with session_factory() as session:
        async with session.begin():
            await SecretRotationRollbackService().execute(session, request_id=rollback_id)

    created = await client.post(
        "/api/v1/secret-destructions",
        json={
            "connection_id": connection_id,
            "target": SecretDestructionTarget.ROLLED_BACK_STAGED.value,
            "rotation_request_id": rotation_id,
            "reason": "清理已回退的暂存凭据",
        },
        headers={"X-Actor-Id": admin},
    )
    assert created.status_code == 201, created.text
    request_id = uuid.UUID(created.json()["id"])
    await client.post(
        f"/api/v1/secret-destructions/{request_id}/decisions",
        json={"decision": "APPROVE", "reason": "同意"},
        headers={"X-Actor-Id": approver},
    )
    async with session_factory() as session:
        async with session.begin():
            outcome = await SecretDestructionService().execute(session, request_id=request_id)
        assert outcome.succeeded is True
        remaining = await session.scalar(
            select(EncryptedSecret.id).where(EncryptedSecret.secret_ref == staged_ref)
        )
        assert remaining is None


async def test_destruction_requires_successful_rollback_for_staged_target(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    dispatched: list[str],
) -> None:
    admin, project = await setup_project(client)
    approver = await actor_for_approver(client, project, admin)
    connection_id = await setup_connection(client, admin, project)
    rotation_id = await rotate_through_activation(
        session_factory, client, connection_id=connection_id, admin=admin, approver=approver
    )
    response = await client.post(
        "/api/v1/secret-destructions",
        json={
            "connection_id": connection_id,
            "target": SecretDestructionTarget.ROLLED_BACK_STAGED.value,
            "rotation_request_id": rotation_id,
            "reason": "没有回退过就销毁暂存",
        },
        headers={"X-Actor-Id": admin},
    )
    assert response.status_code == 400
    assert "没有成功的回退记录" in response.json()["detail"]


# ------------------------------------------------------------ 到期告警（FR-22）
async def test_rotation_policy_validation(client: AsyncClient, dispatched: list[str]) -> None:
    admin, project = await setup_project(client)
    connection_id = await setup_connection(client, admin, project)

    bad = await client.put(
        f"/api/v1/connections/{connection_id}/credential-rotation-policy",
        json={"enabled": True, "max_age_days": 30, "warning_days": 30},
        headers={"X-Actor-Id": admin},
    )
    assert bad.status_code == 400
    assert "必须小于最大年龄" in bad.json()["detail"]

    missing_age = await client.put(
        f"/api/v1/connections/{connection_id}/credential-rotation-policy",
        json={"enabled": True},
        headers={"X-Actor-Id": admin},
    )
    assert missing_age.status_code == 400
    assert "最大年龄" in missing_age.json()["detail"]

    ok = await client.put(
        f"/api/v1/connections/{connection_id}/credential-rotation-policy",
        json={"enabled": True, "max_age_days": 30, "warning_days": 7},
        headers={"X-Actor-Id": admin},
    )
    assert ok.status_code == 200
    assert ok.json()["status"] == ConnectionStatus.UNTESTED.value


async def test_alert_scan_opens_escalates_and_cools_down(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    dispatched: list[str],
) -> None:
    admin, project = await setup_project(client)
    connection_id = await setup_connection(client, admin, project)
    await client.put(
        f"/api/v1/connections/{connection_id}/credential-rotation-policy",
        json={"enabled": True, "max_age_days": 30, "warning_days": 7},
        headers={"X-Actor-Id": admin},
    )

    # 进入预警窗口：签发 25 天前，30 天到期，预警 7 天
    await _set_issued_at(session_factory, connection_id, days_ago=25)
    async with session_factory() as session:
        async with session.begin():
            alerts = await CredentialAlertService().scan(session)
    assert len(alerts) == 1
    assert alerts[0].level is CredentialAlertLevel.WARNING

    # 冷却期内重复扫描不产生新告警
    async with session_factory() as session:
        async with session.begin():
            again = await CredentialAlertService().scan(session)
    assert again == []
    async with session_factory() as session:
        row = await session.get(DataConnection, uuid.UUID(connection_id))
        assert row is not None
    listed = await client.get(
        f"/api/v1/projects/{project}/credential-alerts",
        params={"status": CredentialAlertStatus.OPEN.value},
        headers={"X-Actor-Id": admin},
    )
    assert len(listed.json()) == 1
    assert listed.json()[0]["level"] == CredentialAlertLevel.WARNING.value
    assert listed.json()[0]["delivery_required"] is False  # 未配置 Webhook

    # 逾期：等级升级，旧告警关闭、新告警开启
    await _set_issued_at(session_factory, connection_id, days_ago=40)
    async with session_factory() as session:
        async with session.begin():
            escalated = await CredentialAlertService().scan(session)
    assert len(escalated) == 1
    assert escalated[0].level is CredentialAlertLevel.OVERDUE

    all_alerts = await client.get(
        f"/api/v1/projects/{project}/credential-alerts", headers={"X-Actor-Id": admin}
    )
    by_level = {item["level"]: item["status"] for item in all_alerts.json()}
    assert by_level[CredentialAlertLevel.WARNING.value] == CredentialAlertStatus.RESOLVED.value
    assert by_level[CredentialAlertLevel.OVERDUE.value] == CredentialAlertStatus.OPEN.value


async def test_rotation_activation_resolves_open_alerts(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    dispatched: list[str],
) -> None:
    """凭据换新后未解决的告警必须关闭，否则会一直重复提醒已解决的事。"""
    admin, project = await setup_project(client)
    approver = await actor_for_approver(client, project, admin)
    connection_id = await setup_connection(client, admin, project)
    await client.put(
        f"/api/v1/connections/{connection_id}/credential-rotation-policy",
        json={"enabled": True, "max_age_days": 30, "warning_days": 7},
        headers={"X-Actor-Id": admin},
    )
    await _set_issued_at(session_factory, connection_id, days_ago=40)
    async with session_factory() as session:
        async with session.begin():
            assert len(await CredentialAlertService().scan(session)) == 1

    await rotate_through_activation(
        session_factory, client, connection_id=connection_id, admin=admin, approver=approver
    )

    remaining = await client.get(
        f"/api/v1/projects/{project}/credential-alerts",
        params={"status": CredentialAlertStatus.OPEN.value},
        headers={"X-Actor-Id": admin},
    )
    assert remaining.json() == []


async def _set_issued_at(
    session_factory: async_sessionmaker[AsyncSession], connection_id: str, *, days_ago: int
) -> None:
    async with session_factory() as session:
        async with session.begin():
            row = await session.get(DataConnection, uuid.UUID(connection_id))
            assert row is not None
            row.credential_issued_at = datetime.now(UTC) - timedelta(days=days_ago)
