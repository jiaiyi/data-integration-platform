"""身份、项目与成员治理接口（FR-03 ~ FR-06）。"""

import uuid

from fastapi import APIRouter, Depends, status
from sqlalchemy.ext.asyncio import AsyncSession

from data_integration_platform.api.dependencies import (
    ActorId,
    TraceId,
    require_bootstrap_token,
)
from data_integration_platform.api.schemas import (
    AddMemberRequest,
    BootstrapRequest,
    BootstrapResponse,
    CreateProjectRequest,
    GrantHarnessRoleRequest,
    MembershipResponse,
    ProjectResponse,
    RoleGrantResponse,
    UserResponse,
)
from data_integration_platform.application.projects import ProjectService
from data_integration_platform.domain.harness import HarnessRole
from data_integration_platform.infrastructure.database import get_session

router = APIRouter(tags=["projects"])
service = ProjectService()


@router.post(
    "/bootstrap",
    response_model=BootstrapResponse,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_bootstrap_token)],
)
async def bootstrap(
    request: BootstrapRequest,
    trace_id: TraceId,
    session: AsyncSession = Depends(get_session),
) -> BootstrapResponse:
    """首次初始化：创建首位管理员与其所属项目。一次性动作，不可重复调用。"""
    async with session.begin():
        user, project = await service.bootstrap(
            session,
            admin_subject=request.admin_subject,
            admin_display_name=request.admin_display_name,
            project_name=request.project_name,
            trace_id=trace_id,
        )
        response = BootstrapResponse(
            user=UserResponse.model_validate(user),
            project=ProjectResponse.model_validate(project),
        )
    return response


@router.post("/projects", response_model=ProjectResponse, status_code=status.HTTP_201_CREATED)
async def create_project(
    request: CreateProjectRequest,
    actor: ActorId,
    trace_id: TraceId,
    session: AsyncSession = Depends(get_session),
) -> ProjectResponse:
    """创建项目，创建者成为该项目管理员。"""
    async with session.begin():
        project = await service.create_project(
            session, name=request.name, actor_id=actor, trace_id=trace_id
        )
    return ProjectResponse.model_validate(project)


@router.get("/projects", response_model=list[ProjectResponse])
async def list_projects(
    actor: ActorId,
    session: AsyncSession = Depends(get_session),
) -> list[ProjectResponse]:
    """列出调用者所属项目；跨项目资源不可见（FR-02）。"""
    projects = await service.list_projects(session, actor_id=actor)
    return [ProjectResponse.model_validate(item) for item in projects]


@router.get("/projects/{project_id}/members", response_model=list[MembershipResponse])
async def list_members(
    project_id: uuid.UUID,
    actor: ActorId,
    session: AsyncSession = Depends(get_session),
) -> list[MembershipResponse]:
    members = await service.list_members(session, project_id=project_id, actor_id=actor)
    return [MembershipResponse.model_validate(item) for item in members]


@router.put("/projects/{project_id}/members", response_model=MembershipResponse)
async def add_member(
    project_id: uuid.UUID,
    request: AddMemberRequest,
    actor: ActorId,
    trace_id: TraceId,
    session: AsyncSession = Depends(get_session),
) -> MembershipResponse:
    """添加成员或调整既有成员的项目角色；主体不存在时按需建档。"""
    async with session.begin():
        membership = await service.add_member(
            session,
            project_id=project_id,
            subject=request.subject,
            display_name=request.display_name,
            role=request.role,
            actor_id=actor,
            trace_id=trace_id,
        )
    return MembershipResponse.model_validate(membership)


@router.delete("/projects/{project_id}/members/{user_id}", status_code=status.HTTP_204_NO_CONTENT)
async def remove_member(
    project_id: uuid.UUID,
    user_id: uuid.UUID,
    actor: ActorId,
    trace_id: TraceId,
    session: AsyncSession = Depends(get_session),
) -> None:
    """移除成员，并同时撤销其全部 Harness 职责槽。"""
    async with session.begin():
        await service.remove_member(
            session,
            project_id=project_id,
            user_id=user_id,
            actor_id=actor,
            trace_id=trace_id,
        )


@router.get("/projects/{project_id}/role-grants", response_model=list[RoleGrantResponse])
async def list_role_grants(
    project_id: uuid.UUID,
    actor: ActorId,
    session: AsyncSession = Depends(get_session),
) -> list[RoleGrantResponse]:
    grants = await service.list_role_grants(session, project_id=project_id, actor_id=actor)
    return [RoleGrantResponse.model_validate(item) for item in grants]


@router.post(
    "/projects/{project_id}/role-grants",
    response_model=RoleGrantResponse,
    status_code=status.HTTP_201_CREATED,
)
async def grant_harness_role(
    project_id: uuid.UUID,
    request: GrantHarnessRoleRequest,
    actor: ActorId,
    trace_id: TraceId,
    session: AsyncSession = Depends(get_session),
) -> RoleGrantResponse:
    """把成员映射到 Harness 职责槽；互斥槽位在授予期即被拒绝（SOD-02）。"""
    async with session.begin():
        grant = await service.grant_harness_role(
            session,
            project_id=project_id,
            user_id=request.user_id,
            role=request.role,
            actor_id=actor,
            trace_id=trace_id,
        )
    return RoleGrantResponse.model_validate(grant)


@router.delete(
    "/projects/{project_id}/role-grants/{user_id}", status_code=status.HTTP_204_NO_CONTENT
)
async def revoke_harness_role(
    project_id: uuid.UUID,
    user_id: uuid.UUID,
    role: HarnessRole,
    actor: ActorId,
    trace_id: TraceId,
    session: AsyncSession = Depends(get_session),
) -> None:
    """撤销职责槽。"""
    async with session.begin():
        await service.revoke_harness_role(
            session,
            project_id=project_id,
            user_id=user_id,
            role=role,
            actor_id=actor,
            trace_id=trace_id,
        )
