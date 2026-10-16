"""数据库会话基础设施（PostgreSQL 业务真相）。"""

from collections.abc import AsyncIterator

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase

from data_integration_platform.config import get_settings


class Base(DeclarativeBase):
    """所有 ORM 模型的声明基类；metadata 是 Alembic autogenerate 的目标。"""


def create_engine(database_url: str | None = None) -> AsyncEngine:
    """按显式 URL 或进程配置创建异步引擎；pool_pre_ping 抵抗连接被中途回收。"""
    url = database_url or get_settings().database_url
    return create_async_engine(url, pool_pre_ping=True)


engine = create_engine()
SessionFactory = async_sessionmaker(engine, expire_on_commit=False)


async def get_session() -> AsyncIterator[AsyncSession]:
    """FastAPI 依赖：为每个请求提供一个会话，事务边界由用例显式开启。"""
    async with SessionFactory() as session:
        yield session
