"""Secret 销毁（T-15 / FR-21 / FR-23）。

销毁是**不可逆**动作，因此这里刻意不做「调用方指定要删哪个引用」的接口：
调用方只能声明「销毁哪次轮换的**源凭据**」或「销毁哪次轮换**已回退的暂存凭据**」，
实际引用由平台从轮换记录推导，并逐条证明它当前**没有被任何连接在用**。

五条放行条件，任一不成立即拒绝：
1. 轮换记录存在且属于该连接；
2. 销毁源凭据：该轮换必须已成功激活（否则旧凭据可能还在用）；
3. 销毁已回退暂存凭据：该轮换必须已成功回退（否则暂存凭据可能已激活在用）；
4. 待销毁引用必须与该连接**当前使用**的引用不同；
5. 执行阶段重新校验 2~4（申请与执行之间状态可能已变）。
"""

import uuid
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from data_integration_platform.application.access import (
    require_project_roles,
    require_security_approver,
)
from data_integration_platform.application.audit import record_audit_event
from data_integration_platform.application.connections import READABLE, WRITABLE
from data_integration_platform.application.masking import scrub_secrets
from data_integration_platform.application.secret_governance import (
    OperationOutcome,
    local_secret_id,
    require_independent_approval,
    require_status,
)
from data_integration_platform.domain.enums import (
    AuditEventType,
    SecretDestructionStatus,
    SecretDestructionTarget,
    SecretRotationRollbackStatus,
    SecretRotationStatus,
)
from data_integration_platform.domain.errors import DomainError, ResourceNotFound
from data_integration_platform.domain.harness import HarnessApprovalValue
from data_integration_platform.infrastructure.models import (
    DataConnection,
    SecretDestructionRequest,
    SecretRotationRequest,
    SecretRotationRollbackRequest,
)
from data_integration_platform.infrastructure.secret_factory import (
    provider_name_of,
    secret_provider_for_ref,
)

_PENDING = frozenset({SecretDestructionStatus.PENDING})
_APPROVED = frozenset({SecretDestructionStatus.APPROVED})


