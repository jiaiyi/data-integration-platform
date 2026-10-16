"""Secret 轮换接口（SPEC §6.1：4 个端点 / FR-19）。

发起时同步暂存新凭据并投递验证任务；**验证通过前不会进入审批**，
审批通过后再投递激活任务。
"""

import uuid

from fastapi import APIRouter, Depends, status
from sqlalchemy.ext.asyncio import AsyncSession

from data_integration_platform.api.dependencies import ActorId, TraceId
from data_integration_platform.api.schemas import (
    CreateSecretRotationRequest,
    HarnessDecisionRequest,
    SecretRotationRequestResponse,
)
from data_integration_platform.application.secret_rotations import SecretRotationService
from data_integration_platform.application.secrets import ConnectionCredentials
from data_integration_platform.celery_app import dispatch
from data_integration_platform.domain.harness import HarnessApprovalValue
from data_integration_platform.infrastructure.database import get_session
from data_integration_platform.secret_rotation_tasks import (
    activate_secret_rotation,
    validate_secret_rotation,
)

router = APIRouter(tags=["secret-rotations"])


@router.post(
    "/secret-rotations",
    response_model=SecretRotationRequestResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_secret_rotation(
    request: CreateSecretRotationRequest,
    actor: ActorId,
    trace_id: TraceId,
    session: AsyncSession = Depends(get_session),
) -> SecretRotationRequestResponse:
    """受理轮换申请：暂存新凭据（同步）并投递连通性验证（异步）。"""
    credentials = ConnectionCredentials.model_validate(request.new_credentials.model_dump())
    async with session.begin():
        row = await SecretRotationService().request(
            session,
            connection_id=request.connection_id,
            actor_id=actor,
            new_credentials=credentials,
            reason=request.reason,
            trace_id=trace_id,
        )
        request_id = row.id
        snapshot = SecretRotationRequestResponse.model_validate(row)
    await dispatch(validate_secret_rotation.delay, str(request_id))
    return snapshot


@router.get("/secret-rotations/{request_id}", response_model=SecretRotationRequestResponse)
async def get_secret_rotation(
    request_id: uuid.UUID,
    actor: ActorId,
    session: AsyncSession = Depends(get_session),
) -> SecretRotationRequestResponse:
    row = await SecretRotationService().get(session, request_id=request_id, actor_id=actor)
    return SecretRotationRequestResponse.model_validate(row)


@router.get(
    "/connections/{connection_id}/secret-rotations",
    response_model=list[SecretRotationRequestResponse],
)
async def list_secret_rotations(
    connection_id: uuid.UUID,
    actor: ActorId,
    session: AsyncSession = Depends(get_session),
) -> list[SecretRotationRequestResponse]:
    rows = await SecretRotationService().list_for_connection(
        session, connection_id=connection_id, actor_id=actor
    )
    return [SecretRotationRequestResponse.model_validate(row) for row in rows]


@router.post(
    "/secret-rotations/{request_id}/decisions",
    response_model=SecretRotationRequestResponse,
)
async def decide_secret_rotation(
    request_id: uuid.UUID,
    request: HarnessDecisionRequest,
    actor: ActorId,
    trace_id: TraceId,
    session: AsyncSession = Depends(get_session),
) -> SecretRotationRequestResponse:
    """具名审批；通过后投递激活任务。状态不是「等待审批」时会被拒绝（含未验证的情况）。"""
    service = SecretRotationService()
    async with session.begin():
        row = await service.decide(
            session,
            request_id=request_id,
            actor_id=actor,
            decision=request.decision,
            reason=request.reason,
            trace_id=trace_id,
        )
        snapshot = SecretRotationRequestResponse.model_validate(row)
    if request.decision is HarnessApprovalValue.APPROVE:
        await dispatch(activate_secret_rotation.delay, str(request_id))
    return snapshot
