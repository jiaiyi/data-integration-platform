"""控制面 ORM 模型：41 张表（PRD §8.2 分组 / SPEC §5.2 表清单）。

约定：
- 枚举一律 `native_enum=False` 以字符串保存，避免 PostgreSQL 原生枚举的升级回滚困难；
- 幂等靠 `idempotency_key` / `token_sha256` 的 unique 约束，乐观锁靠 `lock_version`；
- 时间统一带时区的 UTC；审计只记变化摘要，不记秘密。
"""

import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    DateTime,
    Enum,
    Float,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    String,
    Text,
    UniqueConstraint,
    Uuid,
)
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func

from data_integration_platform.domain.benchmark import (
    BenchmarkGateConclusion,
    BenchmarkRunStatus,
)
from data_integration_platform.domain.enums import (
    AgentRunStatus,
    ApprovalDecisionValue,
    ApprovalStatus,
    AuditEventType,
    CleanupStatus,
    ConnectionStatus,
    ConnectorType,
    CredentialAlertDeliveryStatus,
    CredentialAlertLevel,
    CredentialAlertStatus,
    DataEndpointRole,
    DataProfileStatus,
    ExecutionStatus,
    ExecutionTrigger,
    FailureCategory,
    FailureStage,
    FileAssetFormat,
    PipelineVersionStatus,
    ProjectRole,
    ProjectStatus,
    QualityStatus,
    RollbackStatus,
    ScheduleMisfirePolicy,
    ScheduleMode,
    SecretDestructionStatus,
    SecretDestructionTarget,
    SecretMigrationStatus,
    SecretRotationRollbackStatus,
    SecretRotationStatus,
    UserStatus,
)
from data_integration_platform.domain.harness import (
    ExecutionPreparationStatus,
    HarnessApprovalValue,
    HarnessOutboxStatus,
    HarnessRole,
    RiskLevel,
    ToolIntentOutcome,
)
from data_integration_platform.domain.safety_evolution import (
    SafetyProposalKind,
    SafetyProposalStatus,
    SafetyReleaseScope,
    SafetyReleaseStatus,
)
from data_integration_platform.domain.side_effects import (
    SideEffectExecutionStatus,
    SideEffectOperation,
)
from data_integration_platform.domain.supervision import (
    RuntimeMitigationStatus,
    SupervisionAction,
)
from data_integration_platform.infrastructure.database import Base

# 枚举列的固定长度。
# 不指定时 SQLAlchemy 会按「创建时枚举值的最长值」生成 VARCHAR(n)，于是**新增一个更长的
# 枚举值就会在 PostgreSQL 上插入失败**（SQLite 不校验长度，所以只在真库上暴露）。
# 固定成宽裕长度后，枚举增长是纯代码变更，不再需要配套迁移。
ENUM_COLUMN_LENGTH = 64


def enum_column(enum_type: type[Any]) -> Enum:
    """把领域枚举保存为非原生字符串枚举，降低 PostgreSQL 枚举升级风险。"""
    return Enum(
        enum_type,
        native_enum=False,
        validate_strings=True,
        length=ENUM_COLUMN_LENGTH,
    )


def _utcnow() -> datetime:
    """UTC 时间戳的 Python 侧默认值。"""
    return datetime.now(UTC)


class TimestampMixin:
    """统一时间戳。

    同时声明 Python 侧 `default` 与数据库侧 `server_default`：前者保证
    `flush()` 之后属性已加载，异步会话下序列化刚创建的对象不会触发隐式 IO
    （MissingGreenlet）；后者保证绕过 ORM 的写入仍有值。
    """

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=_utcnow,
        server_default=func.now(),
        nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=_utcnow,
        server_default=func.now(),
        onupdate=_utcnow,
        nullable=False,
    )


# ============================================================ A. 身份与项目治理
class User(Base, TimestampMixin):
    __tablename__ = "users"

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    subject: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)
    display_name: Mapped[str] = mapped_column(String(255), nullable=False)
    status: Mapped[UserStatus] = mapped_column(
        enum_column(UserStatus), nullable=False, default=UserStatus.ACTIVE
    )


class Project(Base, TimestampMixin):
    __tablename__ = "projects"

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)
    status: Mapped[ProjectStatus] = mapped_column(
        enum_column(ProjectStatus), nullable=False, default=ProjectStatus.ACTIVE
    )


class ProjectMembership(Base, TimestampMixin):
    """项目成员角色：授予「能碰什么资源」。"""

    __tablename__ = "project_memberships"
    __table_args__ = (UniqueConstraint("project_id", "user_id", name="uq_project_memberships"),)

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    project_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("projects.id", ondelete="CASCADE"), nullable=False
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    role: Mapped[ProjectRole] = mapped_column(enum_column(ProjectRole), nullable=False)


