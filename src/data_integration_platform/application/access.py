"""项目访问校验原语（PRD FR-02、FR-06 / SOD-05）。

两套角色必须都覆盖（PRD §2.3）：
- `ProjectRole` 授予「能碰什么资源」，由本模块校验；
- `HarnessRole` 授予「能批准/执行什么动作」，只在本模块提供角色读取，
  具体动作校验由审批与授权链路（M4/M5）使用。
"""

import uuid
from collections.abc import Collection

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from data_integration_platform.domain.enums import ProjectRole
from data_integration_platform.domain.errors import PermissionDenied
from data_integration_platform.domain.harness import HarnessRole, require_auditor_is_read_only
from data_integration_platform.infrastructure.models import ProjectMembership, ProjectRoleGrant


async def project_roles_of(
    session: AsyncSession, *, project_id: uuid.UUID, actor_id: uuid.UUID
) -> frozenset[ProjectRole]:
    """列出主体在某项目内的成员角色；非成员返回空集合。"""
    roles = await session.scalars(
        select(ProjectMembership.role).where(
            ProjectMembership.project_id == project_id,
            ProjectMembership.user_id == actor_id,
        )
    )
    return frozenset(roles.all())


async def harness_roles_of(
    session: AsyncSession, *, project_id: uuid.UUID, actor_id: uuid.UUID
) -> frozenset[HarnessRole]:
    """列出主体在某项目内已获授的 Harness 职责槽。"""
    roles = await session.scalars(
        select(ProjectRoleGrant.role).where(
            ProjectRoleGrant.project_id == project_id,
            ProjectRoleGrant.user_id == actor_id,
        )
    )
    return frozenset(roles.all())


async def require_project_roles(
    session: AsyncSession,
    *,
    project_id: uuid.UUID,
    actor_id: uuid.UUID,
    allowed: Collection[ProjectRole],
    write: bool = False,
) -> frozenset[ProjectRole]:
    """校验主体在项目内具备所需成员角色。

    跨项目访问在这里被拒（FR-02 按 P0 处理）；写入口额外执行审计只读校验（SOD-05）。
    """
    roles = await project_roles_of(session, project_id=project_id, actor_id=actor_id)
    if not roles & set(allowed):
        raise PermissionDenied("无权访问该项目资源")
    if write:
        require_auditor_is_read_only(
            harness_roles=await harness_roles_of(session, project_id=project_id, actor_id=actor_id)
        )
    return roles
