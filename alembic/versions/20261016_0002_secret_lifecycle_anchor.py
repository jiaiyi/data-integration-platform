"""secret lifecycle anchor: connection_id + nullable secret_id

Revision ID: 20261016_0002
Revises: 20261016_0001
Create Date: 2026-10-16

T-15 凭据全生命周期的前置修正。三处都是「两个事实源」或「锚点选错」的问题：

1. 4 张请求表补 `connection_id`（非空）。原来的锚点是 `secret_id` → `encrypted_secrets`，
   而 Vault 后端的凭据**在本地没有密文行**，只以 `vault://` 引用存在 ——
   以 `secret_id` 为必填键，Vault 凭据根本无法登记迁移/轮换/销毁。
2. `secret_id` 放宽为可空，且外键动作由 `CASCADE` 改为 `SET NULL`：
   销毁凭据属常规操作（FR-21），若保持 CASCADE，删除 `encrypted_secrets` 行会把
   轮换与销毁申请记录一并级联删掉 —— 治理记录被抹掉比没有记录更糟。
3. 轮换表去掉 `staged_ciphertext` / `staged_key_id`：暂存凭据由 Provider 落库，
   本表只保留 `staged_secret_ref` 引用；再存一份密文等于两处事实源。
4. 迁移表补 `source_secret_ref`：换掉连接引用后就找不到源凭据了，
   而 FR-18 要求「不销毁源凭据、可回退」——回退的依据必须留档。
5. 轮换表补 `previous_credential_issued_at`：激活会覆盖连接的签发时间，
   回退时必须还原，否则到期告警要么误报、要么被静默关闭。

前提：这 4 张表在本次迁移之前**没有任何写入路径**，必然为空，因此可以直接加 NOT NULL 列。
若真存在数据，迁移会明确失败，而不是静默填一个错的 connection_id。
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "20261016_0002"
down_revision: str | None = "20261016_0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# 初始迁移里的外键是匿名创建的，PostgreSQL 按 `<表>_<列>_fkey` 命名；
# 名字不符时迁移会明确失败，而不是悄悄跳过约束修改。
_LIFECYCLE_TABLES = (
    "secret_migration_requests",
    "secret_rotation_requests",
    "secret_rotation_rollback_requests",
    "secret_destruction_requests",
)
_SECRET_ID_TABLES = (
    "secret_migration_requests",
    "secret_rotation_requests",
    "secret_destruction_requests",
)


def _secret_id_fkey(table: str) -> str:
    return f"{table}_secret_id_fkey"


def upgrade() -> None:
    """为凭据生命周期申请补上正确的锚点与外键语义。"""
    for table in _LIFECYCLE_TABLES:
        op.add_column(table, sa.Column("connection_id", sa.Uuid(), nullable=False))
        op.create_foreign_key(
            f"fk_{table}_connection_id",
            table,
            "data_connections",
            ["connection_id"],
            ["id"],
            ondelete="CASCADE",
        )
        op.create_index(f"ix_{table}_connection_id", table, ["connection_id"])

    for table in _SECRET_ID_TABLES:
        op.drop_constraint(_secret_id_fkey(table), table, type_="foreignkey")
        op.alter_column(table, "secret_id", existing_type=sa.Uuid(), nullable=True)
        op.create_foreign_key(
            f"fk_{table}_secret_id",
            table,
            "encrypted_secrets",
            ["secret_id"],
            ["id"],
            ondelete="SET NULL",
        )

    op.add_column(
        "secret_migration_requests",
        sa.Column("source_secret_ref", sa.String(length=512), nullable=True),
    )
    op.add_column(
        "secret_rotation_requests",
        sa.Column("previous_credential_issued_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.drop_column("secret_rotation_requests", "staged_ciphertext")
    op.drop_column("secret_rotation_requests", "staged_key_id")


def downgrade() -> None:
    """回退到 T-15 之前的表结构。"""
    op.drop_column("secret_rotation_requests", "previous_credential_issued_at")
    op.drop_column("secret_migration_requests", "source_secret_ref")
    op.add_column(
        "secret_rotation_requests", sa.Column("staged_key_id", sa.String(length=128), nullable=True)
    )
    op.add_column(
        "secret_rotation_requests", sa.Column("staged_ciphertext", sa.LargeBinary(), nullable=True)
    )

    for table in _SECRET_ID_TABLES:
        op.drop_constraint(f"fk_{table}_secret_id", table, type_="foreignkey")
        op.alter_column(table, "secret_id", existing_type=sa.Uuid(), nullable=False)
        op.create_foreign_key(
            _secret_id_fkey(table),
            table,
            "encrypted_secrets",
            ["secret_id"],
            ["id"],
            ondelete="CASCADE",
        )

    for table in _LIFECYCLE_TABLES:
        op.drop_index(f"ix_{table}_connection_id", table_name=table)
        op.drop_constraint(f"fk_{table}_connection_id", table, type_="foreignkey")
        op.drop_column(table, "connection_id")
