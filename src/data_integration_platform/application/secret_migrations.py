"""Secret Provider 迁移（T-15 / FR-18 / FR-23）。

迁移的语义是**换存放位置，不换凭据内容**：
- 源凭据**不销毁**（`source_retained=True`），且迁移前的引用留档在 `source_secret_ref`；
- 因此「可回退」= 源凭据仍在、引用可查，需要时再迁回去，而不是凭空造一条回退记录；
- 迁移不改变凭据内容，所以连接的连通性结论**保持有效**，不重置为「未测试」。
"""

import uuid
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from data_integration_platform.application.access import require_project_roles
from data_integration_platform.application.audit import record_audit_event
from data_integration_platform.application.connections import READABLE, WRITABLE
from data_integration_platform.application.masking import scrub_secrets
from data_integration_platform.application.secret_governance import (
    OperationOutcome,
    local_secret_id,
    require_status,
)
from data_integration_platform.application.side_effect_runtime import (
    approval_verdict,
    begin_execution,
    decide_for,
    preparation_id_for,
    prepare_for,
    settle,
    worker_owner,
)
from data_integration_platform.domain.enums import AuditEventType, SecretMigrationStatus
from data_integration_platform.domain.errors import DomainError, ResourceNotFound
from data_integration_platform.domain.harness import HarnessApprovalValue, HarnessRole
from data_integration_platform.domain.side_effects import SideEffectOperation
from data_integration_platform.infrastructure.models import DataConnection, SecretMigrationRequest
from data_integration_platform.infrastructure.secret_factory import (
    build_secret_provider,
    provider_name_of,
    secret_provider_for_ref,
)

_PENDING = frozenset({SecretMigrationStatus.PENDING})
_APPROVED = frozenset({SecretMigrationStatus.APPROVED})


