"""Secret 轮换：暂存 → 验证 → 激活（T-15 / FR-19 / FR-23）。

阶段划分刻意按「快慢」而不是「步骤数」：
- **暂存同步做**：把新凭据交给 Provider 加密落库是本地操作（或一次 Vault 写入），
  失败就让申请不成立，不留半成品记录；
- **验证异步做**：用新凭据真连一次数据源是外部调用，必须在 Worker 里跑（SPEC §10.3）；
- **激活异步做**：切换连接引用并重算有效期，同样在 Worker 里完成。

激活前必须验证通过（FR-19 明确要求），因此 `WAITING_APPROVAL` 只能由验证成功的路径进入，
审批人是安全审批槽且不得是申请人（SOD-01）。
"""

import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from data_integration_platform.application.access import (
    require_project_roles,
    require_security_approver,
)
from data_integration_platform.application.audit import record_audit_event
from data_integration_platform.application.connections import READABLE, WRITABLE
from data_integration_platform.application.credential_alerts import resolve_open_alerts
from data_integration_platform.application.masking import scrub_secrets
from data_integration_platform.application.probing import CredentialVerifier
from data_integration_platform.application.secret_governance import (
    OperationOutcome,
    local_secret_id,
    require_independent_approval,
    require_status,
)
from data_integration_platform.application.secrets import ConnectionCredentials
from data_integration_platform.config import get_settings
from data_integration_platform.domain.enums import AuditEventType, SecretRotationStatus
from data_integration_platform.domain.errors import DomainError, ResourceNotFound
from data_integration_platform.domain.harness import HarnessApprovalValue
from data_integration_platform.infrastructure.models import DataConnection, SecretRotationRequest
from data_integration_platform.infrastructure.secret_factory import (
    provider_name_of,
    secret_provider_for_ref,
)

_VALIDATING = frozenset({SecretRotationStatus.VALIDATING})
_WAITING = frozenset({SecretRotationStatus.WAITING_APPROVAL})
_APPROVED = frozenset({SecretRotationStatus.APPROVED})


