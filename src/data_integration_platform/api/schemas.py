"""请求与响应契约（SPEC §6.2）。

所有响应都只含低敏字段：凭据以 `secret_ref` 引用形式出现，样本一律为脱敏结果。
"""

import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, SecretStr

from data_integration_platform.domain.enums import (
    AgentRunStatus,
    ApprovalDecisionValue,
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
    HarnessRole,
    RiskLevel,
)
from data_integration_platform.domain.side_effects import (
    SideEffectExecutionStatus,
    SideEffectOperation,
)
from data_integration_platform.domain.supervision import SupervisionAction


class ResponseModel(BaseModel):
    """可直接由 ORM 对象构造的响应基类。"""

    model_config = ConfigDict(from_attributes=True)


# ------------------------------------------------------------------ 系统
class HealthResponse(BaseModel):
    status: str
    service: str
    version: str
    environment: str
    auth_mode: str
    # 策略决策可追溯的前提是决策记录能对回策略包版本（FR-49），控制台也需要看到它。
    policy_bundle_version: str


# ------------------------------------------------------------------ 身份
class UserResponse(ResponseModel):
    id: uuid.UUID
    subject: str
    display_name: str
    status: UserStatus


class BootstrapRequest(BaseModel):
    admin_subject: str = Field(min_length=1, max_length=255)
    admin_display_name: str = Field(min_length=1, max_length=255)
    project_name: str = Field(min_length=1, max_length=255)


# ------------------------------------------------------------------ 项目
class ProjectResponse(ResponseModel):
    id: uuid.UUID
    name: str
    status: ProjectStatus
    created_at: datetime


class BootstrapResponse(BaseModel):
    user: UserResponse
    project: ProjectResponse


class CreateProjectRequest(BaseModel):
    name: str = Field(min_length=1, max_length=255)


class AddMemberRequest(BaseModel):
    subject: str = Field(min_length=1, max_length=255)
    display_name: str = Field(min_length=1, max_length=255)
    role: ProjectRole


class MembershipResponse(ResponseModel):
    id: uuid.UUID
    project_id: uuid.UUID
    user_id: uuid.UUID
    role: ProjectRole


class GrantHarnessRoleRequest(BaseModel):
    user_id: uuid.UUID
    role: HarnessRole


class RoleGrantResponse(ResponseModel):
    id: uuid.UUID
    project_id: uuid.UUID
    user_id: uuid.UUID
    role: HarnessRole


# ------------------------------------------------------------------ 连接与探查
class ConnectionCredentialsRequest(BaseModel):
    """凭据入参；全部用 `SecretStr` 包裹，不会出现在响应、日志或异常里。"""

    model_config = ConfigDict(extra="forbid")

    username: str | None = None
    password: SecretStr | None = None
    auth_header_name: str | None = None
    auth_header_value: SecretStr | None = None
    access_key: str | None = None
    secret_key: SecretStr | None = None
    session_token: SecretStr | None = None


class CreateConnectionRequest(BaseModel):
    project_id: uuid.UUID
    name: str = Field(min_length=1, max_length=255)
    connector_type: ConnectorType
    endpoint_role: DataEndpointRole
    host: str | None = Field(default=None, max_length=255)
    port: int | None = Field(default=None, ge=1, le=65535)
    database_name: str | None = Field(default=None, max_length=255)
    options: dict[str, str] = Field(default_factory=dict)
    credentials: ConnectionCredentialsRequest | None = None


class UpdateConnectionRequest(BaseModel):
    """只更新非凭据配置；更换凭据须走受管的凭据生命周期（FR-23）。"""

    name: str | None = Field(default=None, min_length=1, max_length=255)
    host: str | None = Field(default=None, max_length=255)
    port: int | None = Field(default=None, ge=1, le=65535)
    database_name: str | None = Field(default=None, max_length=255)
    options: dict[str, str] | None = None
    endpoint_role: DataEndpointRole | None = None


class DataConnectionResponse(ResponseModel):
    """连接事实。

    `secret_ref` 是**引用**不是秘密：凭据以 `fernet://…` 或 `vault://…` 形式呈现，
    界面与 API 都不提供明文查看入口（G-07）。
    """

    id: uuid.UUID
    project_id: uuid.UUID
    name: str
    connector_type: ConnectorType
    endpoint_role: DataEndpointRole
    status: ConnectionStatus
    host: str | None
    port: int | None
    database_name: str | None
    options: dict[str, Any]
    secret_ref: str | None
    last_tested_at: datetime | None
    last_test_error: str | None


