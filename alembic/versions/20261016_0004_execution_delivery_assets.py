"""link delivered execution reports back to their run

Revision ID: 20261016_0004
Revises: 20261016_0003
Create Date: 2026-10-16

T-40 把执行结果交付成**受管文件对象**，交付物要能回答两个问题：
「这次执行交付了什么」与「同一次执行是不是被交付了两遍」。

靠文件名约定回答不了这两个问题：名字可以被改、也可以重名。
所以给 `file_assets` 加一个可空的 `execution_run_id`：
非空表示这份资产是某次执行的交付物，用户上传的资产留空。

`ondelete="SET NULL"` 而不是 CASCADE：**执行记录被删不该让交付物跟着消失**。
交付物是执行结果的留档，删掉运行记录属于审计上的可疑动作，让它留下痕迹更好。

唯一约束 `(execution_run_id, name)` 是「重复交付同一份」的防线：
第二次交付会撞上约束，服务层据此把已有的那一份返回，而不是再落一份重复文件。
PostgreSQL 与 SQLite 都把 NULL 视作彼此不等，所以用户上传的资产（run_id 为空）
不受这条约束影响。
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "20261016_0004"
down_revision: str | None = "20261016_0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "file_assets",
        sa.Column("execution_run_id", sa.Uuid(as_uuid=True), nullable=True),
    )
    op.create_foreign_key(
        "fk_file_assets_execution_run",
        "file_assets",
        "execution_runs",
        ["execution_run_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_unique_constraint(
        "uq_file_assets_run_name", "file_assets", ["execution_run_id", "name"]
    )


def downgrade() -> None:
    op.drop_constraint("uq_file_assets_run_name", "file_assets", type_="unique")
    op.drop_constraint("fk_file_assets_execution_run", "file_assets", type_="foreignkey")
    op.drop_column("file_assets", "execution_run_id")
