"""测试夹具。

单元测试用内存 SQLite 承载 ORM，避免依赖本机 PostgreSQL：
迁移与真实方言由 `alembic upgrade head` 单独验证，这里只验证应用行为。

环境变量必须在**导入项目模块之前**设置：`config.get_settings` 是 `lru_cache` 单例，
且 `main.py` 在导入时就会取一次，之后再设就晚了。
"""

import os
import tempfile
from pathlib import Path

from cryptography.fernet import Fernet

# 凭据存储需要主密钥；测试自备一把，不依赖开发者本地 .env。
os.environ.setdefault("DIP_SECRET_MASTER_KEY", Fernet.generate_key().decode())
# 受管文件资产写到临时目录，不污染仓库工作区。
os.environ.setdefault(
    "DIP_FILE_ASSET_ROOT", str(Path(tempfile.mkdtemp(prefix="dip-test-assets-")) / "root")
)

import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass

import pytest
from httpx import ASGITransport, AsyncClient
from pydantic import SecretStr
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from data_integration_platform.config import Settings, get_settings
from data_integration_platform.domain.enums import ProjectRole
from data_integration_platform.infrastructure.database import Base, get_session
from data_integration_platform.infrastructure.models import (
    Project,
    ProjectMembership,
    User,
)
from data_integration_platform.main import app

TEST_BOOTSTRAP_TOKEN = "b" * 40


@pytest.fixture
async def session_factory() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    """内存库会话工厂。

    必须显式使用 StaticPool：`:memory:` 库随连接创建，默认连接池会让每个连接
    拿到各自独立的空库，导致建表与查询落在不同数据库上。
    """
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", poolclass=StaticPool)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


@pytest.fixture
async def session(
    session_factory: async_sessionmaker[AsyncSession],
) -> AsyncIterator[AsyncSession]:
    async with session_factory() as active:
        yield active


@pytest.fixture
def test_settings() -> Settings:
    """判定测试用的配置：本地身份模式 + 已启用的一次性初始化接口。"""
    return Settings(
        _env_file=None,
        environment="local",
        auth_mode="dev-header",
        bootstrap_enabled=True,
        bootstrap_token=SecretStr(TEST_BOOTSTRAP_TOKEN),
    )


@pytest.fixture
async def client(
    session_factory: async_sessionmaker[AsyncSession], test_settings: Settings
) -> AsyncIterator[AsyncClient]:
    """覆盖会话与配置依赖的 ASGI 测试客户端。"""

    async def override_session() -> AsyncIterator[AsyncSession]:
        async with session_factory() as active:
            yield active

    app.dependency_overrides[get_session] = override_session
    app.dependency_overrides[get_settings] = lambda: test_settings
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://control-plane") as instance:
        yield instance
    app.dependency_overrides.clear()


@dataclass(frozen=True, slots=True)
class ProjectContext:
    """一个项目 + 一名数据工程师（可写角色）的标识。

    管道版本与调度都要「项目 + 可写主体」才能落库，两个测试模块共用这一份，
    免得各自搭一套略有差别的项目夹具。
    """

    project_id: uuid.UUID
    engineer_id: uuid.UUID


@pytest.fixture
async def project(session: AsyncSession) -> ProjectContext:
    created = Project(name=f"项目-{uuid.uuid4().hex[:8]}")
    engineer = User(subject=f"engineer-{uuid.uuid4().hex[:8]}", display_name="李四")
    session.add_all([created, engineer])
    await session.flush()
    session.add(
        ProjectMembership(
            project_id=created.id, user_id=engineer.id, role=ProjectRole.DATA_ENGINEER
        )
    )
    await session.flush()
    return ProjectContext(project_id=created.id, engineer_id=engineer.id)