class ProjectRoleGrant(Base, TimestampMixin):
    """Harness 职责槽授予：授予「能批准/执行什么动作」，不会隐式升级为安全审批人。"""

    __tablename__ = "project_role_grants"
    __table_args__ = (
        UniqueConstraint("project_id", "user_id", "role", name="uq_project_role_grants_identity"),
        Index("ix_project_role_grants_project_user", "project_id", "user_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    project_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("projects.id", ondelete="CASCADE"), nullable=False
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    role: Mapped[HarnessRole] = mapped_column(enum_column(HarnessRole), nullable=False)


# ========================================================== B. 连接、Profile 与资产
class DataConnection(Base, TimestampMixin):
    """连接登记。凭据只保存 `secret_ref` 引用，明文永不进入本表与任何响应。"""

    __tablename__ = "data_connections"
    __table_args__ = (
        UniqueConstraint("project_id", "name", name="uq_data_connections_project_name"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    project_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("projects.id", ondelete="CASCADE"), nullable=False
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    connector_type: Mapped[ConnectorType] = mapped_column(
        enum_column(ConnectorType), nullable=False
    )
    endpoint_role: Mapped[DataEndpointRole] = mapped_column(
        enum_column(DataEndpointRole), nullable=False
    )
    status: Mapped[ConnectionStatus] = mapped_column(
        enum_column(ConnectionStatus), nullable=False, default=ConnectionStatus.UNTESTED
    )
    host: Mapped[str | None] = mapped_column(String(255))
    port: Mapped[int | None] = mapped_column(Integer)
    database_name: Mapped[str | None] = mapped_column(String(255))
    options: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    secret_ref: Mapped[str | None] = mapped_column(String(512))
    last_tested_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_test_error: Mapped[str | None] = mapped_column(Text)
    credential_rotation_enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False
    )
    credential_max_age_days: Mapped[int | None] = mapped_column(Integer)
    credential_warning_days: Mapped[int | None] = mapped_column(Integer)
    credential_issued_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_by: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True))


class DataProfile(Base, TimestampMixin):
    """受管元数据事实，**有时效**：过期或非就绪的 Profile 不可用于生成。"""

    __tablename__ = "data_profiles"
    __table_args__ = (Index("ix_data_profiles_connection", "connection_id", "status"),)

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    connection_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("data_connections.id", ondelete="CASCADE"), nullable=False
    )
    schema_name: Mapped[str] = mapped_column(String(255), nullable=False)
    table_name: Mapped[str] = mapped_column(String(255), nullable=False)
    endpoint_role: Mapped[DataEndpointRole] = mapped_column(
        enum_column(DataEndpointRole), nullable=False
    )
    status: Mapped[DataProfileStatus] = mapped_column(
        enum_column(DataProfileStatus), nullable=False, default=DataProfileStatus.PENDING
    )
    columns: Mapped[list[dict[str, Any]]] = mapped_column(JSON, nullable=False, default=list)
    primary_keys: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    statistics: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    masked_sample: Mapped[list[dict[str, Any]]] = mapped_column(JSON, nullable=False, default=list)
    sample_size: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error: Mapped[str | None] = mapped_column(Text)
    requested_by: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True))


class FileAsset(Base, TimestampMixin):
    """受管文件资产；单文件大小与行数上限见 config.file_asset_*。

    `execution_run_id` 非空表示这份资产是**某次执行的交付物**（T-40）。
    用户上传的资产留空。加这一列而不是靠文件名约定：交付要能
    「按执行查」并且**重复交付只能是同一份**，靠名字猜既查不准也拦不住重复。
    """

    __tablename__ = "file_assets"
    __table_args__ = (UniqueConstraint("execution_run_id", "name", name="uq_file_assets_run_name"),)

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    project_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("projects.id", ondelete="CASCADE"), nullable=False
    )
    execution_run_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("execution_runs.id", ondelete="SET NULL")
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    format: Mapped[FileAssetFormat] = mapped_column(enum_column(FileAssetFormat), nullable=False)
    endpoint_role: Mapped[DataEndpointRole] = mapped_column(
        enum_column(DataEndpointRole), nullable=False, default=DataEndpointRole.SOURCE
    )
    size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    row_count: Mapped[int | None] = mapped_column(BigInteger)
    storage_ref: Mapped[str] = mapped_column(String(1024), nullable=False)
    checksum_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    created_by: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True))


# ============================================================ C. Pipeline 与版本
class Pipeline(Base, TimestampMixin):
    __tablename__ = "pipelines"
    __table_args__ = (UniqueConstraint("project_id", "name", name="uq_pipelines_project_name"),)

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    project_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("projects.id", ondelete="CASCADE"), nullable=False
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)


class PipelineVersion(Base, TimestampMixin):
    """不可变版本；需求变更创建 V+1，不覆盖旧版本（FR-39）。"""

    __tablename__ = "pipeline_versions"
    __table_args__ = (
        UniqueConstraint("pipeline_id", "version_number", name="uq_pipeline_versions_number"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    pipeline_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("pipelines.id", ondelete="CASCADE"), nullable=False
    )
    version_number: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[PipelineVersionStatus] = mapped_column(
        enum_column(PipelineVersionStatus), nullable=False, default=PipelineVersionStatus.DRAFT
    )
    requirement: Mapped[str] = mapped_column(Text, nullable=False)
    parent_version_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("pipeline_versions.id", ondelete="SET NULL")
    )
    plan: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    artifact_sha256: Mapped[str | None] = mapped_column(String(64))
    lock_version: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_by: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)


