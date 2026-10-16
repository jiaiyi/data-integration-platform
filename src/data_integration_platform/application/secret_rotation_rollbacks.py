"""Secret 轮换回退（T-15 / FR-20 / FR-23）。

回退不是「撤销轮换」，而是**把连接切回旧凭据**：
- 轮换记录里的 `previous_secret_ref` 就是旧凭据，它一直没被销毁，因此回退只是换回引用；
- 只在保留期内允许（`rollback_deadline_at`），过期后必须重新轮换而不是回退，
  否则会悄悄回到一个早已超过最大年龄的凭据；
- 回退后，本次轮换写入的暂存凭据成为「已回退的暂存凭据」，
  这是唯一允许被销毁的暂存对象之一（FR-21）。
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
    require_rotation_rollback_window_open,
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
from data_integration_platform.domain.enums import (
    AuditEventType,
    SecretRotationRollbackStatus,
)
from data_integration_platform.domain.errors import DomainError, ResourceNotFound
from data_integration_platform.domain.harness import HarnessApprovalValue, HarnessRole
from data_integration_platform.domain.side_effects import SideEffectOperation
from data_integration_platform.infrastructure.models import (
    DataConnection,
    SecretRotationRequest,
    SecretRotationRollbackRequest,
)
from data_integration_platform.infrastructure.secret_factory import secret_provider_for_ref

_PENDING = frozenset({SecretRotationRollbackStatus.PENDING})
_APPROVED = frozenset({SecretRotationRollbackStatus.APPROVED})


class SecretRotationRollbackService:
    async def request(
        self,
        session: AsyncSession,
        *,
        rotation_request_id: uuid.UUID,
        actor_id: uuid.UUID,
        reason: str,
        trace_id: str | None = None,
    ) -> SecretRotationRollbackRequest:
        """发起回退申请；前置校验在这里做，避免把「已过窗口」的申请记录下来。"""
        rotation = await session.get(SecretRotationRequest, rotation_request_id)
        if rotation is None:
            raise ResourceNotFound("轮换申请不存在")
        connection = await self._require_connection(session, rotation.connection_id)
        await require_project_roles(
            session,
            project_id=connection.project_id,
            actor_id=actor_id,
            allowed=WRITABLE,
            write=True,
        )
        require_rotation_rollback_window_open(
            activated_at=rotation.activated_at, deadline=rotation.rollback_deadline_at
        )
        existing = await session.scalar(
            select(SecretRotationRollbackRequest.id).where(
                SecretRotationRollbackRequest.rotation_request_id == rotation_request_id,
                SecretRotationRollbackRequest.status.in_(
                    [
                        SecretRotationRollbackStatus.PENDING,
                        SecretRotationRollbackStatus.APPROVED,
                        SecretRotationRollbackStatus.EXECUTING,
                        SecretRotationRollbackStatus.SUCCEEDED,
                    ]
                ),
            )
        )
        if existing is not None:
            raise DomainError("该轮换已有生效中或已完成的回退申请")

        request = SecretRotationRollbackRequest(
            connection_id=connection.id,
            rotation_request_id=rotation_request_id,
            status=SecretRotationRollbackStatus.PENDING,
            reason=reason[:2000],
            requested_by=actor_id,
        )
        session.add(request)
        await session.flush()
        record_audit_event(
            session,
            event_type=AuditEventType.SECRET_ROTATION_ROLLBACK_REQUESTED,
            target_type="secret_rotation_rollback_request",
            project_id=connection.project_id,
            actor_id=actor_id,
            target_id=str(request.id),
            change_summary={"rotation_request_id": str(rotation_request_id)},
            trace_id=trace_id,
        )
        await prepare_for(
            session,
            operation=SideEffectOperation.SECRET_ROTATION_ROLLBACK,
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
    ) -> SecretRotationRollbackRequest:
        """具名审批；权限与职责槽只在受管入口判一次（FR-57）。"""
        request = await self._require(session, request_id)
        connection = await self._require_connection(session, request.connection_id)
        require_status(request.status, _PENDING, action="审批回退申请")
        preparation = await decide_for(
            session,
            operation=SideEffectOperation.SECRET_ROTATION_ROLLBACK,
            aggregate_id=str(request.id),
            actor_id=actor_id,
            role=role,
            decision=decision,
            reason=reason,
            trace_id=trace_id,
        )
        verdict = approval_verdict(preparation)
        if verdict is not None:
            request.status = (
                SecretRotationRollbackStatus.APPROVED
                if verdict
                else SecretRotationRollbackStatus.REJECTED
            )
            request.decided_by = actor_id
            request.decided_at = datetime.now(UTC)
        request.reason = reason[:2000]
        await session.flush()
        record_audit_event(
            session,
            event_type=AuditEventType.SECRET_ROTATION_ROLLBACK_DECIDED,
            target_type="secret_rotation_rollback_request",
            project_id=connection.project_id,
            actor_id=actor_id,
            target_id=str(request.id),
            change_summary={"decision": decision.value, "reason": reason[:200]},
            trace_id=trace_id,
        )
        return request

    async def execute(self, session: AsyncSession, *, request_id: uuid.UUID) -> OperationOutcome:
        """执行回退：把连接切回旧凭据，并恢复旧的凭据签发时间。

        业务逻辑一行没动，多出来的是受管入口的三道闸（租约 → 验签 → 事实未漂移）。
        """
        owner = worker_owner()
        preparation_id = await preparation_id_for(
            session,
            operation=SideEffectOperation.SECRET_ROTATION_ROLLBACK,
            aggregate_id=str(request_id),
        )
        await begin_execution(session, preparation_id=preparation_id, owner=owner)
        outcome = await self._apply(session, request_id=request_id)
        await settle(session, preparation_id=preparation_id, owner=owner, outcome=outcome)
        return outcome

    async def _apply(self, session: AsyncSession, *, request_id: uuid.UUID) -> OperationOutcome:
        request = await self._require(session, request_id)
        require_status(request.status, _APPROVED, action="执行回退")
        rotation = await session.get(SecretRotationRequest, request.rotation_request_id)
        if rotation is None:
            return await self._fail(session, request, "关联的轮换申请不存在")
        connection = await self._require_connection(session, request.connection_id)
        request.status = SecretRotationRollbackStatus.EXECUTING
        await session.flush()

        try:
            # 旧凭据没被销毁，直接确认它仍可读 —— 读不出来就不能切回去。
            await secret_provider_for_ref(session, rotation.previous_secret_ref).load(
                rotation.previous_secret_ref
            )
        except DomainError as exc:
            return await self._fail(session, request, str(exc))
        except Exception as exc:
            return await self._fail(session, request, f"{type(exc).__name__}: {exc}")

        now = datetime.now(UTC)
        connection.secret_ref = rotation.previous_secret_ref
        # 还原旧凭据的签发时间（轮换时留档），使到期告警继续基于真实事实判断。
        connection.credential_issued_at = rotation.previous_credential_issued_at
        request.status = SecretRotationRollbackStatus.SUCCEEDED
        request.executed_at = now
        await session.flush()
        record_audit_event(
            session,
            event_type=AuditEventType.SECRET_ROTATION_ROLLBACK_EXECUTED,
            target_type="secret_rotation_rollback_request",
            project_id=connection.project_id,
            target_id=str(request.id),
            change_summary={
                "rotation_request_id": str(rotation.id),
                "restored_ref_provider": rotation.previous_secret_ref.split("://", 1)[0],
            },
        )
        return OperationOutcome.success()

    async def get(
        self, session: AsyncSession, *, request_id: uuid.UUID, actor_id: uuid.UUID
    ) -> SecretRotationRollbackRequest:
        request = await self._require(session, request_id)
        connection = await self._require_connection(session, request.connection_id)
        await require_project_roles(
            session, project_id=connection.project_id, actor_id=actor_id, allowed=READABLE
        )
        return request

    async def list_for_connection(
        self, session: AsyncSession, *, connection_id: uuid.UUID, actor_id: uuid.UUID
    ) -> list[SecretRotationRollbackRequest]:
        connection = await self._require_connection(session, connection_id)
        await require_project_roles(
            session, project_id=connection.project_id, actor_id=actor_id, allowed=READABLE
        )
        rows = await session.scalars(
            select(SecretRotationRollbackRequest)
            .where(SecretRotationRollbackRequest.connection_id == connection_id)
            .order_by(SecretRotationRollbackRequest.created_at.desc())
        )
        return list(rows)

    async def _fail(
        self, session: AsyncSession, request: SecretRotationRollbackRequest, reason: str
    ) -> OperationOutcome:
        request.status = SecretRotationRollbackStatus.FAILED
        request.failure_reason = scrub_secrets(reason)[:2000]
        request.executed_at = datetime.now(UTC)
        await session.flush()
        return OperationOutcome.failure(request.failure_reason or reason)

    async def _require(
        self, session: AsyncSession, request_id: uuid.UUID
    ) -> SecretRotationRollbackRequest:
        request = await session.get(SecretRotationRollbackRequest, request_id)
        if request is None:
            raise ResourceNotFound("回退申请不存在")
        return request

    async def _require_connection(
        self, session: AsyncSession, connection_id: uuid.UUID
    ) -> DataConnection:
        connection = await session.get(DataConnection, connection_id)
        if connection is None:
            raise ResourceNotFound("数据连接不存在")
        return connection
