"""请求与响应契约（SPEC §6.2）。

所有响应都只含低敏字段：凭据以 `secret_ref` 引用形式出现，样本一律为脱敏结果。
"""

import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, SecretStr

from data_integration_platform.domain.enums import (
    ConnectionStatus,
    ConnectorType,
    CredentialAlertDeliveryStatus,
    CredentialAlertLevel,
    CredentialAlertStatus,
    DataEndpointRole,
    DataProfileStatus,
    FileAssetFormat,
    ProjectRole,
    ProjectStatus,
    SecretDestructionStatus,
    SecretDestructionTarget,
    SecretMigrationStatus,
    SecretRotationRollbackStatus,
    SecretRotationStatus,
    UserStatus,
)
from data_integration_platform.domain.harness import HarnessApprovalValue, HarnessRole


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
    """受管副作用的具名审批入参。"""

    decision: HarnessApprovalValue
    reason: str = Field(min_length=1, max_length=2000)


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
    name: str
    format: FileAssetFormat
    endpoint_role: DataEndpointRole
    size_bytes: int
    row_count: int | None
    checksum_sha256: str
    created_at: datetime