class PipelineArtifact(Base, TimestampMixin):
    """冻结制品：HOCON / EtlPlan / 审查视图。"""

    __tablename__ = "pipeline_artifacts"
    __table_args__ = (
        UniqueConstraint("version_id", "artifact_type", name="uq_pipeline_artifacts_type"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    version_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("pipeline_versions.id", ondelete="CASCADE"), nullable=False
    )
    artifact_type: Mapped[str] = mapped_column(String(64), nullable=False)
    storage_ref: Mapped[str] = mapped_column(String(1024), nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    size_bytes: Mapped[int | None] = mapped_column(BigInteger)


# ================================================================ D. 生成与调度
class AgentRun(Base, TimestampMixin):
    """一次生成的可观测事实；候选与修复计数共享同一预算（FR-31）。"""

    __tablename__ = "agent_runs"
    __table_args__ = (Index("ix_agent_runs_version", "version_id", "status"),)

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    version_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("pipeline_versions.id", ondelete="CASCADE"), nullable=False
    )
    status: Mapped[AgentRunStatus] = mapped_column(
        enum_column(AgentRunStatus), nullable=False, default=AgentRunStatus.QUEUED
    )
    thread_id: Mapped[str] = mapped_column(String(255), nullable=False)
    generation_phase: Mapped[str | None] = mapped_column(String(64))
    generation_backend: Mapped[str | None] = mapped_column(String(64))
    generation_model: Mapped[str | None] = mapped_column(String(255))
    profile_ids: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    quality_contract: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    incremental_contract: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    row_llm_contract: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    pending_question: Mapped[str | None] = mapped_column(Text)
    clarification_rounds: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    generation_candidates: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    generation_repairs: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    generation_provider_calls: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    generation_elapsed_ms: Mapped[int | None] = mapped_column(Integer)
    generation_diagnostics: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    error: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_by: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)


class PipelineSchedule(Base, TimestampMixin):
    """调度配置；触发时刻与水位窗口随版本冻结。"""

    __tablename__ = "pipeline_schedules"
    __table_args__ = (UniqueConstraint("version_id", name="uq_pipeline_schedules_version"),)

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    version_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("pipeline_versions.id", ondelete="CASCADE"), nullable=False
    )
    mode: Mapped[ScheduleMode] = mapped_column(enum_column(ScheduleMode), nullable=False)
    cron_expression: Mapped[str] = mapped_column(String(128), nullable=False)
    timezone: Mapped[str] = mapped_column(String(64), nullable=False, default="UTC")
    misfire_policy: Mapped[ScheduleMisfirePolicy] = mapped_column(
        enum_column(ScheduleMisfirePolicy), nullable=False, default=ScheduleMisfirePolicy.SKIP
    )
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    watermark_column: Mapped[str | None] = mapped_column(String(255))
    watermark_window_start: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    watermark_window_end: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    next_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    lock_version: Mapped[int] = mapped_column(Integer, nullable=False, default=0)


# ================================================================ E. 版本审批
class ApprovalRequest(Base, TimestampMixin):
    """版本审批；绑定 Artifact 摘要，内容变化即审批失效（SOD-04）。"""

    __tablename__ = "approval_requests"
    __table_args__ = (Index("ix_approval_requests_version", "version_id", "status"),)

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    version_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("pipeline_versions.id", ondelete="CASCADE"), nullable=False
    )
    status: Mapped[ApprovalStatus] = mapped_column(
        enum_column(ApprovalStatus), nullable=False, default=ApprovalStatus.PENDING
    )
    artifact_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    decision: Mapped[ApprovalDecisionValue | None] = mapped_column(
        enum_column(ApprovalDecisionValue)
    )
    decided_by: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True))
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    reason: Mapped[str | None] = mapped_column(Text)
    requested_by: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)


# ============================================================ F. Harness 安全内核
class HarnessPolicyDecision(Base, TimestampMixin):
    """PDP 决策快照：策略包版本与规则命中随决策一并留存，可追溯。"""

    __tablename__ = "harness_policy_decisions"

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tool_name: Mapped[str] = mapped_column(String(128), nullable=False)
    tool_version: Mapped[str] = mapped_column(String(64), nullable=False)
    outcome: Mapped[ToolIntentOutcome] = mapped_column(
        enum_column(ToolIntentOutcome), nullable=False
    )
    risk_level: Mapped[RiskLevel] = mapped_column(enum_column(RiskLevel), nullable=False)
    policy_bundle_version: Mapped[str] = mapped_column(String(128), nullable=False)
    request_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    resource_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    matched_rules: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    required_roles: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    required_approvals: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    subject_id: Mapped[str] = mapped_column(String(64), nullable=False)
    environment: Mapped[str] = mapped_column(String(64), nullable=False)
    reason: Mapped[str | None] = mapped_column(Text)


class ExecutionPreparation(Base, TimestampMixin):
    """准备单：冻结输入/资源/制品指纹与预算，**不产生任何外部副作用**。"""

    __tablename__ = "execution_preparations"
    __table_args__ = (
        UniqueConstraint("idempotency_key", name="uq_execution_preparations_idempotency"),
        Index("ix_execution_preparations_version", "pipeline_version_id", "status"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    policy_decision_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("harness_policy_decisions.id", ondelete="SET NULL")
    )
    pipeline_version_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("pipeline_versions.id", ondelete="CASCADE"), nullable=False
    )
    status: Mapped[ExecutionPreparationStatus] = mapped_column(
        enum_column(ExecutionPreparationStatus),
        nullable=False,
        default=ExecutionPreparationStatus.WAITING_APPROVAL,
    )
    risk_level: Mapped[RiskLevel] = mapped_column(enum_column(RiskLevel), nullable=False)
    policy_bundle_version: Mapped[str] = mapped_column(String(128), nullable=False)
    environment: Mapped[str] = mapped_column(String(64), nullable=False)
    tool_name: Mapped[str] = mapped_column(String(128), nullable=False)
    tool_version: Mapped[str] = mapped_column(String(64), nullable=False)
    artifact_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    input_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    resource_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    supervision_contract_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    data_classification_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    impact_report: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    rollback_plan: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    required_roles: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    required_approvals: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    idempotency_key: Mapped[str] = mapped_column(String(128), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    committed_by: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True))
    committed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    lock_version: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_by: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)


