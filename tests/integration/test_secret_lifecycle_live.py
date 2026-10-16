"""凭据全生命周期的真库 / 真 HTTP 集成验证（T-15）。

三件事必须真跑才能证明，桩测证明不了：

1. **跨 Provider 迁移**：fernet-db → Vault KV v2。这里起一个实现 KV v2 语义的本地 HTTP 服务
   （Docker Hub 不可达，拉不到 `hashicorp/vault` 镜像），迁移后凭据必须能从「Vault」读回来，
   且**源凭据仍然存在**（FR-18 不销毁源凭据）。
2. **轮换验证连的是真库**：用真实 MySQL 连接测试暂存凭据，错误口令必须验证失败并停在 `FAILED`。
3. **Webhook 投递幂等**：起一个本地接收端记录 `delivery_id`，重复投递只能产生一次外发。
"""

import json
import socket
import threading
import uuid
from collections.abc import AsyncIterator, Iterator
from contextlib import ExitStack, contextmanager
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, ClassVar
from urllib.parse import unquote, urlsplit

import pytest
from pydantic import SecretStr
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from data_integration_platform.application.credential_alerts import CredentialAlertService
from data_integration_platform.application.probing import ProbingService
from data_integration_platform.application.secret_migrations import SecretMigrationService
from data_integration_platform.application.secret_rotations import SecretRotationService
from data_integration_platform.application.secrets import ConnectionCredentials
from data_integration_platform.config import get_settings
from data_integration_platform.domain.enums import (
    ConnectionStatus,
    ConnectorType,
    CredentialAlertDeliveryStatus,
    CredentialAlertStatus,
    DataEndpointRole,
    ProjectRole,
    SecretRotationStatus,
)
from data_integration_platform.domain.harness import HarnessApprovalValue, HarnessRole
from data_integration_platform.infrastructure.models import (
    CredentialAlertDelivery,
    CredentialExpiryAlert,
    DataConnection,
    EncryptedSecret,
    Project,
    ProjectMembership,
    ProjectRoleGrant,
    SecretMigrationRequest,
    SecretRotationRequest,
    User,
)
from data_integration_platform.infrastructure.probe_registry import ProbeRegistry
from data_integration_platform.infrastructure.secret_factory import (
    FernetDbSecretProvider,
    create_secret_provider,
    secret_provider_for_ref,
)

CONTROL_DB = "postgresql+asyncpg://dip:dip@127.0.0.1:5432/dip"
MYSQL_PASSWORD = "dip_reader_e2e"
VAULT_MOUNT = "secret"
VAULT_TOKEN = "integration-vault-token"


def _reachable(host: str, port: int) -> bool:
    try:
        with socket.create_connection((host, port), timeout=1.5):
            return True
    except OSError:
        return False


requires_control_db = pytest.mark.skipif(
    not _reachable("127.0.0.1", 5432), reason="控制面 PostgreSQL 未启动"
)
requires_mysql = pytest.mark.skipif(
    not _reachable("127.0.0.1", 13306), reason="MySQL e2e 未启动（--profile e2e）"
)

pytestmark = pytest.mark.integration


# ----------------------------------------------------- 本地 KV v2 语义服务
class _KvV2Handler(BaseHTTPRequestHandler):
    """实现 HTTPS SecretProvider 需要的最小子集：写、读、软删除。"""

    # 类级存储：测试期间跨请求共享，显式声明避免被误当实例属性。
    store: ClassVar[dict[str, dict[str, Any]]] = {}
    versions: ClassVar[dict[str, int]] = {}

    def log_message(self, *args: Any) -> None:  # 静音
        return

    def _path(self) -> str:
        parts = urlsplit(self.path)
        remainder = unquote(parts.path).removeprefix(f"/v1/{VAULT_MOUNT}/data/")
        return remainder

    def _body(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(length) or b"{}")

    def _respond(self, code: int, payload: dict[str, Any] | None = None) -> None:
        raw = json.dumps(payload or {}).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        if code != 204:
            self.wfile.write(raw)

    def _authorized(self) -> bool:
        return self.headers.get("X-Vault-Token") == VAULT_TOKEN

    def do_POST(self) -> None:
        if not self._authorized():
            self._respond(403)
            return
        path = self._path()
        type(self).store[path] = self._body().get("data", {})
        type(self).versions[path] = type(self).versions.get(path, 0) + 1
        self._respond(200, {"data": {"version": type(self).versions[path]}})

    def do_GET(self) -> None:
        if not self._authorized():
            self._respond(403)
            return
        path = self._path()
        if path not in type(self).store:
            self._respond(404, {"errors": []})
            return
        self._respond(
            200,
            {
                "data": {
                    "data": type(self).store[path],
                    "metadata": {"version": type(self).versions[path]},
                }
            },
        )

    def do_DELETE(self) -> None:
        if not self._authorized():
            self._respond(403)
            return
        path = self._path()
        type(self).store.pop(path, None)
        self._respond(204)