class SecretDestructionService:
    async def request(
        self,
        session: AsyncSession,
        *,
        connection_id: uuid.UUID,
        actor_id: uuid.UUID,
        target: SecretDestructionTarget,
        rotation_request_id: uuid.UUID,
        reason: str,
        trace_id: str | None = None,
    ) -> SecretDestructionRequest:
        connection = await self._require_connection(session, connection_id)
        await require_project_roles(
            session,
            project_id=connection.project_id,
            actor_id=actor_id,
            allowed=WRITABLE,
            write=True,
        )
        resolved = await self._resolve_ref(
            session,
            connection=connection,
            target=target,
            rotation_request_id=rotation_request_id,
        )
        request = SecretDestructionRequest(
            connection_id=connection.id,
            secret_id=await local_secret_id(session, resolved),
            target=target,
            source_rotation_request_id=rotation_request_id,
            status=SecretDestructionStatus.PENDING,
            reason=reason[:2000],
            requested_by=actor_id,
        )
        session.add(request)
        await session.flush()
        record_audit_event(
            session,
            event_type=AuditEventType.SECRET_DESTRUCTION_REQUESTED,
            target_type="secret_destruction_request",
            project_id=connection.project_id,
            actor_id=actor_id,
            target_id=str(request.id),
            change_summary={
                "target": target.value,
                "rotation_request_id": str(rotation_request_id),
                # 只记引用前缀（Provider 名），不记完整引用。
                "ref_provider": provider_name_of(resolved),
            },
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
        trace_id: str | None = None,
    ) -> SecretDestructionRequest:
        request = await self._require(session, request_id)
        connection = await self._require_connection(session, request.connection_id)
        require_status(request.status, _PENDING, action="审批销毁申请")
        require_independent_approval(requested_by=request.requested_by, approver_id=actor_id)
        await require_security_approver(
            session, project_id=connection.project_id, actor_id=actor_id
        )
        request.status = (
            SecretDestructionStatus.APPROVED
            if decision is HarnessApprovalValue.APPROVE
            else SecretDestructionStatus.REJECTED
        )
        request.decided_by = actor_id
        request.decided_at = datetime.now(UTC)
        request.reason = reason[:2000]
        await session.flush()
        record_audit_event(
            session,
            event_type=AuditEventType.SECRET_DESTRUCTION_DECIDED,
            target_type="secret_destruction_request",
            project_id=connection.project_id,
            actor_id=actor_id,
            target_id=str(request.id),
            change_summary={"decision": decision.value, "reason": reason[:200]},
            trace_id=trace_id,
        )
        return request

    async def execute(self, session: AsyncSession, *, request_id: uuid.UUID) -> OperationOutcome:
        request = await self._require(session, request_id)
        require_status(request.status, _APPROVED, action="执行销毁")
        connection = await self._require_connection(session, request.connection_id)
        if request.source_rotation_request_id is None:
            return await self._fail(session, request, "销毁申请缺少来源轮换记录")

        request.status = SecretDestructionStatus.EXECUTING
        await session.flush()
        try:
            # 执行前重新求证：申请与执行之间连接可能已经换了凭据。
            resolved = await self._resolve_ref(
                session,
                connection=connection,
                target=request.target,
                rotation_request_id=request.source_rotation_request_id,
            )
            await secret_provider_for_ref(session, resolved).destroy(resolved)
        except DomainError as exc:
            return await self._fail(session, request, str(exc))
        except Exception as exc:
            return await self._fail(session, request, f"{type(exc).__name__}: {exc}")

        request.status = SecretDestructionStatus.SUCCEEDED
        request.executed_at = datetime.now(UTC)
        await session.flush()
        record_audit_event(
            session,
            event_type=AuditEventType.SECRET_DESTRUCTION_EXECUTED,
            target_type="secret_destruction_request",
            project_id=connection.project_id,
            target_id=str(request.id),
            change_summary={"target": request.target.value},
        )
        return OperationOutcome.success()

    async def get(
        self, session: AsyncSession, *, request_id: uuid.UUID, actor_id: uuid.UUID
    ) -> SecretDestructionRequest:
        request = await self._require(session, request_id)
        connection = await self._require_connection(session, request.connection_id)
        await require_project_roles(
            session, project_id=connection.project_id, actor_id=actor_id, allowed=READABLE
        )
        return request

    async def list_for_connection(
        self, session: AsyncSession, *, connection_id: uuid.UUID, actor_id: uuid.UUID
    ) -> list[SecretDestructionRequest]:
        connection = await self._require_connection(session, connection_id)
        await require_project_roles(
            session, project_id=connection.project_id, actor_id=actor_id, allowed=READABLE
        )
        rows = await session.scalars(
            select(SecretDestructionRequest)
            .where(SecretDestructionRequest.connection_id == connection_id)
            .order_by(SecretDestructionRequest.created_at.desc())
        )
        return list(rows)

    async def _resolve_ref(
        self,
        session: AsyncSession,
        *,
        connection: DataConnection,
        target: SecretDestructionTarget,
        rotation_request_id: uuid.UUID,
    ) -> str:
        """推导待销毁的引用，并逐条证明它当前没有被使用。"""
        rotation = await session.get(SecretRotationRequest, rotation_request_id)
        if rotation is None or rotation.connection_id != connection.id:
            raise ResourceNotFound("该连接下不存在指定的轮换记录")

        if target is SecretDestructionTarget.ROTATION_SOURCE:
            if rotation.status is not SecretRotationStatus.SUCCEEDED:
                raise DomainError("轮换尚未成功激活，源凭据可能仍在使用，不允许销毁")
            candidate = rotation.previous_secret_ref
        else:
            rolled_back = await session.scalar(
                select(SecretRotationRollbackRequest.id).where(
                    SecretRotationRollbackRequest.rotation_request_id == rotation_request_id,
                    SecretRotationRollbackRequest.status == SecretRotationRollbackStatus.SUCCEEDED,
                )
            )
            if rolled_back is None:
                raise DomainError("该轮换没有成功的回退记录，暂存凭据可能已激活在用，不允许销毁")
            if not rotation.staged_secret_ref:
                raise DomainError("该轮换没有暂存凭据引用")
            candidate = rotation.staged_secret_ref

        if connection.secret_ref == candidate:
            raise DomainError("待销毁的凭据正是该连接当前使用的凭据，拒绝销毁")
        return candidate

    async def _fail(
        self, session: AsyncSession, request: SecretDestructionRequest, reason: str
    ) -> OperationOutcome:
        request.status = SecretDestructionStatus.FAILED
        request.failure_reason = scrub_secrets(reason)[:2000]
        request.executed_at = datetime.now(UTC)
        await session.flush()
        return OperationOutcome.failure(request.failure_reason or reason)

    async def _require(
        self, session: AsyncSession, request_id: uuid.UUID
    ) -> SecretDestructionRequest:
        request = await session.get(SecretDestructionRequest, request_id)
        if request is None:
            raise ResourceNotFound("销毁申请不存在")
        return request

    async def _require_connection(
        self, session: AsyncSession, connection_id: uuid.UUID
    ) -> DataConnection:
        connection = await session.get(DataConnection, connection_id)
        if connection is None:
            raise ResourceNotFound("数据连接不存在")
        return connection