class HarnessApprovalDecision(Base, TimestampMixin):
    """具名职责审批；一名审批人对一个准备单最多占一个槽位（SOD-02）。"""

    __tablename__ = "harness_approval_decisions"
    __table_args__ = (UniqueConstraint("preparation_id", "role", name="uq_harness_approvals_slot"),)

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    preparation_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("execution_preparations.id", ondelete="CASCADE"),
        nullable=False,
    )
    approver_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    role: Mapped[HarnessRole] = mapped_column(enum_column(HarnessRole), nullable=False)
    decision: Mapped[HarnessApprovalValue] = mapped_column(
        enum_column(HarnessApprovalValue), nullable=False
    )
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    decided_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class HarnessCapabilityGrant(Base, TimestampMixin):
    """单次能力令牌；`consumed_at` 置位即代表已消费，第二次使用必失败（FR-46）。"""

    __tablename__ = "harness_capability_grants"
    __table_args__ = (UniqueConstraint("token_sha256", name="uq_harness_capability_token"),)

    token_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    preparation_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("execution_preparations.id", ondelete="CASCADE"),
        nullable=False,
    )
    token_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    key_id: Mapped[str] = mapped_column(String(128), nullable=False)
    claims: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    claims_signature_algorithm: Mapped[str] = mapped_column(String(32), nullable=False)
    claims_signature_value: Mapped[str] = mapped_column(Text, nullable=False)
    subject_id: Mapped[str] = mapped_column(String(64), nullable=False)
    environment: Mapped[str] = mapped_column(String(64), nullable=False)
    issued_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    single_use: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    consumed_by: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True))
    execution_run_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True))


class HarnessCommandOutbox(Base, TimestampMixin):
    """事务 Outbox：与执行记录在同一事务写入，保证不产生「无本地事实的远端作业」。"""

    __tablename__ = "harness_command_outbox"
    __table_args__ = (Index("ix_harness_command_outbox_status", "status", "created_at"),)

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    preparation_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("execution_preparations.id", ondelete="CASCADE"),
        nullable=False,
    )
    command_payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    payload_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[HarnessOutboxStatus] = mapped_column(
        enum_column(HarnessOutboxStatus), nullable=False, default=HarnessOutboxStatus.PENDING
    )
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    lease_owner: Mapped[str | None] = mapped_column(String(255))
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(Text)
    dispatched_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class HarnessEvidenceStream(Base, TimestampMixin):
    """证据流；`head_hash` 指向链尾，便于快速校验与追加。"""

    __tablename__ = "harness_evidence_streams"
    __table_args__ = (
        UniqueConstraint("aggregate_type", "aggregate_id", name="uq_harness_evidence_streams"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    aggregate_type: Mapped[str] = mapped_column(String(64), nullable=False)
    aggregate_id: Mapped[str] = mapped_column(String(128), nullable=False)
    head_hash: Mapped[str | None] = mapped_column(String(64))
    sequence: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    key_id: Mapped[str | None] = mapped_column(String(128))


class HarnessEvidenceEvent(Base, TimestampMixin):
    """追加式哈希链事件；改写 `payload` 即断链，删除即缺号，均可被完整性验证发现。"""

    __tablename__ = "harness_evidence_events"
    __table_args__ = (
        UniqueConstraint("stream_id", "sequence", name="uq_harness_evidence_events_seq"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    stream_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("harness_evidence_streams.id", ondelete="CASCADE"),
        nullable=False,
    )
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    event_type: Mapped[str] = mapped_column(String(64), nullable=False)
    risk_level: Mapped[RiskLevel] = mapped_column(enum_column(RiskLevel), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    payload_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    previous_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    event_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    signature_algorithm: Mapped[str] = mapped_column(String(32), nullable=False)
    signature_key_id: Mapped[str] = mapped_column(String(128), nullable=False)
    signature_value: Mapped[str] = mapped_column(Text, nullable=False)
    trace_id: Mapped[str | None] = mapped_column(String(32))


class HarnessSideEffectPreparation(Base, TimestampMixin):
    """通用副作用准备单：九种操作共用一套「申请—审批—提交」入口（FR-57）。"""

    __tablename__ = "harness_side_effect_preparations"
    __table_args__ = (
        UniqueConstraint("idempotency_key", name="uq_side_effect_preparations_idempotency"),
        Index("ix_side_effect_preparations_aggregate", "aggregate_type", "aggregate_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    operation: Mapped[SideEffectOperation] = mapped_column(
        enum_column(SideEffectOperation), nullable=False
    )
    project_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    aggregate_type: Mapped[str] = mapped_column(String(64), nullable=False)
    aggregate_id: Mapped[str] = mapped_column(String(128), nullable=False)
    status: Mapped[ExecutionPreparationStatus] = mapped_column(
        enum_column(ExecutionPreparationStatus),
        nullable=False,
        default=ExecutionPreparationStatus.WAITING_APPROVAL,
    )
    risk_level: Mapped[RiskLevel] = mapped_column(enum_column(RiskLevel), nullable=False)
    policy_bundle_version: Mapped[str] = mapped_column(String(128), nullable=False)
    environment: Mapped[str] = mapped_column(String(64), nullable=False)
    tool_name: Mapped[str] = mapped_column(String(128), nullable=False)
    tool_version: Mapped[str] = mapped_column(String(64), nullable=False)
    fact_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    fact_summary: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    input_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    resource_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    impact_report: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    rollback_plan: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    required_roles: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    required_approvals: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    idempotency_key: Mapped[str] = mapped_column(String(128), nullable=False)
    execution_status: Mapped[SideEffectExecutionStatus] = mapped_column(
        enum_column(SideEffectExecutionStatus),
        nullable=False,
        default=SideEffectExecutionStatus.PENDING,
    )
    execution_lease_owner: Mapped[str | None] = mapped_column(String(255))
    execution_lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    committed_by: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True))
    committed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    failure_reason: Mapped[str | None] = mapped_column(Text)
    lock_version: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_by: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)


class HarnessSideEffectApproval(Base, TimestampMixin):
    __tablename__ = "harness_side_effect_approvals"
    __table_args__ = (
        UniqueConstraint("preparation_id", "role", name="uq_side_effect_approvals_slot"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    preparation_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("harness_side_effect_preparations.id", ondelete="CASCADE"),
        nullable=False,
    )
    approver_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    role: Mapped[HarnessRole] = mapped_column(enum_column(HarnessRole), nullable=False)
    decision: Mapped[HarnessApprovalValue] = mapped_column(
        enum_column(HarnessApprovalValue), nullable=False
    )
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    decided_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class HarnessSideEffectCapabilityGrant(Base, TimestampMixin):
    __tablename__ = "harness_side_effect_capability_grants"
    __table_args__ = (UniqueConstraint("token_sha256", name="uq_side_effect_capability_token"),)

    token_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    preparation_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("harness_side_effect_preparations.id", ondelete="CASCADE"),
        nullable=False,
    )
    token_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    key_id: Mapped[str] = mapped_column(String(128), nullable=False)
    claims: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    claims_signature_algorithm: Mapped[str] = mapped_column(String(32), nullable=False)
    claims_signature_value: Mapped[str] = mapped_column(Text, nullable=False)
    subject_id: Mapped[str] = mapped_column(String(64), nullable=False)
    environment: Mapped[str] = mapped_column(String(64), nullable=False)
    issued_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    single_use: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    consumed_by: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True))