# --------------------------------------------------------- 本地 Webhook 接收端
class _WebhookHandler(BaseHTTPRequestHandler):
    received: ClassVar[list[dict[str, Any]]] = []

    def log_message(self, *args: Any) -> None:  # 静音
        return

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length)
        type(self).received.append(
            {
                "delivery_id": json.loads(raw).get("delivery_id"),
                "signature": self.headers.get("X-DIP-Signature"),
                "raw": raw,
            }
        )
        self.send_response(200)
        self.send_header("Content-Length", "0")
        self.end_headers()


@contextmanager
def _serve(handler: type[BaseHTTPRequestHandler]) -> Iterator[tuple[str, int]]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield "127.0.0.1", server.server_address[1]
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


@pytest.fixture
def fake_vault(monkeypatch: pytest.MonkeyPatch) -> Iterator[str]:
    """本地 KV v2 服务；同时把配置指向它并让 settings 重新读取。"""
    _KvV2Handler.store = {}
    _KvV2Handler.versions = {}
    # ExitStack 让服务在整个测试期间保持存活：写在 with 块里的 yield 一退出服务就关了。
    with ExitStack() as stack:
        host, port = stack.enter_context(_serve(_KvV2Handler))
        url = f"http://{host}:{port}"
        monkeypatch.setenv("DIP_VAULT_ADDRESS", url)
        monkeypatch.setenv("DIP_VAULT_TOKEN", VAULT_TOKEN)
        monkeypatch.setenv("DIP_VAULT_KV_MOUNT", VAULT_MOUNT)
        get_settings.cache_clear()
        yield url
    get_settings.cache_clear()


@pytest.fixture
def fake_webhook(monkeypatch: pytest.MonkeyPatch) -> Iterator[str]:
    _WebhookHandler.received = []
    with ExitStack() as stack:
        host, port = stack.enter_context(_serve(_WebhookHandler))
        url = f"http://{host}:{port}/hook"
        monkeypatch.setenv("DIP_CREDENTIAL_ALERT_WEBHOOK_URL", url)
        monkeypatch.setenv("DIP_CREDENTIAL_ALERT_WEBHOOK_ALLOWED_HOSTS", "127.0.0.1")
        monkeypatch.setenv(
            "DIP_CREDENTIAL_ALERT_WEBHOOK_SIGNING_SECRET", "signing-secret-32-bytes-minimum!"
        )
        get_settings.cache_clear()
        yield url
    get_settings.cache_clear()


@pytest.fixture
async def session_factory() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = create_async_engine(CONTROL_DB, poolclass=NullPool)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    yield factory
    await engine.dispose()


async def seed_approver(
    session: AsyncSession, *, project_id: uuid.UUID, role: HarnessRole
) -> uuid.UUID:
    """造一个持有指定职责槽的审批人。

    每个槽位一个人：SOD-02 不允许同一个人在**同一张**准备单上占两个槽，
    所以双人审批的两次调用必须拿到两个不同主体。
    """
    user = User(subject=f"approver-{uuid.uuid4().hex[:8]}", display_name="审批人")
    session.add(user)
    await session.flush()
    session.add(
        ProjectMembership(project_id=project_id, user_id=user.id, role=ProjectRole.APPROVER)
    )
    session.add(ProjectRoleGrant(project_id=project_id, user_id=user.id, role=role))
    await session.flush()
    return user.id