class CreateProfileRequest(BaseModel):
    schema_name: str = Field(min_length=1, max_length=255)
    table_name: str = Field(min_length=1, max_length=255)
    endpoint_role: DataEndpointRole
    sample_limit: int | None = Field(default=None, ge=1, le=1000)


class DataProfileResponse(ResponseModel):
    """Profile 事实；`masked_sample` 是脱敏后的样本，原文不落盘（FR-12）。"""

    id: uuid.UUID
    connection_id: uuid.UUID
    schema_name: str
    table_name: str
    endpoint_role: DataEndpointRole
    status: DataProfileStatus
    columns: list[dict[str, Any]]
    primary_keys: list[str]
    statistics: dict[str, Any]
    masked_sample: list[dict[str, Any]]
    sample_size: int
    expires_at: datetime | None
    error: str | None


class UpdateCredentialRotationPolicyRequest(BaseModel):
    """连接级凭据轮换策略；用于推导到期告警，不修改凭据本身。"""

    enabled: bool
    max_age_days: int | None = Field(default=None, ge=1, le=3650)
    warning_days: int | None = Field(default=None, ge=1, le=3650)


class HarnessDecisionRequest(BaseModel):
    """受管副作用的具名审批入参。

    `role` 只在准备单有**多个**审批槽时才是必填的（例如凭据销毁要数据审批人与
    安全审批人各一名）：只有一个槽时留空即可，服务端取那唯一的槽位。
    要求两个槽位却不说清是哪一个，服务端会拒绝而不是替调用方猜。
    """

    decision: HarnessApprovalValue
    reason: str = Field(min_length=1, max_length=2000)
    role: HarnessRole | None = None


# ------------------------------------------------------------ 凭据治理（T-15）
class _ManagedRequestResponse(ResponseModel):
    """四类凭据申请的公共字段。"""

    id: uuid.UUID
    connection_id: uuid.UUID
    status: str
    reason: str | None
    requested_by: uuid.UUID
    decided_by: uuid.UUID | None
    decided_at: datetime | None
    executed_at: datetime | None
    failure_reason: str | None
    created_at: datetime


class CreateSecretMigrationRequest(BaseModel):
    connection_id: uuid.UUID
    target_provider: str = Field(min_length=1, max_length=64)
    reason: str = Field(min_length=1, max_length=2000)


class SecretMigrationRequestResponse(_ManagedRequestResponse):
    status: SecretMigrationStatus
    source_provider: str
    target_provider: str
    source_retained: bool


class CreateSecretRotationRequest(BaseModel):
    connection_id: uuid.UUID
    new_credentials: ConnectionCredentialsRequest
    reason: str = Field(min_length=1, max_length=2000)


class SecretRotationRequestResponse(_ManagedRequestResponse):
    status: SecretRotationStatus
    previous_secret_ref: str
    staged_secret_ref: str | None
    validated_at: datetime | None
    activated_at: datetime | None
    rollback_deadline_at: datetime | None


class CreateSecretRotationRollbackRequest(BaseModel):
    rotation_request_id: uuid.UUID
    reason: str = Field(min_length=1, max_length=2000)


class SecretRotationRollbackRequestResponse(_ManagedRequestResponse):
    status: SecretRotationRollbackStatus
    rotation_request_id: uuid.UUID


class CreateSecretDestructionRequest(BaseModel):
    connection_id: uuid.UUID
    target: SecretDestructionTarget
    rotation_request_id: uuid.UUID
    reason: str = Field(min_length=1, max_length=2000)


class SecretDestructionRequestResponse(_ManagedRequestResponse):
    status: SecretDestructionStatus
    target: SecretDestructionTarget
    source_rotation_request_id: uuid.UUID | None


class CredentialAlertResponse(ResponseModel):
    id: uuid.UUID
    connection_id: uuid.UUID
    level: CredentialAlertLevel
    status: CredentialAlertStatus
    credential_issued_at: datetime | None
    credential_max_age_days: int | None
    warning_days: int | None
    due_at: datetime | None
    opened_at: datetime
    resolved_at: datetime | None
    resolution_note: str | None
    scan_count: int
    cooldown_until: datetime | None
    delivery_required: bool