# ============================================================ G. 执行与监督
class ExecutionRun(Base, TimestampMixin):
    """执行实例：控制面上最宽的运行事实表。"""

    __tablename__ = "execution_runs"
    __table_args__ = (
        UniqueConstraint("idempotency_key", name="uq_execution_runs_idempotency"),
        Index("ix_execution_runs_status", "status", "last_observed_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    pipeline_version_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("pipeline_versions.id", ondelete="CASCADE"), nullable=False
    )
    harness_preparation_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("execution_preparations.id", ondelete="CASCADE"),
        nullable=False,
    )
    status: Mapped[ExecutionStatus] = mapped_column(
        enum_column(ExecutionStatus), nullable=False, default=ExecutionStatus.QUEUED
    )
    engine_status: Mapped[str | None] = mapped_column(String(64))
    idempotency_key: Mapped[str] = mapped_column(String(128), nullable=False)
    seatunnel_job_id: Mapped[str | None] = mapped_column(String(128))
    submitted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_observed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    retry_of_run_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("execution_runs.id", ondelete="SET NULL")
    )
    attempt_number: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    cancel_requested_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    cancel_reason: Mapped[str | None] = mapped_column(Text)
    # 指标：引擎未提供时留空，不用 0 冒充（FR-61）。
    source_rows: Mapped[int | None] = mapped_column(BigInteger)
    sink_rows: Mapped[int | None] = mapped_column(BigInteger)
    source_bytes: Mapped[int | None] = mapped_column(BigInteger)
    sink_bytes: Mapped[int | None] = mapped_column(BigInteger)
    table_metrics: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    failure_category: Mapped[FailureCategory | None] = mapped_column(enum_column(FailureCategory))
    failure_stage: Mapped[FailureStage | None] = mapped_column(enum_column(FailureStage))
    retryable: Mapped[bool | None] = mapped_column(Boolean)
    diagnosis: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    supervision_action: Mapped[SupervisionAction | None] = mapped_column(
        enum_column(SupervisionAction)
    )
    supervision_contract_sha256: Mapped[str | None] = mapped_column(String(64))
    quarantined_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    quarantine_reason: Mapped[str | None] = mapped_column(Text)
    trigger: Mapped[ExecutionTrigger] = mapped_column(
        enum_column(ExecutionTrigger), nullable=False, default=ExecutionTrigger.MANUAL
    )
    schedule_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("pipeline_schedules.id", ondelete="SET NULL")
    )
    scheduled_for: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    watermark_window: Mapped[dict[str, Any] | None] = mapped_column(JSON)