class SecretRotationService:
    async def request(
        self,
        session: AsyncSession,
        *,
        connection_id: uuid.UUID,
        actor_id: uuid.UUID,
        new_credentials: ConnectionCredentials,
        reason: str,
        trace_id: str | None = None,
    ) -> SecretRotationRequest:
        """受理轮换申请：把新凭据暂存起来，状态置 `VALIDATING` 等待异步验证。"""
        connection = await self._require_connection(session, connection_id)
        await require_project_roles(
            session,
            project_id=connection.project_id,
            actor_id=actor_id,
            allowed=WRITABLE,
            write=True,
        )
        if not connection.secret_ref:
            raise DomainError("该连接没有受管凭据，无法轮换")

        request = SecretRotationRequest(
            connection_id=connection.id,
            secret_id=await local_secret_id(session, connection.secret_ref),
            status=SecretRotationStatus.VALIDATING,
            previous_secret_ref=connection.secret_ref,
            # 激活时会把连接的签发时间改成新凭据的起点，所以旧值必须先留档，
            # 否则回退后无从还原，到期告警要么误报要么被静默关闭。
            previous_credential_issued_at=connection.credential_issued_at,
            reason=reason[:2000],
            requested_by=actor_id,
        )
        session.add(request)
        await session.flush()

        # 暂存凭据由 Provider 落库，轮换表只存引用；此处失败即申请不成立（整体回滚）。
        staged = await secret_provider_for_ref(session, connection.secret_ref).store(
            project_id=connection.project_id,
            name=f"{connection.name}-rotation-{str(request.id)[:8]}",
            credentials=new_credentials,
        )
        request.staged_secret_ref = staged.secret_ref
        await session.flush()
        record_audit_event(
            session,
            event_type=AuditEventType.SECRET_ROTATION_REQUESTED,
            target_type="secret_rotation_request",
            project_id=connection.project_id,
            actor_id=actor_id,
            target_id=str(request.id),
            change_summary={
                "connection_id": str(connection.id),
                "provider": provider_name_of(connection.secret_ref),
                "staged": True,
            },
            trace_id=trace_id,
        )
        return request

    async def validate(
        self,
        session: AsyncSession,
        *,
        request_id: uuid.UUID,
        connectivity: CredentialVerifier,
    ) -> OperationOutcome:
        """用暂存凭据做真实连通性验证；不通过就停在 `FAILED`，绝不进入审批。

        `connectivity` 是 `ProbingService`（注入以便测试）：调用方负责构造它。
        """
        request = await self._require(session, request_id)
        require_status(request.status, _VALIDATING, action="验证轮换暂存凭据")
        connection = await self._require_connection(session, request.connection_id)
        if not request.staged_secret_ref:
            return await self._fail(session, request, "轮换申请缺少暂存凭据引用")

        try:
            credentials = await secret_provider_for_ref(session, request.staged_secret_ref).load(
                request.staged_secret_ref
            )
        except DomainError as exc:
            return await self._fail(session, request, str(exc))

        outcome = await connectivity.verify_credentials(
            connection=connection, credentials=credentials
        )
        if not outcome.succeeded:
            return await self._fail(session, request, outcome.reason or "暂存凭据验证未通过")

        request.validated_at = datetime.now(UTC)
        request.status = SecretRotationStatus.WAITING_APPROVAL
        await session.flush()
        record_audit_event(
            session,
            event_type=AuditEventType.SECRET_ROTATION_VALIDATED,
            target_type="secret_rotation_request",
            project_id=connection.project_id,
            target_id=str(request.id),
            change_summary={
                "validated": True,
                "provider": provider_name_of(request.staged_secret_ref),
            },
        )
        return OperationOutcome.success()

    async def decide(
        self,
        session: AsyncSession,
        *,
        request_id: uuid.UUID,
        actor_id: uuid.UUID,
        decision: HarnessApprovalValue,
        reason: str,
        trace_id: str | None = None,
    ) -> SecretRotationRequest:
        """具名审批；只能在**验证通过之后**审批（`WAITING_APPROVAL`）。"""
        request = await self._require(session, request_id)
        connection = await self._require_connection(session, request.connection_id)
        require_status(request.status, _WAITING, action="审批轮换申请")
        require_independent_approval(requested_by=request.requested_by, approver_id=actor_id)
        await require_security_approver(
            session, project_id=connection.project_id, actor_id=actor_id
        )
        request.status = (
            SecretRotationStatus.APPROVED
            if decision is HarnessApprovalValue.APPROVE
            else SecretRotationStatus.REJECTED
        )
        request.decided_by = actor_id
        request.decided_at = datetime.now(UTC)
        request.reason = reason[:2000]
        await session.flush()
        record_audit_event(
            session,
            event_type=AuditEventType.SECRET_ROTATION_DECIDED,
            target_type="secret_rotation_request",
            project_id=connection.project_id,
            actor_id=actor_id,
            target_id=str(request.id),
            change_summary={"decision": decision.value, "reason": reason[:200]},
            trace_id=trace_id,
        )
        return request

    async def activate(self, session: AsyncSession, *, request_id: uuid.UUID) -> OperationOutcome:
        """激活：把连接切换到暂存凭据，重算有效期并开启回退窗口。"""
        request = await self._require(session, request_id)
        require_status(request.status, _APPROVED, action="激活轮换")
        connection = await self._require_connection(session, request.connection_id)
        if not request.staged_secret_ref:
            return await self._fail(session, request, "轮换申请缺少暂存凭据引用")

        request.status = SecretRotationStatus.ACTIVATING
        await session.flush()
        now = datetime.now(UTC)
        settings = get_settings()
        try:
            # 激活前再确认暂存凭据仍可读：审批期间它可能已被销毁或不可用。
            await secret_provider_for_ref(session, request.staged_secret_ref).load(
                request.staged_secret_ref
            )
        except DomainError as exc:
            return await self._fail(session, request, str(exc))

        connection.secret_ref = request.staged_secret_ref
        connection.credential_issued_at = now
        request.activated_at = now
        request.rollback_deadline_at = now + timedelta(days=settings.secret_rotation_rollback_days)
        request.status = SecretRotationStatus.SUCCEEDED
        request.executed_at = now
        await session.flush()

        # 凭据已换新，该连接上未解决的到期告警应当关闭 —— 否则会一直重复提醒。
        resolved = await resolve_open_alerts(
            session, connection_id=connection.id, note="凭据已轮换并激活"
        )
        record_audit_event(
            session,
            event_type=AuditEventType.SECRET_ROTATION_ACTIVATED,
            target_type="secret_rotation_request",
            project_id=connection.project_id,
            target_id=str(request.id),
            change_summary={
                "rollback_deadline_at": request.rollback_deadline_at.isoformat(),
                "resolved_alerts": resolved,
            },
        )
        return OperationOutcome.success()

    async def get(
        self, session: AsyncSession, *, request_id: uuid.UUID, actor_id: uuid.UUID
    ) -> SecretRotationRequest:
        request = await self._require(session, request_id)
        connection = await self._require_connection(session, request.connection_id)
        await require_project_roles(
            session, project_id=connection.project_id, actor_id=actor_id, allowed=READABLE
        )
        return request

    async def list_for_connection(
        self, session: AsyncSession, *, connection_id: uuid.UUID, actor_id: uuid.UUID
    ) -> list[SecretRotationRequest]:
        connection = await self._require_connection(session, connection_id)
        await require_project_roles(
            session, project_id=connection.project_id, actor_id=actor_id, allowed=READABLE
        )
        rows = await session.scalars(
            select(SecretRotationRequest)
            .where(SecretRotationRequest.connection_id == connection_id)
            .order_by(SecretRotationRequest.created_at.desc())
        )
        return list(rows)

    async def _fail(
        self, session: AsyncSession, request: SecretRotationRequest, reason: str
    ) -> OperationOutcome:
        request.status = SecretRotationStatus.FAILED
        request.failure_reason = scrub_secrets(reason)[:2000]
        request.executed_at = datetime.now(UTC)
        await session.flush()
        return OperationOutcome.failure(request.failure_reason or reason)

    async def _require(self, session: AsyncSession, request_id: uuid.UUID) -> SecretRotationRequest:
        request = await session.get(SecretRotationRequest, request_id)
        if request is None:
            raise ResourceNotFound("轮换申请不存在")
        return request

    async def _require_connection(
        self, session: AsyncSession, connection_id: uuid.UUID
    ) -> DataConnection:
        connection = await session.get(DataConnection, connection_id)
        if connection is None:
            raise ResourceNotFound("数据连接不存在")
        return connection