async def approve_and_commit(
    factory: async_sessionmaker[AsyncSession],
    *,
    connection_id: uuid.UUID,
    service: Any,
    request_id: uuid.UUID,
    roles: tuple[HarnessRole, ...] = (HarnessRole.SECURITY_APPROVER,),
) -> None:
    """走完受管入口的「具名审批 → 提交」。

    集成用例关心的是真库上的业务结果，不关心审批人是谁；但**审批这一步不能跳** ——
    它正是 T-36 要求所有副作用都经过的那道门，跳过去测到的就不是真实链路了。
    这里走的是**业务服务自己的 `decide`**：受管记录与业务状态必须一起推进，
    只推一边会让业务行停在 PENDING、执行时被状态机拦下（踩过一次）。
    双槽操作（凭据销毁）每个槽位单独一个人，见 `seed_approver`。
    """
    for role in roles:
        async with factory() as session:
            async with session.begin():
                connection = await session.get(DataConnection, connection_id)
                assert connection is not None
                approver = await seed_approver(session, project_id=connection.project_id, role=role)
                await service.decide(
                    session,
                    request_id=request_id,
                    actor_id=approver,
                    decision=HarnessApprovalValue.APPROVE,
                    reason="集成验证：具名审批",
                    role=role,
                )


async def seed_connection(
    factory: async_sessionmaker[AsyncSession],
    *,
    password: str = MYSQL_PASSWORD,
    username: str = "dip_reader",
) -> tuple[uuid.UUID, uuid.UUID]:
    """建一个使用 fernet-db 凭据的真实 MySQL 连接，并给出一个有写权限的申请人。

    返回 `(connection_id, actor_id)`：申请人必须是项目成员，否则连申请都发不出去。
    """
    async with factory() as session:
        async with session.begin():
            project = Project(name=f"it-secret-{uuid.uuid4().hex[:10]}")
            session.add(project)
            await session.flush()
            actor = User(subject=f"actor-{uuid.uuid4().hex[:8]}", display_name="申请人")
            session.add(actor)
            await session.flush()
            session.add(
                ProjectMembership(
                    project_id=project.id, user_id=actor.id, role=ProjectRole.DATA_ENGINEER
                )
            )
            secrets = create_secret_provider(session)
            stored = await secrets.store(
                project_id=project.id,
                name="mysql-source",
                credentials=ConnectionCredentials(username=username, password=SecretStr(password)),
            )
            connection = DataConnection(
                project_id=project.id,
                name="mysql-source",
                connector_type=ConnectorType.MYSQL,
                endpoint_role=DataEndpointRole.SOURCE,
                status=ConnectionStatus.READY,
                host="127.0.0.1",
                port=13306,
                database_name="source_db",
                options={},
                secret_ref=stored.secret_ref,
                created_by=actor.id,
            )
            session.add(connection)
            await session.flush()
            return connection.id, actor.id


# ------------------------------------------------- 跨 Provider 迁移（FR-18）
@requires_control_db
async def test_migration_moves_credential_between_providers_and_keeps_source(
    session_factory: async_sessionmaker[AsyncSession], fake_vault: str
) -> None:
    connection_id, actor = await seed_connection(session_factory)
    service = SecretMigrationService()

    async with session_factory() as session:
        async with session.begin():
            request = await service.request(
                session,
                connection_id=connection_id,
                actor_id=actor,
                target_provider="vault-kv-v2",
                reason="迁移到企业 Secret Provider",
            )
            source_ref = request.source_secret_ref
            request_id = request.id

    await approve_and_commit(
        session_factory,
        connection_id=connection_id,
        service=service,
        request_id=request_id,
    )

    async with session_factory() as session:
        async with session.begin():
            outcome = await service.execute(session, request_id=request_id)
        assert outcome.succeeded is True, outcome.reason
        row = await session.get(SecretMigrationRequest, request_id)
        assert row is not None
        connection = await session.get(DataConnection, connection_id)
        assert connection is not None
        assert connection.secret_ref is not None
        assert connection.secret_ref.startswith("vault://")
        assert row.target_secret_ref == connection.secret_ref
        # 源凭据未被销毁：这正是「可回退」的依据。
        surviving = await session.scalar(
            select(EncryptedSecret.id).where(EncryptedSecret.secret_ref == source_ref)
        )
        assert surviving is not None

    # 从「Vault」读回来必须能还原出同一份凭据。
    async with session_factory() as session:
        connection = await session.get(DataConnection, connection_id)
        assert connection is not None and connection.secret_ref is not None
        # 迁移后引用属于目标 Provider：按引用选 Provider 才能读对存储。
        migrated = await secret_provider_for_ref(session, connection.secret_ref).load(
            connection.secret_ref
        )
    assert migrated.username == "dip_reader"
    assert migrated.password is not None
    assert migrated.password.get_secret_value() == MYSQL_PASSWORD


