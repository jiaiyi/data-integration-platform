"""测试夹具。

单元测试用内存 SQLite 承载 ORM，避免依赖本机 PostgreSQL：
迁移与真实方言由 `alembic upgrade head` 单独验证，这里只验证应用行为。
"""

from collections.abc import AsyncIterator

import pytest
from httpx import ASGITransport, AsyncClient
from pydantic import SecretStr
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from data_integration_platform.config import Settings, get_settings
from data_integration_platform.infrastructure.database import Base, get_session
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