class ExecutionEvent(Base, TimestampMixin):
    """执行事件时间线；只记事件类型与摘要载荷。"""

    __tablename__ = "execution_events"
    __table_args__ = (Index("ix_execution_events_run", "run_id", "occurred_at"),)

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    run_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("execution_runs.id", ondelete="CASCADE"), nullable=False
    )
    event_type: Mapped[str] = mapped_column(String(64), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class RuntimeSupervisionSnapshot(Base, TimestampMixin):
    """监督快照：只含聚合指标与规则命中，不含原始业务行（FR-82）。"""

    __tablename__ = "runtime_supervision_snapshots"
    __table_args__ = (Index("ix_supervision_snapshots_run", "run_id", "created_at"),)

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    run_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("execution_runs.id", ondelete="CASCADE"), nullable=False
    )
    observed_records: Mapped[int | None] = mapped_column(BigInteger)
    observed_bytes: Mapped[int | None] = mapped_column(BigInteger)
    elapsed_seconds: Mapped[float | None] = mapped_column(Float)
    output_ratio: Mapped[float | None] = mapped_column(Float)
    reject_rate: Mapped[float | None] = mapped_column(Float)
    action: Mapped[SupervisionAction] = mapped_column(
        enum_column(SupervisionAction), nullable=False
    )
    risk_level: Mapped[RiskLevel] = mapped_column(enum_column(RiskLevel), nullable=False)
    # 「阻断发布」与「外部取消」分开记录，不混为一谈（FR-80）。
    wants_cancel: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    blocks_publish: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    mitigation_status: Mapped[RuntimeMitigationStatus] = mapped_column(
        enum_column(RuntimeMitigationStatus),
        nullable=False,
        default=RuntimeMitigationStatus.PENDING,
    )
    matched_rules: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    contract_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class WorkerHeartbeat(Base, TimestampMixin):
    """Worker 存活与能力上报；超过 stale 阈值即判定失联。"""

    __tablename__ = "worker_heartbeats"
    __table_args__ = (UniqueConstraint("worker_id", name="uq_worker_heartbeats_worker"),)

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    worker_id: Mapped[str] = mapped_column(String(255), nullable=False)
    hostname: Mapped[str | None] = mapped_column(String(255))
    version: Mapped[str | None] = mapped_column(String(64))
    heartbeat_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    capabilities: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)


# ========================================================== H. 质量、回滚与清理
class QualityReport(Base, TimestampMixin):
    """质量报告：有效/错误行数、错误码分布与未命中规则样本摘要（不含原始值）。"""

    __tablename__ = "quality_reports"
    __table_args__ = (UniqueConstraint("run_id", name="uq_quality_reports_run"),)

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    run_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("execution_runs.id", ondelete="CASCADE"), nullable=False
    )
    status: Mapped[QualityStatus] = mapped_column(
        enum_column(QualityStatus), nullable=False, default=QualityStatus.INCOMPLETE
    )
    valid_rows: Mapped[int | None] = mapped_column(BigInteger)
    rejected_rows: Mapped[int | None] = mapped_column(BigInteger)
    error_code_distribution: Mapped[dict[str, Any]] = mapped_column(
        JSON, nullable=False, default=dict
    )
    unmatched_rule_samples: Mapped[list[dict[str, Any]]] = mapped_column(
        JSON, nullable=False, default=list
    )
    minimum_rows_required: Mapped[int | None] = mapped_column(BigInteger)
    gate_passed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class RollbackRequest(Base, TimestampMixin):
    """受管回滚；不改写原执行记录，与清理互斥（FR-73、FR-74）。"""

    __tablename__ = "rollback_requests"
    __table_args__ = (Index("ix_rollback_requests_run", "run_id", "status"),)

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    run_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("execution_runs.id", ondelete="CASCADE"), nullable=False
    )
    target_object: Mapped[str] = mapped_column(String(512), nullable=False)
    status: Mapped[RollbackStatus] = mapped_column(
        enum_column(RollbackStatus), nullable=False, default=RollbackStatus.PENDING
    )
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    retained_version: Mapped[str | None] = mapped_column(String(255))
    requested_by: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    decided_by: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True))
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    executed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    failure_reason: Mapped[str | None] = mapped_column(Text)


class RollbackCleanupRequest(Base, TimestampMixin):
    """受管清理：清理影子表未发布脏数据并释放资源。"""

    __tablename__ = "rollback_cleanup_requests"
    __table_args__ = (Index("ix_rollback_cleanups_run", "run_id", "status"),)

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    run_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("execution_runs.id", ondelete="CASCADE"), nullable=False
    )
    rollback_request_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("rollback_requests.id", ondelete="SET NULL")
    )
    target_object: Mapped[str] = mapped_column(String(512), nullable=False)
    shadow_tables: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    status: Mapped[CleanupStatus] = mapped_column(
        enum_column(CleanupStatus), nullable=False, default=CleanupStatus.PENDING
    )
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    requested_by: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    decided_by: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True))
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    executed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    failure_reason: Mapped[str | None] = mapped_column(Text)


# ============================================================== I. 凭据治理
class EncryptedSecret(Base, TimestampMixin):
    """凭据存储；本表只有密文与引用标识，明文永不落盘、不回显。"""

    __tablename__ = "encrypted_secrets"
    __table_args__ = (UniqueConstraint("secret_ref", name="uq_encrypted_secrets_ref"),)

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    project_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    provider: Mapped[str] = mapped_column(String(64), nullable=False)
    secret_ref: Mapped[str] = mapped_column(String(512), nullable=False)
    ciphertext: Mapped[bytes | None] = mapped_column(LargeBinary)
    key_id: Mapped[str | None] = mapped_column(String(128))