# ------------------------------------------------- 轮换验证连真库（FR-19）
def _mysql_root() -> Any:
    import pymysql

    return pymysql.connect(host="127.0.0.1", port=13306, user="root", password="root_e2e")


def _set_mysql_password(user: str, password: str) -> None:
    connection = _mysql_root()
    try:
        with connection.cursor() as cursor:
            cursor.execute(f"ALTER USER '{user}'@'%' IDENTIFIED BY '{password}'")
        connection.commit()
    finally:
        connection.close()


@pytest.fixture
def rotating_mysql_user() -> Iterator[tuple[str, str, str]]:
    """专用 MySQL 账号。

    轮换测试会**真的去改数据源侧的口令**（这才是轮换的完整闭环），
    因此绝不能借用 M2 集成测试共享的 `dip_reader`，否则会把别人的测试带崩。
    """
    import pymysql  # noqa: F401  仅用于显式声明依赖

    user = f"dip_rot_{uuid.uuid4().hex[:6]}"
    password_v1, password_v2 = "rot-v1-password", "rot-v2-password"
    connection = _mysql_root()
    try:
        with connection.cursor() as cursor:
            cursor.execute(f"CREATE USER '{user}'@'%' IDENTIFIED BY '{password_v1}'")
            cursor.execute(f"GRANT SELECT ON source_db.* TO '{user}'@'%'")
        connection.commit()
    finally:
        connection.close()
    try:
        yield user, password_v1, password_v2
    finally:
        cleanup = _mysql_root()
        try:
            with cleanup.cursor() as cursor:
                cursor.execute(f"DROP USER IF EXISTS '{user}'@'%'")
            cleanup.commit()
        finally:
            cleanup.close()


