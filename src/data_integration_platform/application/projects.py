"""身份与项目治理用例（PRD FR-03 ~ FR-06）。

两套角色的边界（PRD §2.3）：`ProjectRole` 决定「能碰什么资源」，
`HarnessRole` 决定「能批准/执行什么动作」。管理员可以把成员映射到职责槽，
但**不会因此隐式获得审批权限** —— 授予是显式动作，且受 SOD-02 互斥约束。
"""

import uuid
from collections.abc import Sequence

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from data_integration_platform.application.access import require_project_roles
from data_integration_platform.application.audit import record_audit_event
from data_integration_platform.domain.enums import (
    AuditEventType,
    ProjectRole,
    ProjectStatus,
    UserStatus,
)
from data_integration_platform.domain.errors import DomainError, ResourceNotFound, VersionConflict
from data_integration_platform.domain.harness import (
    HarnessRole,
    require_no_conflicting_harness_roles,
)
from data_integration_platform.infrastructure.models import (
    Project,
    ProjectMembership,
    ProjectRoleGrant,
    User,
)

ADMIN_ONLY = frozenset({ProjectRole.ADMIN})
READABLE = frozenset(
    {ProjectRole.ADMIN, ProjectRole.DATA_ENGINEER, ProjectRole.APPROVER, ProjectRole.VIEWER}
)