class SecretMigrationRequest(Base, TimestampMixin):
    """Provider 迁移；迁移过程不销毁源凭据，可回退（FR-18）。

    以 `connection_id` 为必填锚点，`secret_id` 可空：Vault 后端的凭据在本地**没有密文行**，
    只以 `vault://` 引用存在，若以 `secret_id` 为必填键，Vault 凭据根本无法登记迁移。
    """

    __tablename__ = "secret_migration_requests"

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    connection_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("data_connections.id", ondelete="CASCADE"), nullable=False
    )
    secret_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("encrypted_secrets.id", ondelete="SET NULL")
    )
    source_provider: Mapped[str] = mapped_column(String(64), nullable=False)
    # 迁移前的引用必须留档：换掉连接的 secret_ref 之后就再也找不到源凭据了，
    # 而 FR-18 要求「不销毁源凭据、可回退」——回退的依据就是这个引用。
    source_secret_ref: Mapped[str | None] = mapped_column(String(512))
    target_provider: Mapped[str] = mapped_column(String(64), nullable=False)
    target_secret_ref: Mapped[str | None] = mapped_column(String(512))
    source_retained: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    status: Mapped[SecretMigrationStatus] = mapped_column(
        enum_column(SecretMigrationStatus), nullable=False, default=SecretMigrationStatus.PENDING
    )
    reason: Mapped[str | None] = mapped_column(Text)
    requested_by: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    decided_by: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True))
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    executed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    failure_reason: Mapped[str | None] = mapped_column(Text)


class SecretRotationRequest(Base, TimestampMixin):
    """凭据轮换：暂存 → 验证 → 激活；激活前必须验证新凭据可用（FR-19）。

    暂存凭据由 Provider 落库，本表只保存 `staged_secret_ref` **引用** ——
    再存一份 `staged_ciphertext` 会让密文有两个事实源，两处迟早不一致。
    """

    __tablename__ = "secret_rotation_requests"

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    connection_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("data_connections.id", ondelete="CASCADE"), nullable=False
    )
    secret_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("encrypted_secrets.id", ondelete="SET NULL")
    )
    status: Mapped[SecretRotationStatus] = mapped_column(
        enum_column(SecretRotationStatus), nullable=False, default=SecretRotationStatus.VALIDATING
    )
    previous_secret_ref: Mapped[str] = mapped_column(String(512), nullable=False)
    # 旧凭据的签发时间也要留档：激活时会把连接的签发时间改成「新凭据的起点」，
    # 回退时必须还原，否则到期告警会失去依据（要么误报，要么被悄悄关掉）。
    previous_credential_issued_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    staged_secret_ref: Mapped[str | None] = mapped_column(String(512))
    validated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    activated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    rollback_deadline_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    reason: Mapped[str | None] = mapped_column(Text)
    requested_by: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    decided_by: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True))
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    executed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    failure_reason: Mapped[str | None] = mapped_column(Text)


class SecretRotationRollbackRequest(Base, TimestampMixin):
    """轮换回退：激活后发现问题时回到旧凭据（FR-20）。"""

    __tablename__ = "secret_rotation_rollback_requests"

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    connection_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("data_connections.id", ondelete="CASCADE"), nullable=False
    )
    rotation_request_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("secret_rotation_requests.id", ondelete="CASCADE"),
        nullable=False,
    )
    status: Mapped[SecretRotationRollbackStatus] = mapped_column(
        enum_column(SecretRotationRollbackStatus),
        nullable=False,
        default=SecretRotationRollbackStatus.PENDING,
    )
    reason: Mapped[str | None] = mapped_column(Text)
    requested_by: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    decided_by: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True))
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    executed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    failure_reason: Mapped[str | None] = mapped_column(Text)


class SecretDestructionRequest(Base, TimestampMixin):
    """凭据销毁：只作用于轮换源凭据或已回退的暂存凭据（FR-21）。

    销毁对象由 `target` + `source_rotation_request_id` 共同确定，
    实际要销毁的引用从轮换记录里推导，**不接受调用方直接指定任意 secret_ref**。
    """

    __tablename__ = "secret_destruction_requests"

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    connection_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("data_connections.id", ondelete="CASCADE"), nullable=False
    )
    secret_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("encrypted_secrets.id", ondelete="SET NULL")
    )
    target: Mapped[SecretDestructionTarget] = mapped_column(
        enum_column(SecretDestructionTarget), nullable=False
    )
    source_rotation_request_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("secret_rotation_requests.id", ondelete="SET NULL")
    )
    status: Mapped[SecretDestructionStatus] = mapped_column(
        enum_column(SecretDestructionStatus),
        nullable=False,
        default=SecretDestructionStatus.PENDING,
    )
    reason: Mapped[str | None] = mapped_column(Text)
    requested_by: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    decided_by: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True))
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    executed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    failure_reason: Mapped[str | None] = mapped_column(Text)


class CredentialExpiryAlert(Base, TimestampMixin):
    """凭据到期告警；预警与逾期各有冷却窗口，避免重复打扰。"""

    __tablename__ = "credential_expiry_alerts"
    __table_args__ = (
        Index("ix_credential_expiry_alerts_conn", "connection_id", "level", "status"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    connection_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("data_connections.id", ondelete="CASCADE"), nullable=False
    )
    secret_ref: Mapped[str | None] = mapped_column(String(512))
    level: Mapped[CredentialAlertLevel] = mapped_column(
        enum_column(CredentialAlertLevel), nullable=False
    )
    status: Mapped[CredentialAlertStatus] = mapped_column(
        enum_column(CredentialAlertStatus), nullable=False, default=CredentialAlertStatus.OPEN
    )
    credential_issued_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    credential_max_age_days: Mapped[int | None] = mapped_column(Integer)
    warning_days: Mapped[int | None] = mapped_column(Integer)
    due_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    opened_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    resolution_note: Mapped[str | None] = mapped_column(Text)
    last_scanned_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    scan_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    cooldown_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    delivery_required: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    alert_context: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)