@requires_control_db
@requires_mysql
async def test_rotation_validation_uses_real_connectivity(
    session_factory: async_sessionmaker[AsyncSession],
    rotating_mysql_user: tuple[str, str, str],
) -> None:
    """完整闭环：源端改口令 → 轮换 → 新凭据真的能连上。"""
    user, password_v1, password_v2 = rotating_mysql_user
    connection_id, actor = await seed_connection(
        session_factory, username=user, password=password_v1
    )
    service = SecretRotationService()

    async def new_rotation(password: str) -> uuid.UUID:
        async with session_factory() as session:
            async with session.begin():
                row = await service.request(
                    session,
                    connection_id=connection_id,
                    actor_id=actor,
                    new_credentials=ConnectionCredentials(
                        username=user, password=SecretStr(password)
                    ),
                    reason="轮换",
                )
                return row.id

    # ① 源端尚未改口令时提交「新口令」：验证必须失败并停在 FAILED，绝不能进入审批
    bad_id = await new_rotation(password_v2)
    async with session_factory() as session:
        async with session.begin():
            outcome = await service.validate(
                session, request_id=bad_id, connectivity=_probing(session)
            )
    assert outcome.succeeded is False
    async with session_factory() as session:
        row = await session.get(SecretRotationRequest, bad_id)
        assert row is not None
        assert row.status is SecretRotationStatus.FAILED
        assert row.validated_at is None
        assert row.failure_reason is not None
        # 失败原因不得回显口令明文
        assert password_v2 not in row.failure_reason
        connection = await session.get(DataConnection, connection_id)
        assert connection is not None
        # 验证失败不改变连接正在使用的凭据
        assert connection.secret_ref == row.previous_secret_ref

    # ② 在数据源侧改口令后重新申请：验证通过，进入等待审批
    _set_mysql_password(user, password_v2)
    good_id = await new_rotation(password_v2)
    async with session_factory() as session:
        async with session.begin():
            outcome = await service.validate(
                session, request_id=good_id, connectivity=_probing(session)
            )
    assert outcome.succeeded is True, outcome.reason
    async with session_factory() as session:
        row = await session.get(SecretRotationRequest, good_id)
        assert row is not None
        assert row.status is SecretRotationStatus.WAITING_APPROVAL
        assert row.validated_at is not None
        staged_ref = row.staged_secret_ref
        previous_ref = row.previous_secret_ref
        # 激活前连接仍在用旧凭据
        connection = await session.get(DataConnection, connection_id)
        assert connection is not None
        assert connection.secret_ref == previous_ref

    # ③ 激活后连接切到新凭据，并且**新凭据真的能连上真库**
    await approve_and_commit(
        session_factory,
        connection_id=connection_id,
        service=service,
        request_id=good_id,
    )
    async with session_factory() as session:
        async with session.begin():
            activated = await service.execute(session, request_id=good_id)
    assert activated.succeeded is True, activated.reason

    async with session_factory() as session:
        connection = await session.get(DataConnection, connection_id)
        assert connection is not None
        assert connection.secret_ref == staged_ref
        assert connection.secret_ref != previous_ref
        assert connection.credential_issued_at is not None
        probe = _probing(session)
        live = await probe.verify_credentials(
            connection=connection,
            credentials=await secret_provider_for_ref(session, staged_ref).load(staged_ref),
        )
    assert live.succeeded is True, live.reason


def _probing(session: AsyncSession) -> ProbingService:
    return ProbingService(registry=ProbeRegistry(), secret_provider=create_secret_provider(session))


# ----------------------------------------------- 告警与 Webhook 投递（FR-22）
@requires_control_db
async def test_webhook_delivery_is_idempotent_by_delivery_id(
    session_factory: async_sessionmaker[AsyncSession], fake_webhook: str
) -> None:
    connection_id, _actor = await seed_connection(session_factory)
    async with session_factory() as session:
        async with session.begin():
            connection = await session.get(DataConnection, connection_id)
            assert connection is not None
            connection.credential_rotation_enabled = True
            connection.credential_max_age_days = 30
            connection.credential_warning_days = 7
            connection.credential_issued_at = datetime.now(UTC) - timedelta(days=40)

    async with session_factory() as session:
        async with session.begin():
            service = CredentialAlertService()
            changed = await service.scan(session)
            created = await service.enqueue_deliveries(session)
    # 扫描是全局的：只挑出本次连接产生的告警与投递。
    mine = [alert for alert in changed if alert.connection_id == connection_id]
    assert len(mine) == 1
    deliveries = [item for item in created if item.alert_id == mine[0].id]
    assert len(deliveries) == 1
    delivery_id = deliveries[0].delivery_id

    async with session_factory() as session:
        async with session.begin():
            first = await CredentialAlertService().deliver(session, delivery_id=delivery_id)
    assert first.succeeded is True, first.reason
    assert len(_WebhookHandler.received) == 1
    payload = _WebhookHandler.received[0]
    assert payload["delivery_id"] == delivery_id
    # 配了签名密钥就必须带签名，接收端才能验真
    assert payload["signature"] is not None and payload["signature"].startswith("sha256=")
    body = json.loads(payload["raw"])
    assert body["level"] == "OVERDUE"
    assert body["connection_id"] == str(connection_id)
    # 外发内容不得包含凭据或引用
    assert "secret_ref" not in body
    assert MYSQL_PASSWORD not in payload["raw"].decode()

    # 再次投递同一 delivery_id：不得产生第二次外发
    async with session_factory() as session:
        async with session.begin():
            again = await CredentialAlertService().deliver(session, delivery_id=delivery_id)
    assert again.succeeded is True
    assert len(_WebhookHandler.received) == 1

    # 投递记录状态与投递 ID 唯一约束都留档
    async with session_factory() as session:
        row = await session.scalar(
            select(CredentialAlertDelivery).where(
                CredentialAlertDelivery.delivery_id == delivery_id
            )
        )
        assert row is not None
        assert row.status is CredentialAlertDeliveryStatus.SUCCEEDED
        assert row.attempts == 1
        alert = await session.get(CredentialExpiryAlert, row.alert_id)
        assert alert is not None
        assert alert.status is CredentialAlertStatus.OPEN
        assert alert.delivery_required is True