class ProjectService:
    """项目、成员与 Harness 职责槽的用例服务；所有写入与审计共用调用方的事务。"""

    async def bootstrap(
        self,
        session: AsyncSession,
        *,
        admin_subject: str,
        admin_display_name: str,
        project_name: str,
        trace_id: str | None = None,
    ) -> tuple[User, Project]:
        """首次初始化：创建首位管理员与其所属项目。

        这是**一次性**动作 —— 已存在任何用户时拒绝执行，避免长期开放的初始化入口
        成为提权通道（FR-05）。首位管理员只获得项目成员角色 ADMIN，
        不会被隐式授予任何 Harness 职责槽。
        """
        existing = await session.scalar(select(func.count()).select_from(User))
        if existing:
            raise DomainError("首次初始化已完成，接口不可重复调用")

        admin = User(
            subject=admin_subject, display_name=admin_display_name, status=UserStatus.ACTIVE
        )
        project = Project(name=project_name, status=ProjectStatus.ACTIVE)
        session.add_all([admin, project])
        await session.flush()

        session.add(
            ProjectMembership(project_id=project.id, user_id=admin.id, role=ProjectRole.ADMIN)
        )
        record_audit_event(
            session,
            event_type=AuditEventType.BOOTSTRAP_COMPLETED,
            target_type="project",
            project_id=project.id,
            actor_id=admin.id,
            target_id=str(project.id),
            change_summary={"admin_subject": admin_subject, "project_name": project_name},
            trace_id=trace_id,
        )
        return admin, project

    async def create_project(
        self,
        session: AsyncSession,
        *,
        name: str,
        actor_id: uuid.UUID,
        trace_id: str | None = None,
    ) -> Project:
        """创建项目，创建者成为该项目管理员并加入成员。"""
        await self._require_user(session, actor_id)
        if await session.scalar(select(Project.id).where(Project.name == name)) is not None:
            raise VersionConflict(f"项目名称已存在：{name}")

        project = Project(name=name, status=ProjectStatus.ACTIVE)
        session.add(project)
        await session.flush()
        session.add(
            ProjectMembership(project_id=project.id, user_id=actor_id, role=ProjectRole.ADMIN)
        )
        record_audit_event(
            session,
            event_type=AuditEventType.PROJECT_CREATED,
            target_type="project",
            project_id=project.id,
            actor_id=actor_id,
            target_id=str(project.id),
            change_summary={"name": name, "creator_role": ProjectRole.ADMIN.value},
            trace_id=trace_id,
        )
        return project

    async def list_projects(
        self, session: AsyncSession, *, actor_id: uuid.UUID
    ) -> Sequence[Project]:
        """列出主体所属的项目；跨项目资源对主体不可见（FR-02）。"""
        return (
            await session.scalars(
                select(Project)
                .join(ProjectMembership, ProjectMembership.project_id == Project.id)
                .where(ProjectMembership.user_id == actor_id)
                .order_by(Project.created_at)
            )
        ).all()

    async def add_member(
        self,
        session: AsyncSession,
        *,
        project_id: uuid.UUID,
        subject: str,
        display_name: str,
        role: ProjectRole,
        actor_id: uuid.UUID,
        trace_id: str | None = None,
    ) -> ProjectMembership:
        """添加成员或调整既有成员的项目角色。

        主体（subject）不存在时按需创建用户，避免要求管理员先单独建档。
        """
        await require_project_roles(
            session, project_id=project_id, actor_id=actor_id, allowed=ADMIN_ONLY, write=True
        )
        user = await self._resolve_user(session, subject=subject, display_name=display_name)
        membership = await session.scalar(
            select(ProjectMembership).where(
                ProjectMembership.project_id == project_id,
                ProjectMembership.user_id == user.id,
            )
        )
        if membership is None:
            membership = ProjectMembership(project_id=project_id, user_id=user.id, role=role)
            session.add(membership)
            previous: str | None = None
        else:
            previous = membership.role.value
            membership.role = role
        await session.flush()
        record_audit_event(
            session,
            event_type=AuditEventType.MEMBERSHIP_GRANTED,
            target_type="project_membership",
            project_id=project_id,
            actor_id=actor_id,
            target_id=str(membership.id),
            change_summary={"subject": subject, "previous_role": previous, "role": role.value},
            trace_id=trace_id,
        )
        return membership

    async def remove_member(
        self,
        session: AsyncSession,
        *,
        project_id: uuid.UUID,
        user_id: uuid.UUID,
        actor_id: uuid.UUID,
        trace_id: str | None = None,
    ) -> None:
        """移除成员并同时撤销其 Harness 职责槽 —— 离开项目即失去审批与执行资格。"""
        await require_project_roles(
            session, project_id=project_id, actor_id=actor_id, allowed=ADMIN_ONLY, write=True
        )
        membership = await session.scalar(
            select(ProjectMembership).where(
                ProjectMembership.project_id == project_id,
                ProjectMembership.user_id == user_id,
            )
        )
        if membership is None:
            raise ResourceNotFound("该用户不是本项目成员")

        revoked = await self._revoke_all_harness_roles(
            session, project_id=project_id, user_id=user_id
        )
        await session.delete(membership)
        record_audit_event(
            session,
            event_type=AuditEventType.MEMBERSHIP_REVOKED,
            target_type="project_membership",
            project_id=project_id,
            actor_id=actor_id,
            target_id=str(user_id),
            change_summary={"revoked_harness_roles": revoked},
            trace_id=trace_id,
        )

    async def grant_harness_role(
        self,
        session: AsyncSession,
        *,
        project_id: uuid.UUID,
        user_id: uuid.UUID,
        role: HarnessRole,
        actor_id: uuid.UUID,
        trace_id: str | None = None,
    ) -> ProjectRoleGrant:
        """把成员映射到 Harness 职责槽；互斥槽位在授予期即被拒绝（SOD-02）。"""
        await require_project_roles(
            session, project_id=project_id, actor_id=actor_id, allowed=ADMIN_ONLY, write=True
        )
        await self._require_membership(session, project_id=project_id, user_id=user_id)

        granted = frozenset(
            (
                await session.scalars(
                    select(ProjectRoleGrant.role).where(
                        ProjectRoleGrant.project_id == project_id,
                        ProjectRoleGrant.user_id == user_id,
                    )
                )
            ).all()
        )
        require_no_conflicting_harness_roles(granted=granted, additional=role)

        existing = await session.scalar(
            select(ProjectRoleGrant).where(
                ProjectRoleGrant.project_id == project_id,
                ProjectRoleGrant.user_id == user_id,
                ProjectRoleGrant.role == role,
            )
        )
        if existing is not None:
            return existing

        grant = ProjectRoleGrant(project_id=project_id, user_id=user_id, role=role)
        session.add(grant)
        await session.flush()
        record_audit_event(
            session,
            event_type=AuditEventType.ROLE_GRANT_ASSIGNED,
            target_type="project_role_grant",
            project_id=project_id,
            actor_id=actor_id,
            target_id=str(user_id),
            change_summary={"role": role.value},
            trace_id=trace_id,
        )
        return grant

    async def revoke_harness_role(
        self,
        session: AsyncSession,
        *,
        project_id: uuid.UUID,
        user_id: uuid.UUID,
        role: HarnessRole,
        actor_id: uuid.UUID,
        trace_id: str | None = None,
    ) -> None:
        """撤销职责槽；撤销不存在的授予按幂等处理。"""
        await require_project_roles(
            session, project_id=project_id, actor_id=actor_id, allowed=ADMIN_ONLY, write=True
        )
        await session.execute(
            delete(ProjectRoleGrant).where(
                ProjectRoleGrant.project_id == project_id,
                ProjectRoleGrant.user_id == user_id,
                ProjectRoleGrant.role == role,
            )
        )
        record_audit_event(
            session,
            event_type=AuditEventType.ROLE_GRANT_REVOKED,
            target_type="project_role_grant",
            project_id=project_id,
            actor_id=actor_id,
            target_id=str(user_id),
            change_summary={"role": role.value},
            trace_id=trace_id,
        )

    async def list_members(
        self, session: AsyncSession, *, project_id: uuid.UUID, actor_id: uuid.UUID
    ) -> Sequence[ProjectMembership]:
        await require_project_roles(
            session, project_id=project_id, actor_id=actor_id, allowed=READABLE
        )
        return (
            await session.scalars(
                select(ProjectMembership)
                .where(ProjectMembership.project_id == project_id)
                .order_by(ProjectMembership.created_at)
            )
        ).all()

    async def list_role_grants(
        self, session: AsyncSession, *, project_id: uuid.UUID, actor_id: uuid.UUID
    ) -> Sequence[ProjectRoleGrant]:
        await require_project_roles(
            session, project_id=project_id, actor_id=actor_id, allowed=READABLE
        )
        return (
            await session.scalars(
                select(ProjectRoleGrant)
                .where(ProjectRoleGrant.project_id == project_id)
                .order_by(ProjectRoleGrant.created_at)
            )
        ).all()

    # -------------------------------------------------------------- 内部
    async def _require_user(self, session: AsyncSession, user_id: uuid.UUID) -> User:
        user = await session.get(User, user_id)
        if user is None:
            raise ResourceNotFound("调用者身份不存在")
        return user

    async def _resolve_user(
        self, session: AsyncSession, *, subject: str, display_name: str
    ) -> User:
        user = await session.scalar(select(User).where(User.subject == subject))
        if user is not None:
            return user
        user = User(subject=subject, display_name=display_name, status=UserStatus.ACTIVE)
        session.add(user)
        await session.flush()
        return user

    async def _require_membership(
        self, session: AsyncSession, *, project_id: uuid.UUID, user_id: uuid.UUID
    ) -> ProjectMembership:
        membership = await session.scalar(
            select(ProjectMembership).where(
                ProjectMembership.project_id == project_id,
                ProjectMembership.user_id == user_id,
            )
        )
        if membership is None:
            raise ResourceNotFound("该用户不是本项目成员，无法授予职责槽")
        return membership

    async def _revoke_all_harness_roles(
        self, session: AsyncSession, *, project_id: uuid.UUID, user_id: uuid.UUID
    ) -> list[str]:
        roles = (
            await session.scalars(
                select(ProjectRoleGrant.role).where(
                    ProjectRoleGrant.project_id == project_id,
                    ProjectRoleGrant.user_id == user_id,
                )
            )
        ).all()
        await session.execute(
            delete(ProjectRoleGrant).where(
                ProjectRoleGrant.project_id == project_id,
                ProjectRoleGrant.user_id == user_id,
            )
        )
        return [role.value for role in roles]
