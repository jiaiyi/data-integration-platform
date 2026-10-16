"""请求与响应契约（SPEC §6.2）。

所有响应都只含低敏字段：凭据以 `secret_ref` 引用形式出现，样本一律为脱敏结果。
"""

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from data_integration_platform.domain.enums import ProjectRole, ProjectStatus, UserStatus
from data_integration_platform.domain.harness import HarnessRole


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
