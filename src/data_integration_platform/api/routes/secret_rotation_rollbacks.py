"""Secret 轮换回退接口（SPEC §6.1：4 个端点 / FR-20）。"""

import uuid

from fastapi import APIRouter, Depends, status
from sqlalchemy.ext.asyncio import AsyncSession

from data_integration_platform.api.dependencies import ActorId, TraceId
from data_integration_platform.api.schemas import (
    CreateSecretRotationRollbackRequest,
    HarnessDecisionRequest,
    SecretRotationRollbackRequestResponse,
)
from data_integration_platform.application.secret_rotation_rollbacks import (
    SecretRotationRollbackService,
)
from data_integration_platform.celery_app import dispatch
from data_integration_platform.domain.harness import HarnessApprovalValue
from data_integration_platform.infrastructure.database import get_session
from data_integration_platform.secret_rotation_rollback_tasks import (
    execute_secret_rotation_rollback,
)

router = APIRouter(tags=["secret-rotation-rollbacks"])


@router.post(
    "/secret-rotation-rollbacks",
    response_model=SecretRotationRollbackRequestResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_secret_rotation_rollback(
    request: CreateSecretRotationRollbackRequest,
    actor: ActorId,
    trace_id: TraceId,
    session: AsyncSession = Depends(get_session),
) -> SecretRotationRollbackRequestResponse:
    """发起回退申请；超出保留期会被拒绝，需改为重新轮换。"""
    async with session.begin():
        row = await SecretRotationRollbackService().request(
            session,
            rotation_request_id=request.rotation_request_id,
            actor_id=actor,
            reason=request.reason,
            trace_id=trace_id,
        )
    return SecretRotationRollbackRequestResponse.model_validate(row)


@router.get(
    "/secret-rotation-rollbacks/{request_id}",
    response_model=SecretRotationRollbackRequestResponse,
)
async def get_secret_rotation_rollback(
    request_id: uuid.UUID,
    actor: ActorId,
    session: AsyncSession = Depends(get_session),
) -> SecretRotationRollbackRequestResponse:
    row = await SecretRotationRollbackService().get(session, request_id=request_id, actor_id=actor)
    return SecretRotationRollbackRequestResponse.model_validate(row)


@router.get(
    "/connections/{connection_id}/secret-rotation-rollbacks",
    response_model=list[SecretRotationRollbackRequestResponse],
)
async def list_secret_rotation_rollbacks(
    connection_id: uuid.UUID,
    actor: ActorId,
    session: AsyncSession = Depends(get_session),
) -> list[SecretRotationRollbackRequestResponse]:
    rows = await SecretRotationRollbackService().list_for_connection(
        session, connection_id=connection_id, actor_id=actor
    )
    return [SecretRotationRollbackRequestResponse.model_validate(row) for row in rows]


@router.post(
    "/secret-rotation-rollbacks/{request_id}/decisions",
    response_model=SecretRotationRollbackRequestResponse,
)
async def decide_secret_rotation_rollback(
    request_id: uuid.UUID,
    request: HarnessDecisionRequest,
    actor: ActorId,
    trace_id: TraceId,
    session: AsyncSession = Depends(get_session),
) -> SecretRotationRollbackRequestResponse:
    """具名审批；通过后投递回退执行任务。"""
    service = SecretRotationRollbackService()
    async with session.begin():
        row = await service.decide(
            session,
            request_id=request_id,
            actor_id=actor,
            decision=request.decision,
            reason=request.reason,
            trace_id=trace_id,
        )
        snapshot = SecretRotationRollbackRequestResponse.model_validate(row)
    if request.decision is HarnessApprovalValue.APPROVE:
        await dispatch(execute_secret_rotation_rollback.delay, str(request_id))
    return snapshot
