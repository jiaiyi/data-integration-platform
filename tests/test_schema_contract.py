"""ORM 模型的结构契约。

这里锁住的都是**只在真实数据库上才会暴露**的一类问题：SQLite 不校验 VARCHAR 长度、
不校验枚举取值，所以模型层的疏忽会一路过测到 PostgreSQL 才炸。
"""

from sqlalchemy import Enum as SAEnum

from data_integration_platform.infrastructure import models  # noqa: F401
from data_integration_platform.infrastructure.database import Base
from data_integration_platform.infrastructure.models import ENUM_COLUMN_LENGTH


def test_control_plane_table_count() -> None:
    """PRD §8.2 分组共 41 张表；数字变化必须是有意的。"""
    assert len(Base.metadata.sorted_tables) == 41


def test_enum_values_fit_the_declared_column_length() -> None:
    """枚举列长度固定，**新增更长的枚举值必须在测试里先失败**。

    实测踩过：`audit_events.event_type` 曾按「建表当时最长枚举值」生成 varchar(19)，
    后来新增的 `SECRET_ROTATION_REQUESTED`（23 字符）在 PostgreSQL 上直接
    `value too long for type character varying(19)`；SQLite 不校验长度，所以完全没拦住。
    """
    offenders: list[tuple[str, str, str]] = []
    for table in Base.metadata.sorted_tables:
        for column in table.columns:
            if not isinstance(column.type, SAEnum):
                continue
            enum_class = column.type.enum_class
            if not isinstance(enum_class, type):
                continue
            values = [member.value for member in enum_class]
            if not values:
                continue
            longest = max(values, key=len)
            if len(longest) > ENUM_COLUMN_LENGTH:
                offenders.append((table.name, column.name, longest))
    assert offenders == [], f"枚举值超出列长度 {ENUM_COLUMN_LENGTH}：{offenders}"


def test_every_enum_column_declares_an_explicit_length() -> None:
    """枚举列必须显式带长度，否则长度会随枚举取值漂移。"""
    missing = [
        (table.name, column.name)
        for table in Base.metadata.sorted_tables
        for column in table.columns
        if isinstance(column.type, SAEnum) and column.type.length is None
    ]
    assert missing == [], f"以下枚举列没有显式长度：{missing}"