@requires_control_db
async def test_alert_scan_without_webhook_keeps_alerts_in_platform(
    session_factory: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    """未配置 Webhook 时告警照常留存，只是不外发 —— 不静默丢弃。"""
    monkeypatch.setenv("DIP_CREDENTIAL_ALERT_WEBHOOK_URL", "")
    get_settings.cache_clear()
    connection_id, _actor = await seed_connection(session_factory)
    async with session_factory() as session:
        async with session.begin():
            connection = await session.get(DataConnection, connection_id)
            assert connection is not None
            connection.credential_rotation_enabled = True
            connection.credential_max_age_days = 30
            connection.credential_warning_days = 7
            connection.credential_issued_at = datetime.now(UTC) - timedelta(days=25)
    assert get_settings().credential_alert_webhook_url is None

    async with session_factory() as session:
        async with session.begin():
            service = CredentialAlertService()
            changed = await service.scan(session)
            created = await service.enqueue_deliveries(session)
    mine = [alert for alert in changed if alert.connection_id == connection_id]
    assert len(mine) == 1
    assert [item for item in created if item.alert_id == mine[0].id] == []
    assert mine[0].delivery_required is False


@requires_control_db
async def test_sod01_applies_to_real_rotation_decision(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """真库上也必须拦住自批：申请人 == 审批人时拒绝。"""
    connection_id, actor = await seed_connection(session_factory)
    async with session_factory() as session:
        async with session.begin():
            connection = await session.get(DataConnection, connection_id)
            assert connection is not None
            # 申请人在真库上也得先具备「能进审批台 + 持有该槽位」这两个条件，
            # 否则拦下他的是权限而不是 SOD-01，测到的就不是自批了。
            membership = await session.scalar(
                select(ProjectMembership).where(
                    ProjectMembership.project_id == connection.project_id,
                    ProjectMembership.user_id == actor,
                )
            )
            assert membership is not None
            # 取 ADMIN：它同时落在「能申请」（WRITABLE）与「能进审批台」（APPROVER_ROLES）里，
            # 这样申请人才能既发得出申请、又有资格去批自己那张单。
            membership.role = ProjectRole.ADMIN
            session.add(
                ProjectRoleGrant(
                    project_id=connection.project_id,
                    user_id=actor,
                    role=HarnessRole.SECURITY_APPROVER,
                )
            )
            await session.flush()

            row = await SecretRotationService().request(
                session,
                connection_id=connection_id,
                actor_id=actor,
                new_credentials=ConnectionCredentials(
                    username="dip_reader", password=SecretStr("x")
                ),
                reason="轮换",
            )
            row.status = SecretRotationStatus.WAITING_APPROVAL
            row.validated_at = datetime.now(UTC)
            request_id = row.id

    from data_integration_platform.domain.errors import PermissionDenied

    async with session_factory() as session:
        with pytest.raises(PermissionDenied, match="SOD-01"):
            await SecretRotationService().decide(
                session,
                request_id=request_id,
                actor_id=actor,
                decision=HarnessApprovalValue.APPROVE,
                reason="自批",
            )


@requires_control_db
async def test_fernet_provider_reports_not_ready_without_master_key(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """主密钥缺失时凭据存储失败关闭，而不是写入可被随意解密的密文。"""
    from data_integration_platform.domain.errors import ExternalServiceUnavailable

    async with session_factory() as session:
        provider = FernetDbSecretProvider(session, None)
        with pytest.raises(ExternalServiceUnavailable, match="未就绪"):
            await provider.store(
                project_id=uuid.uuid4(),
                name="x",
                credentials=ConnectionCredentials(username="u", password=SecretStr("p")),
            )