class SecretMigrationService:
    async def request(
        self,
        session: AsyncSession,
        *,
        connection_id: uuid.UUID,
        actor_id: uuid.UUID,
        target_provider: str,
        reason: str,
        trace_id: str | None = None,
    ) -> SecretMigrationRequest:
        """发起迁移申请：只落待审事实，**不碰任何凭据**。"""
        connection = await self._require_connection(session, connection_id)
        await require_project_roles(
            session,
            project_id=connection.project_id,
            actor_id=actor_id,
            allowed=WRITABLE,
            write=True,
        )
        if not connection.secret_ref:
            raise DomainError("该连接没有受管凭据，无需迁移")
        source_provider = provider_name_of(connection.secret_ref)
        if source_provider == target_provider:
            raise DomainError(f"目标 Provider 与当前一致（{target_provider}），无需迁移")
        # 目标 Provider 未就绪就在这里失败，不把问题推迟到执行阶段。
        build_secret_provider(session, provider_name=target_provider)

        request = SecretMigrationRequest(
            connection_id=connection.id,
            secret_id=await local_secret_id(session, connection.secret_ref),
            source_provider=source_provider,
            source_secret_ref=connection.secret_ref,
            target_provider=target_provider,
            source_retained=True,
            status=SecretMigrationStatus.PENDING,
            reason=reason[:2000],
            requested_by=actor_id,
        )
        session.add(request)
        await session.flush()
        record_audit_event(
            session,
            event_type=AuditEventType.SECRET_MIGRATION_REQUESTED,
            target_type="secret_migration_request",
            project_id=connection.project_id,
            actor_id=actor_id,
            target_id=str(request.id),
            change_summary={
                "connection_id": str(connection.id),
                "source_provider": source_provider,
                "target_provider": target_provider,
            },
            trace_id=trace_id,
        )
        # 受管入口（FR-57）：申请落库的同时生成副作用准备单，**零外部副作用**。
        await prepare_for(
            session,
            operation=SideEffectOperation.SECRET_MIGRATION,
            aggregate_id=str(request.id),
            actor_id=actor_id,
            trace_id=trace_id,
        )
        return request

    async def decide(
        self,
        session: AsyncSession,
        *,
        request_id: uuid.UUID,
        actor_id: uuid.UUID,
        decision: HarnessApprovalValue,
        reason: str,
        role: HarnessRole | None = None,
        trace_id: str | None = None,
    ) -> SecretMigrationRequest:
        """具名审批：安全审批槽 + 申请人不得自批（SOD-01）。

        权限、职责槽、自批与同槽冲突**只在受管入口判一次**；这里保留业务状态机的
        前置检查，两者管的是不同的事（那边管「这次申请还作不作数」，这边管「谁有权批」）。
        """
        request = await self._require(session, request_id)
        connection = await self._require_connection(session, request.connection_id)
        require_status(request.status, _PENDING, action="审批迁移申请")
        preparation = await decide_for(
            session,
            operation=SideEffectOperation.SECRET_MIGRATION,
            aggregate_id=str(request.id),
            actor_id=actor_id,
            role=role,
            decision=decision,
            reason=reason,
            trace_id=trace_id,
        )
        verdict = approval_verdict(preparation)
        if verdict is not None:
            # 槽位没齐时业务行留在 PENDING：投了一票不等于批准。
            request.status = (
                SecretMigrationStatus.APPROVED if verdict else SecretMigrationStatus.REJECTED
            )
            request.decided_by = actor_id
            request.decided_at = datetime.now(UTC)
        request.reason = reason[:2000]
        await session.flush()
        record_audit_event(
            session,
            event_type=AuditEventType.SECRET_MIGRATION_DECIDED,
            target_type="secret_migration_request",
            project_id=connection.project_id,
            actor_id=actor_id,
            target_id=str(request.id),
            change_summary={"decision": decision.value, "reason": reason[:200]},
            trace_id=trace_id,
        )
        return request

    async def execute(self, session: AsyncSession, *, request_id: uuid.UUID) -> OperationOutcome:
        """执行迁移：源 → 目标，保留源凭据，切换连接引用。

        执行前先过受管入口的三道闸（取租约 → 验签 → 重算事实）。取不到准备单就
        直接拒绝 —— 没有受管申请单的迁移在这里走不下去（FR-23）。
        """
        owner = worker_owner()
        preparation_id = await preparation_id_for(
            session,
            operation=SideEffectOperation.SECRET_MIGRATION,
            aggregate_id=str(request_id),
        )
        await begin_execution(session, preparation_id=preparation_id, owner=owner)
        outcome = await self._apply(session, request_id=request_id)
        await settle(session, preparation_id=preparation_id, owner=owner, outcome=outcome)
        return outcome

    async def _apply(self, session: AsyncSession, *, request_id: uuid.UUID) -> OperationOutcome:
        request = await self._require(session, request_id)
        require_status(request.status, _APPROVED, action="执行迁移")
        connection = await self._require_connection(session, request.connection_id)
        request.status = SecretMigrationStatus.EXECUTING
        await session.flush()

        try:
            source_ref = request.source_secret_ref or connection.secret_ref
            if not source_ref:
                raise DomainError("迁移缺少源凭据引用")
            credentials = await secret_provider_for_ref(session, source_ref).load(source_ref)
            target = build_secret_provider(session, provider_name=request.target_provider)
            stored = await target.store(
                project_id=connection.project_id,
                name=f"{connection.name}-migrated",
                credentials=credentials,
            )
        except DomainError as exc:
            return await self._fail(session, request, str(exc))
        except Exception as exc:  # 驱动/网络层漏网异常，同样落成失败事实
            return await self._fail(session, request, f"{type(exc).__name__}: {exc}")

        request.target_secret_ref = stored.secret_ref
        request.status = SecretMigrationStatus.SUCCEEDED
        request.executed_at = datetime.now(UTC)
        # 只换引用，不动凭据内容 —— 连通性结论仍然成立，因此不重置连接状态。
        connection.secret_ref = stored.secret_ref
        await session.flush()
        record_audit_event(
            session,
            event_type=AuditEventType.SECRET_MIGRATION_EXECUTED,
            target_type="secret_migration_request",
            project_id=connection.project_id,
            target_id=str(request.id),
            change_summary={
                "source_provider": request.source_provider,
                "target_provider": request.target_provider,
                "source_retained": request.source_retained,
            },
        )
        return OperationOutcome.success()

    async def get(
        self, session: AsyncSession, *, request_id: uuid.UUID, actor_id: uuid.UUID
    ) -> SecretMigrationRequest:
        request = await self._require(session, request_id)
        connection = await self._require_connection(session, request.connection_id)
        await require_project_roles(
            session, project_id=connection.project_id, actor_id=actor_id, allowed=READABLE
        )
        return request

    async def list_for_connection(
        self, session: AsyncSession, *, connection_id: uuid.UUID, actor_id: uuid.UUID
    ) -> list[SecretMigrationRequest]:
        connection = await self._require_connection(session, connection_id)
        await require_project_roles(
            session, project_id=connection.project_id, actor_id=actor_id, allowed=READABLE
        )
        rows = await session.scalars(
            select(SecretMigrationRequest)
            .where(SecretMigrationRequest.connection_id == connection_id)
            .order_by(SecretMigrationRequest.created_at.desc())
        )
        return list(rows)

    async def _fail(
        self, session: AsyncSession, request: SecretMigrationRequest, reason: str
    ) -> OperationOutcome:
        request.status = SecretMigrationStatus.FAILED
        request.failure_reason = scrub_secrets(reason)[:2000]
        request.executed_at = datetime.now(UTC)
        await session.flush()
        return OperationOutcome.failure(request.failure_reason or reason)

    async def _require(
        self, session: AsyncSession, request_id: uuid.UUID
    ) -> SecretMigrationRequest:
        request = await session.get(SecretMigrationRequest, request_id)
        if request is None:
            raise ResourceNotFound("迁移申请不存在")
        return request

    async def _require_connection(
        self, session: AsyncSession, connection_id: uuid.UUID
    ) -> DataConnection:
        connection = await session.get(DataConnection, connection_id)
        if connection is None:
            raise ResourceNotFound("数据连接不存在")
        return connection