class CredentialAlertDeliveryResponse(ResponseModel):
    delivery_id: str
    alert_id: uuid.UUID
    status: CredentialAlertDeliveryStatus
    attempts: int
    last_error: str | None
    delivered_at: datetime | None


# ------------------------------------------------------------------ 文件资产
class FileAssetResponse(ResponseModel):
    id: uuid.UUID
    project_id: uuid.UUID
    # 非空表示这份文件是某次执行的交付物（T-40）；用户上传的资产留空。
    execution_run_id: uuid.UUID | None
    name: str
    format: FileAssetFormat
    endpoint_role: DataEndpointRole
    size_bytes: int
    row_count: int | None
    checksum_sha256: str
    created_at: datetime


# ------------------------------------------------------------------ 管道版本（T-22）
class CreatePipelineRequest(BaseModel):
    project_id: uuid.UUID
    name: str = Field(min_length=1, max_length=255)
    requirement: str = Field(min_length=1, max_length=20_000)


class RevisePipelineVersionRequest(BaseModel):
    """需求变更：只带新需求，平台据此创建 V+1（FR-39）。"""

    requirement: str = Field(min_length=1, max_length=20_000)


class PipelineVersionResponse(ResponseModel):
    id: uuid.UUID
    pipeline_id: uuid.UUID
    version_number: int
    status: PipelineVersionStatus
    requirement: str
    parent_version_id: uuid.UUID | None
    artifact_sha256: str | None
    lock_version: int


class PipelineDesignResponse(BaseModel):
    """设计审查视图（API-04）；未冻结时 `plan` / `hocon` 为空而不是占位内容。"""

    pipeline_version_id: uuid.UUID
    status: PipelineVersionStatus
    artifact_sha256: str | None
    plan: dict[str, Any] | None
    hocon: str | None


class ApprovalRequestResponse(ResponseModel):
    id: uuid.UUID
    version_id: uuid.UUID
    status: str
    artifact_sha256: str
    requested_by: uuid.UUID


# ------------------------------------------------------------------ 生成记录（T-19 / T-18）
class StartAgentRunRequest(BaseModel):
    """发起生成；受管上下文由平台按 `profile_ids` 装配，调用方不直接给表结构。"""

    profile_ids: list[uuid.UUID] = Field(min_length=1, max_length=20)
    quality_contract: dict[str, Any] | None = None
    incremental_contract: dict[str, Any] | None = None
    row_llm_contract: dict[str, Any] | None = None


class AnswerAgentRunRequest(BaseModel):
    """回答澄清问题；从同一 `thread_id` 恢复，不重跑（FR-27）。"""

    answer: str = Field(min_length=1, max_length=20_000)


class AgentRunResponse(ResponseModel):
    id: uuid.UUID
    version_id: uuid.UUID
    status: AgentRunStatus
    thread_id: str
    generation_phase: str | None
    generation_backend: str | None
    generation_model: str | None
    pending_question: str | None
    clarification_rounds: int
    generation_candidates: int
    generation_repairs: int
    generation_diagnostics: dict[str, Any] | None
    error: str | None
    started_at: datetime | None
    finished_at: datetime | None


# ------------------------------------------------------ 通用副作用（T-36）
class SideEffectPreparationResponse(ResponseModel):
    """副作用准备单视图。

    `impact_report` 与 `rollback_plan` 是审批人判断「这次动什么」的依据，因此原样返回；
    两者都由服务端推导，不含凭据、不含原始业务行。
    """

    id: uuid.UUID
    operation: SideEffectOperation
    project_id: uuid.UUID
    aggregate_type: str
    aggregate_id: str
    status: ExecutionPreparationStatus
    execution_status: SideEffectExecutionStatus
    risk_level: RiskLevel
    policy_bundle_version: str
    environment: str
    tool_name: str
    tool_version: str
    fact_fingerprint: str
    input_fingerprint: str
    resource_fingerprint: str
    impact_report: dict[str, Any]
    rollback_plan: dict[str, Any]
    required_roles: list[str]
    required_approvals: int
    expires_at: datetime
    committed_by: uuid.UUID | None
    committed_at: datetime | None
    failure_reason: str | None
    created_by: uuid.UUID
    created_at: datetime


class SideEffectApprovalResponse(ResponseModel):
    id: uuid.UUID
    preparation_id: uuid.UUID
    approver_id: uuid.UUID
    role: HarnessRole
    decision: HarnessApprovalValue
    reason: str
    decided_at: datetime