class CredentialAlertDelivery(Base, TimestampMixin):
    """Webhook 投递；接收端按 `delivery_id` 幂等（FR-22）。"""

    __tablename__ = "credential_alert_deliveries"
    __table_args__ = (
        UniqueConstraint("delivery_id", name="uq_credential_alert_deliveries_delivery"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    alert_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("credential_expiry_alerts.id", ondelete="CASCADE"),
        nullable=False,
    )
    delivery_id: Mapped[str] = mapped_column(String(128), nullable=False)
    status: Mapped[CredentialAlertDeliveryStatus] = mapped_column(
        enum_column(CredentialAlertDeliveryStatus),
        nullable=False,
        default=CredentialAlertDeliveryStatus.PENDING,
    )
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    target_url: Mapped[str | None] = mapped_column(String(1024))
    last_error: Mapped[str | None] = mapped_column(Text)
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


# ========================================================= J. 评测与安全进化
class BenchmarkRun(Base, TimestampMixin):
    """一次 Benchmark 运行；门禁结论由逐案期望与六项指标共同决定。"""

    __tablename__ = "benchmark_runs"

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    project_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True))
    schema_version: Mapped[str] = mapped_column(String(32), nullable=False)
    suite_version: Mapped[str] = mapped_column(String(64), nullable=False)
    policy_bundle_version: Mapped[str] = mapped_column(String(128), nullable=False)
    status: Mapped[BenchmarkRunStatus] = mapped_column(
        enum_column(BenchmarkRunStatus), nullable=False, default=BenchmarkRunStatus.QUEUED
    )
    case_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    metrics: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    gate_conclusion: Mapped[BenchmarkGateConclusion | None] = mapped_column(
        enum_column(BenchmarkGateConclusion)
    )
    report_paths: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    include_l2: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    triggered_by: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True))
    failure_reason: Mapped[str | None] = mapped_column(Text)


class SafetyImprovementProposal(Base, TimestampMixin):
    """改进提案状态机；候选正文先过数据泄露边界再入库（FR-88）。"""

    __tablename__ = "safety_improvement_proposals"

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    benchmark_run_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("benchmark_runs.id", ondelete="SET NULL")
    )
    kind: Mapped[SafetyProposalKind] = mapped_column(
        enum_column(SafetyProposalKind), nullable=False
    )
    status: Mapped[SafetyProposalStatus] = mapped_column(
        enum_column(SafetyProposalStatus), nullable=False, default=SafetyProposalStatus.DRAFT
    )
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    affected_rule: Mapped[str | None] = mapped_column(String(128))
    candidate_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    candidate_body_ref: Mapped[str | None] = mapped_column(String(1024))
    leakage_checked: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    submitted_by: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True))
    decided_by: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True))
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    promotion_approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    reason: Mapped[str | None] = mapped_column(Text)


class SafetyRelease(Base, TimestampMixin):
    """发布授权状态机；只产生影子/小流量授权，不热修改生产策略与代码（FR-89）。"""

    __tablename__ = "safety_releases"

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    proposal_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("safety_improvement_proposals.id", ondelete="CASCADE"),
        nullable=False,
    )
    status: Mapped[SafetyReleaseStatus] = mapped_column(
        enum_column(SafetyReleaseStatus), nullable=False, default=SafetyReleaseStatus.PENDING
    )
    scope: Mapped[SafetyReleaseScope] = mapped_column(
        enum_column(SafetyReleaseScope), nullable=False
    )
    canary_percent: Mapped[float | None] = mapped_column(Float)
    shadow_target: Mapped[str | None] = mapped_column(String(255))
    approved_by: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True))
    activated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    effective_from: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    effective_to: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    regression_metric: Mapped[str | None] = mapped_column(String(128))
    regression_observed: Mapped[float | None] = mapped_column(Float)
    regression_threshold: Mapped[float | None] = mapped_column(Float)
    gate_conclusion: Mapped[BenchmarkGateConclusion | None] = mapped_column(
        enum_column(BenchmarkGateConclusion)
    )
    # 回归自动撤销在同一事务内完成，`revoked_at` 与原因同时落库（FR-90）。
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revocation_reason: Mapped[str | None] = mapped_column(Text)
    notes: Mapped[str | None] = mapped_column(Text)
    lock_version: Mapped[int] = mapped_column(Integer, nullable=False, default=0)


# ==================================================================== K. 审计
class AuditEvent(Base, TimestampMixin):
    """审计只记录变化：主体、事件类型、目标对象与变化摘要。"""

    __tablename__ = "audit_events"
    __table_args__ = (Index("ix_audit_events_target", "target_type", "target_id"),)

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    project_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True))
    actor_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True))
    event_type: Mapped[AuditEventType] = mapped_column(enum_column(AuditEventType), nullable=False)
    target_type: Mapped[str] = mapped_column(String(64), nullable=False)
    target_id: Mapped[str | None] = mapped_column(String(128))
    change_summary: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    trace_id: Mapped[str | None] = mapped_column(String(32))
