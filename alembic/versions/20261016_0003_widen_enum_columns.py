"""widen enum columns to a fixed length

Revision ID: 20261016_0003
Revises: 20261016_0002
Create Date: 2026-10-16

枚举列此前未指定长度，SQLAlchemy 会按「建表当时枚举值的最长值」生成 VARCHAR(n)，
于是**新增一个更长的枚举值就会在 PostgreSQL 上插入失败** —— 实测踩过：
`audit_events.event_type` 是 varchar(19)，而新增的 `SECRET_ROTATION_REQUESTED` 长 23，
插入直接报 `value too long for type character varying(19)`。
SQLite 不校验 VARCHAR 长度，因此这个问题只在真库上暴露。

这里把 58 个枚举列统一加宽到 VARCHAR(64)，与 `models.ENUM_COLUMN_LENGTH` 对齐；
之后枚举增长只改代码，不再需要配套迁移。

各列原始长度取自 `information_schema`（即真实建表结果），逐个写进 `existing_type`。
降级按同一份原始长度收窄：若此刻已存在更长的值，收窄会失败 —— 这是对的，
总比静默截断数据好。
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "20261016_0003"
down_revision: str | None = "20261016_0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """把所有枚举列加宽到固定长度。"""
    # audit_events
    op.alter_column(
        "audit_events",
        "event_type",
        existing_type=sa.String(length=19),
        type_=sa.String(length=64),
        existing_nullable=False,
    )
    # benchmark_runs
    op.alter_column(
        "benchmark_runs",
        "status",
        existing_type=sa.String(length=9),
        type_=sa.String(length=64),
        existing_nullable=False,
    )
    op.alter_column(
        "benchmark_runs",
        "gate_conclusion",
        existing_type=sa.String(length=6),
        type_=sa.String(length=64),
        existing_nullable=True,
    )
    # harness_policy_decisions
    op.alter_column(
        "harness_policy_decisions",
        "outcome",
        existing_type=sa.String(length=16),
        type_=sa.String(length=64),
        existing_nullable=False,
    )
    op.alter_column(
        "harness_policy_decisions",
        "risk_level",
        existing_type=sa.String(length=2),
        type_=sa.String(length=64),
        existing_nullable=False,
    )
    # harness_side_effect_preparations
    op.alter_column(
        "harness_side_effect_preparations",
        "operation",
        existing_type=sa.String(length=24),
        type_=sa.String(length=64),
        existing_nullable=False,
    )
    op.alter_column(
        "harness_side_effect_preparations",
        "status",
        existing_type=sa.String(length=16),
        type_=sa.String(length=64),
        existing_nullable=False,
    )
    op.alter_column(
        "harness_side_effect_preparations",
        "risk_level",
        existing_type=sa.String(length=2),
        type_=sa.String(length=64),
        existing_nullable=False,
    )
    op.alter_column(
        "harness_side_effect_preparations",
        "execution_status",
        existing_type=sa.String(length=7),
        type_=sa.String(length=64),
        existing_nullable=False,
    )
    # projects
    op.alter_column(
        "projects",
        "status",
        existing_type=sa.String(length=8),
        type_=sa.String(length=64),
        existing_nullable=False,
    )
    # users
    op.alter_column(
        "users",
        "status",
        existing_type=sa.String(length=8),
        type_=sa.String(length=64),
        existing_nullable=False,
    )
    # data_connections
    op.alter_column(
        "data_connections",
        "connector_type",
        existing_type=sa.String(length=10),
        type_=sa.String(length=64),
        existing_nullable=False,
    )
    op.alter_column(
        "data_connections",
        "endpoint_role",
        existing_type=sa.String(length=6),
        type_=sa.String(length=64),
        existing_nullable=False,
    )
    op.alter_column(
        "data_connections",
        "status",
        existing_type=sa.String(length=8),
        type_=sa.String(length=64),
        existing_nullable=False,
    )
    # file_assets
    op.alter_column(
        "file_assets",
        "format",
        existing_type=sa.String(length=8),
        type_=sa.String(length=64),
        existing_nullable=False,
    )
    op.alter_column(
        "file_assets",
        "endpoint_role",
        existing_type=sa.String(length=6),
        type_=sa.String(length=64),
        existing_nullable=False,
    )
    # harness_evidence_events
    op.alter_column(
        "harness_evidence_events",
        "risk_level",
        existing_type=sa.String(length=2),
        type_=sa.String(length=64),
        existing_nullable=False,
    )
    # harness_side_effect_approvals
    op.alter_column(
        "harness_side_effect_approvals",
        "role",
        existing_type=sa.String(length=17),
        type_=sa.String(length=64),
        existing_nullable=False,
    )
    op.alter_column(
        "harness_side_effect_approvals",
        "decision",
        existing_type=sa.String(length=7),
        type_=sa.String(length=64),
        existing_nullable=False,
    )
    # project_memberships
    op.alter_column(
        "project_memberships",
        "role",
        existing_type=sa.String(length=13),
        type_=sa.String(length=64),
        existing_nullable=False,
    )
    # project_role_grants
    op.alter_column(
        "project_role_grants",
        "role",
        existing_type=sa.String(length=17),
        type_=sa.String(length=64),
        existing_nullable=False,
    )
    # safety_improvement_proposals
    op.alter_column(
        "safety_improvement_proposals",
        "kind",
        existing_type=sa.String(length=11),
        type_=sa.String(length=64),
        existing_nullable=False,
    )
    op.alter_column(
        "safety_improvement_proposals",
        "status",
        existing_type=sa.String(length=18),
        type_=sa.String(length=64),
        existing_nullable=False,
    )
    # credential_expiry_alerts
    op.alter_column(
        "credential_expiry_alerts",
        "level",
        existing_type=sa.String(length=7),
        type_=sa.String(length=64),
        existing_nullable=False,
    )
    op.alter_column(
        "credential_expiry_alerts",
        "status",
        existing_type=sa.String(length=8),
        type_=sa.String(length=64),
        existing_nullable=False,
    )
    # data_profiles
    op.alter_column(
        "data_profiles",
        "endpoint_role",
        existing_type=sa.String(length=6),
        type_=sa.String(length=64),
        existing_nullable=False,
    )
    op.alter_column(
        "data_profiles",
        "status",
        existing_type=sa.String(length=7),
        type_=sa.String(length=64),
        existing_nullable=False,
    )
    # pipeline_versions
    op.alter_column(
        "pipeline_versions",
        "status",
        existing_type=sa.String(length=16),
        type_=sa.String(length=64),
        existing_nullable=False,
    )
    # safety_releases
    op.alter_column(
        "safety_releases",
        "status",
        existing_type=sa.String(length=7),
        type_=sa.String(length=64),
        existing_nullable=False,
    )
    op.alter_column(
        "safety_releases",
        "scope",
        existing_type=sa.String(length=6),
        type_=sa.String(length=64),
        existing_nullable=False,
    )
    op.alter_column(
        "safety_releases",
        "gate_conclusion",
        existing_type=sa.String(length=6),
        type_=sa.String(length=64),
        existing_nullable=True,
    )
    # secret_migration_requests
    op.alter_column(
        "secret_migration_requests",
        "status",
        existing_type=sa.String(length=9),
        type_=sa.String(length=64),
        existing_nullable=False,
    )
    # secret_rotation_requests
    op.alter_column(
        "secret_rotation_requests",
        "status",
        existing_type=sa.String(length=16),
        type_=sa.String(length=64),
        existing_nullable=False,
    )
    # agent_runs
    op.alter_column(
        "agent_runs",
        "status",
        existing_type=sa.String(length=16),
        type_=sa.String(length=64),
        existing_nullable=False,
    )
    # approval_requests
    op.alter_column(
        "approval_requests",
        "status",
        existing_type=sa.String(length=8),
        type_=sa.String(length=64),
        existing_nullable=False,
    )
    op.alter_column(
        "approval_requests",
        "decision",
        existing_type=sa.String(length=7),
        type_=sa.String(length=64),
        existing_nullable=True,
    )
    # credential_alert_deliveries
    op.alter_column(
        "credential_alert_deliveries",
        "status",
        existing_type=sa.String(length=9),
        type_=sa.String(length=64),
        existing_nullable=False,
    )
    # execution_preparations
    op.alter_column(
        "execution_preparations",
        "status",
        existing_type=sa.String(length=16),
        type_=sa.String(length=64),
        existing_nullable=False,
    )
    op.alter_column(
        "execution_preparations",
        "risk_level",
        existing_type=sa.String(length=2),
        type_=sa.String(length=64),
        existing_nullable=False,
    )
    # pipeline_schedules
    op.alter_column(
        "pipeline_schedules",
        "mode",
        existing_type=sa.String(length=21),
        type_=sa.String(length=64),
        existing_nullable=False,
    )
    op.alter_column(
        "pipeline_schedules",
        "misfire_policy",
        existing_type=sa.String(length=9),
        type_=sa.String(length=64),
        existing_nullable=False,
    )
    # secret_destruction_requests
    op.alter_column(
        "secret_destruction_requests",
        "target",
        existing_type=sa.String(length=18),
        type_=sa.String(length=64),
        existing_nullable=False,
    )
    op.alter_column(
        "secret_destruction_requests",
        "status",
        existing_type=sa.String(length=9),
        type_=sa.String(length=64),
        existing_nullable=False,
    )
    # secret_rotation_rollback_requests
    op.alter_column(
        "secret_rotation_rollback_requests",
        "status",
        existing_type=sa.String(length=9),
        type_=sa.String(length=64),
        existing_nullable=False,
    )
    # execution_runs
    op.alter_column(
        "execution_runs",
        "status",
        existing_type=sa.String(length=10),
        type_=sa.String(length=64),
        existing_nullable=False,
    )
    op.alter_column(
        "execution_runs",
        "failure_category",
        existing_type=sa.String(length=13),
        type_=sa.String(length=64),
        existing_nullable=True,
    )
    op.alter_column(
        "execution_runs",
        "failure_stage",
        existing_type=sa.String(length=12),
        type_=sa.String(length=64),
        existing_nullable=True,
    )
    op.alter_column(
        "execution_runs",
        "supervision_action",
        existing_type=sa.String(length=10),
        type_=sa.String(length=64),
        existing_nullable=True,
    )
    op.alter_column(
        "execution_runs",
        "trigger",
        existing_type=sa.String(length=9),
        type_=sa.String(length=64),
        existing_nullable=False,
    )
    # harness_approval_decisions
    op.alter_column(
        "harness_approval_decisions",
        "role",
        existing_type=sa.String(length=17),
        type_=sa.String(length=64),
        existing_nullable=False,
    )
    op.alter_column(
        "harness_approval_decisions",
        "decision",
        existing_type=sa.String(length=7),
        type_=sa.String(length=64),
        existing_nullable=False,
    )
    # harness_command_outbox
    op.alter_column(
        "harness_command_outbox",
        "status",
        existing_type=sa.String(length=11),
        type_=sa.String(length=64),
        existing_nullable=False,
    )
    # quality_reports
    op.alter_column(
        "quality_reports",
        "status",
        existing_type=sa.String(length=10),
        type_=sa.String(length=64),
        existing_nullable=False,
    )
    # rollback_requests
    op.alter_column(
        "rollback_requests",
        "status",
        existing_type=sa.String(length=9),
        type_=sa.String(length=64),
        existing_nullable=False,
    )
    # runtime_supervision_snapshots
    op.alter_column(
        "runtime_supervision_snapshots",
        "action",
        existing_type=sa.String(length=10),
        type_=sa.String(length=64),
        existing_nullable=False,
    )
    op.alter_column(
        "runtime_supervision_snapshots",
        "risk_level",
        existing_type=sa.String(length=2),
        type_=sa.String(length=64),
        existing_nullable=False,
    )
    op.alter_column(
        "runtime_supervision_snapshots",
        "mitigation_status",
        existing_type=sa.String(length=7),
        type_=sa.String(length=64),
        existing_nullable=False,
    )
    # rollback_cleanup_requests
    op.alter_column(
        "rollback_cleanup_requests",
        "status",
        existing_type=sa.String(length=9),
        type_=sa.String(length=64),
        existing_nullable=False,
    )


def downgrade() -> None:
    """收窄回原始长度（存在更长值时会失败，而不是截断）。"""
    # audit_events
    op.alter_column(
        "audit_events",
        "event_type",
        existing_type=sa.String(length=64),
        type_=sa.String(length=19),
        existing_nullable=False,
    )
    # benchmark_runs
    op.alter_column(
        "benchmark_runs",
        "status",
        existing_type=sa.String(length=64),
        type_=sa.String(length=9),
        existing_nullable=False,
    )
    op.alter_column(
        "benchmark_runs",
        "gate_conclusion",
        existing_type=sa.String(length=64),
        type_=sa.String(length=6),
        existing_nullable=True,
    )
    # harness_policy_decisions
    op.alter_column(
        "harness_policy_decisions",
        "outcome",
        existing_type=sa.String(length=64),
        type_=sa.String(length=16),
        existing_nullable=False,
    )
    op.alter_column(
        "harness_policy_decisions",
        "risk_level",
        existing_type=sa.String(length=64),
        type_=sa.String(length=2),
        existing_nullable=False,
    )
    # harness_side_effect_preparations
    op.alter_column(
        "harness_side_effect_preparations",
        "operation",
        existing_type=sa.String(length=64),
        type_=sa.String(length=24),
        existing_nullable=False,
    )
    op.alter_column(
        "harness_side_effect_preparations",
        "status",
        existing_type=sa.String(length=64),
        type_=sa.String(length=16),
        existing_nullable=False,
    )
    op.alter_column(
        "harness_side_effect_preparations",
        "risk_level",
        existing_type=sa.String(length=64),
        type_=sa.String(length=2),
        existing_nullable=False,
    )
    op.alter_column(
        "harness_side_effect_preparations",
        "execution_status",
        existing_type=sa.String(length=64),
        type_=sa.String(length=7),
        existing_nullable=False,
    )
    # projects
    op.alter_column(
        "projects",
        "status",
        existing_type=sa.String(length=64),
        type_=sa.String(length=8),
        existing_nullable=False,
    )
    # users
    op.alter_column(
        "users",
        "status",
        existing_type=sa.String(length=64),
        type_=sa.String(length=8),
        existing_nullable=False,
    )
    # data_connections
    op.alter_column(
        "data_connections",
        "connector_type",
        existing_type=sa.String(length=64),
        type_=sa.String(length=10),
        existing_nullable=False,
    )
    op.alter_column(
        "data_connections",
        "endpoint_role",
        existing_type=sa.String(length=64),
        type_=sa.String(length=6),
        existing_nullable=False,
    )
    op.alter_column(
        "data_connections",
        "status",
        existing_type=sa.String(length=64),
        type_=sa.String(length=8),
        existing_nullable=False,
    )
    # file_assets
    op.alter_column(
        "file_assets",
        "format",
        existing_type=sa.String(length=64),
        type_=sa.String(length=8),
        existing_nullable=False,
    )
    op.alter_column(
        "file_assets",
        "endpoint_role",
        existing_type=sa.String(length=64),
        type_=sa.String(length=6),
        existing_nullable=False,
    )
    # harness_evidence_events
    op.alter_column(
        "harness_evidence_events",
        "risk_level",
        existing_type=sa.String(length=64),
        type_=sa.String(length=2),
        existing_nullable=False,
    )
    # harness_side_effect_approvals
    op.alter_column(
        "harness_side_effect_approvals",
        "role",
        existing_type=sa.String(length=64),
        type_=sa.String(length=17),
        existing_nullable=False,
    )
    op.alter_column(
        "harness_side_effect_approvals",
        "decision",
        existing_type=sa.String(length=64),
        type_=sa.String(length=7),
        existing_nullable=False,
    )
    # project_memberships
    op.alter_column(
        "project_memberships",
        "role",
        existing_type=sa.String(length=64),
        type_=sa.String(length=13),
        existing_nullable=False,
    )
    # project_role_grants
    op.alter_column(
        "project_role_grants",
        "role",
        existing_type=sa.String(length=64),
        type_=sa.String(length=17),
        existing_nullable=False,
    )
    # safety_improvement_proposals
    op.alter_column(
        "safety_improvement_proposals",
        "kind",
        existing_type=sa.String(length=64),
        type_=sa.String(length=11),
        existing_nullable=False,
    )
    op.alter_column(
        "safety_improvement_proposals",
        "status",
        existing_type=sa.String(length=64),
        type_=sa.String(length=18),
        existing_nullable=False,
    )
    # credential_expiry_alerts
    op.alter_column(
        "credential_expiry_alerts",
        "level",
        existing_type=sa.String(length=64),
        type_=sa.String(length=7),
        existing_nullable=False,
    )
    op.alter_column(
        "credential_expiry_alerts",
        "status",
        existing_type=sa.String(length=64),
        type_=sa.String(length=8),
        existing_nullable=False,
    )
    # data_profiles
    op.alter_column(
        "data_profiles",
        "endpoint_role",
        existing_type=sa.String(length=64),
        type_=sa.String(length=6),
        existing_nullable=False,
    )
    op.alter_column(
        "data_profiles",
        "status",
        existing_type=sa.String(length=64),
        type_=sa.String(length=7),
        existing_nullable=False,
    )
    # pipeline_versions
    op.alter_column(
        "pipeline_versions",
        "status",
        existing_type=sa.String(length=64),
        type_=sa.String(length=16),
        existing_nullable=False,
    )
    # safety_releases
    op.alter_column(
        "safety_releases",
        "status",
        existing_type=sa.String(length=64),
        type_=sa.String(length=7),
        existing_nullable=False,
    )
    op.alter_column(
        "safety_releases",
        "scope",
        existing_type=sa.String(length=64),
        type_=sa.String(length=6),
        existing_nullable=False,
    )
    op.alter_column(
        "safety_releases",
        "gate_conclusion",
        existing_type=sa.String(length=64),
        type_=sa.String(length=6),
        existing_nullable=True,
    )
    # secret_migration_requests
    op.alter_column(
        "secret_migration_requests",
        "status",
        existing_type=sa.String(length=64),
        type_=sa.String(length=9),
        existing_nullable=False,
    )
    # secret_rotation_requests
    op.alter_column(
        "secret_rotation_requests",
        "status",
        existing_type=sa.String(length=64),
        type_=sa.String(length=16),
        existing_nullable=False,
    )
    # agent_runs
    op.alter_column(
        "agent_runs",
        "status",
        existing_type=sa.String(length=64),
        type_=sa.String(length=16),
        existing_nullable=False,
    )
    # approval_requests
    op.alter_column(
        "approval_requests",
        "status",
        existing_type=sa.String(length=64),
        type_=sa.String(length=8),
        existing_nullable=False,
    )
    op.alter_column(
        "approval_requests",
        "decision",
        existing_type=sa.String(length=64),
        type_=sa.String(length=7),
        existing_nullable=True,
    )
    # credential_alert_deliveries
    op.alter_column(
        "credential_alert_deliveries",
        "status",
        existing_type=sa.String(length=64),
        type_=sa.String(length=9),
        existing_nullable=False,
    )
    # execution_preparations
    op.alter_column(
        "execution_preparations",
        "status",
        existing_type=sa.String(length=64),
        type_=sa.String(length=16),
        existing_nullable=False,
    )
    op.alter_column(
        "execution_preparations",
        "risk_level",
        existing_type=sa.String(length=64),
        type_=sa.String(length=2),
        existing_nullable=False,
    )
    # pipeline_schedules
    op.alter_column(
        "pipeline_schedules",
        "mode",
        existing_type=sa.String(length=64),
        type_=sa.String(length=21),
        existing_nullable=False,
    )
    op.alter_column(
        "pipeline_schedules",
        "misfire_policy",
        existing_type=sa.String(length=64),
        type_=sa.String(length=9),
        existing_nullable=False,
    )
    # secret_destruction_requests
    op.alter_column(
        "secret_destruction_requests",
        "target",
        existing_type=sa.String(length=64),
        type_=sa.String(length=18),
        existing_nullable=False,
    )
    op.alter_column(
        "secret_destruction_requests",
        "status",
        existing_type=sa.String(length=64),
        type_=sa.String(length=9),
        existing_nullable=False,
    )
    # secret_rotation_rollback_requests
    op.alter_column(
        "secret_rotation_rollback_requests",
        "status",
        existing_type=sa.String(length=64),
        type_=sa.String(length=9),
        existing_nullable=False,
    )
    # execution_runs
    op.alter_column(
        "execution_runs",
        "status",
        existing_type=sa.String(length=64),
        type_=sa.String(length=10),
        existing_nullable=False,
    )
    op.alter_column(
        "execution_runs",
        "failure_category",
        existing_type=sa.String(length=64),
        type_=sa.String(length=13),
        existing_nullable=True,
    )
    op.alter_column(
        "execution_runs",
        "failure_stage",
        existing_type=sa.String(length=64),
        type_=sa.String(length=12),
        existing_nullable=True,
    )
    op.alter_column(
        "execution_runs",
        "supervision_action",
        existing_type=sa.String(length=64),
        type_=sa.String(length=10),
        existing_nullable=True,
    )
    op.alter_column(
        "execution_runs",
        "trigger",
        existing_type=sa.String(length=64),
        type_=sa.String(length=9),
        existing_nullable=False,
    )
    # harness_approval_decisions
    op.alter_column(
        "harness_approval_decisions",
        "role",
        existing_type=sa.String(length=64),
        type_=sa.String(length=17),
        existing_nullable=False,
    )
    op.alter_column(
        "harness_approval_decisions",
        "decision",
        existing_type=sa.String(length=64),
        type_=sa.String(length=7),
        existing_nullable=False,
    )
    # harness_command_outbox
    op.alter_column(
        "harness_command_outbox",
        "status",
        existing_type=sa.String(length=64),
        type_=sa.String(length=11),
        existing_nullable=False,
    )
    # quality_reports
    op.alter_column(
        "quality_reports",
        "status",
        existing_type=sa.String(length=64),
        type_=sa.String(length=10),
        existing_nullable=False,
    )
    # rollback_requests
    op.alter_column(
        "rollback_requests",
        "status",
        existing_type=sa.String(length=64),
        type_=sa.String(length=9),
        existing_nullable=False,
    )
    # runtime_supervision_snapshots
    op.alter_column(
        "runtime_supervision_snapshots",
        "action",
        existing_type=sa.String(length=64),
        type_=sa.String(length=10),
        existing_nullable=False,
    )
    op.alter_column(
        "runtime_supervision_snapshots",
        "risk_level",
        existing_type=sa.String(length=64),
        type_=sa.String(length=2),
        existing_nullable=False,
    )
    op.alter_column(
        "runtime_supervision_snapshots",
        "mitigation_status",
        existing_type=sa.String(length=64),
        type_=sa.String(length=7),
        existing_nullable=False,
    )
    # rollback_cleanup_requests
    op.alter_column(
        "rollback_cleanup_requests",
        "status",
        existing_type=sa.String(length=64),
        type_=sa.String(length=9),
        existing_nullable=False,
    )