# ------------------------------------------------------ 执行与三阶段协议（M5）
class PrepareExecutionRequest(BaseModel):
    """Prepare 的入参。

    **只有一个幂等键**：风险等级、工具版本、资源范围、制品摘要全部由服务端推导
    （FR-55）。这里多出任何一个字段，都是在请调用方替服务端决定事实。

    `extra="forbid"` 不是洁癖：pydantic 默认**静默丢弃**未知字段，于是
    「客户端塞了 `risk_level`」会变成一个成功的 201 —— 调用方以为它生效了，
    平台却根本没看。拒绝它是让 FR-55 在契约层就成立，而不是靠服务端「选择忽略」。
    """

    model_config = ConfigDict(extra="forbid")

    idempotency_key: str = Field(min_length=8, max_length=128)


class ExecutionPreparationResponse(ResponseModel):
    id: uuid.UUID
    pipeline_version_id: uuid.UUID
    status: ExecutionPreparationStatus
    risk_level: RiskLevel
    policy_bundle_version: str
    environment: str
    tool_name: str
    tool_version: str
    artifact_sha256: str
    input_fingerprint: str
    resource_fingerprint: str
    impact_report: dict[str, Any]
    rollback_plan: dict[str, Any]
    required_roles: list[str]
    required_approvals: int
    expires_at: datetime
    committed_by: uuid.UUID | None
    committed_at: datetime | None
    created_by: uuid.UUID
    created_at: datetime


class ExecutionPreparationDecisionRequest(BaseModel):
    role: HarnessRole
    decision: HarnessApprovalValue
    reason: str = Field(min_length=1, max_length=2000)


class CancelExecutionRequest(BaseModel):
    reason: str = Field(min_length=1, max_length=2000)


class ExecutionRunResponse(ResponseModel):
    """执行实例的完整投影。

    指标字段引擎没给就是 `None`，**不用 0 冒充**（FR-61）——「没量到」与
    「量到 0 行」是两件对排查完全不同的事。
    """

    id: uuid.UUID
    pipeline_version_id: uuid.UUID
    harness_preparation_id: uuid.UUID
    status: ExecutionStatus
    trigger: ExecutionTrigger
    engine_status: str | None
    seatunnel_job_id: str | None
    submitted_at: datetime | None
    started_at: datetime | None
    finished_at: datetime | None
    last_observed_at: datetime | None
    retry_of_run_id: uuid.UUID | None
    attempt_number: int
    cancel_requested_at: datetime | None
    cancel_reason: str | None
    source_rows: int | None
    sink_rows: int | None
    source_bytes: int | None
    sink_bytes: int | None
    table_metrics: dict[str, Any] | None
    failure_category: FailureCategory | None
    failure_stage: FailureStage | None
    retryable: bool | None
    diagnosis: dict[str, Any] | None
    supervision_action: SupervisionAction | None
    supervision_contract_sha256: str | None
    quarantined_at: datetime | None
    quarantine_reason: str | None
    schedule_id: uuid.UUID | None
    scheduled_for: datetime | None
    watermark_window: dict[str, Any] | None
    created_at: datetime


class ApprovalDecisionRequest(BaseModel):
    """版本审批的具名决策。

    与副作用的 `HarnessDecisionRequest` 刻意分开：两者用的**不是同一套审批枚举**
    （`ApprovalDecisionValue` 管「版本能不能被执行准备引用」，
    `HarnessApprovalValue` 管「这条受管副作用能不能动手」），
    合成一个模型会让两套语义在契约层就含糊掉。
    """

    decision: ApprovalDecisionValue
    reason: str = Field(min_length=1, max_length=2000)


class ScheduleResponse(ResponseModel):
    id: uuid.UUID
    version_id: uuid.UUID
    mode: ScheduleMode
    cron_expression: str
    timezone: str
    misfire_policy: ScheduleMisfirePolicy
    enabled: bool
    watermark_column: str | None
    watermark_window_start: datetime | None
    watermark_window_end: datetime | None
    next_run_at: datetime | None
    last_run_at: datetime | None
    created_at: datetime


class ConfigureScheduleRequest(BaseModel):
    mode: ScheduleMode
    cron_expression: str = Field(min_length=1, max_length=128)
    timezone: str = Field(min_length=1, max_length=64)
    misfire_policy: ScheduleMisfirePolicy
    watermark_column: str | None = Field(default=None, max_length=255)


class SetScheduleEnabledRequest(BaseModel):
    enabled: bool
