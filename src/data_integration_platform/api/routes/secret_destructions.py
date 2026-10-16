"""Secret 销毁接口（SPEC §6.1：4 个端点 / FR-21）。

调用方只能声明「销毁哪次轮换的源凭据或已回退暂存凭据」，
具体引用由平台推导并证明其未被使用 —— 不提供「删除任意引用」的入口。
"""

import uuid

from fastapi import APIRouter, Depends, status
from sqlalchemy.ext.asyncio import AsyncSession

from data_integration_platform.api.dependencies import ActorId, TraceId
from data_integration_platform.api.schemas import (
    CreateSecretDestructionRequest,
    HarnessDecisionRequest,
    SecretDestructionRequestResponse,
)
from data_integration_platform.application.secret_destructions import SecretDestructionService
from data_integration_platform.celery_app import dispatch
from data_integration_platform.domain.enums import SecretDestructionStatus
from data_integration_platform.infrastructure.database import get_session
from data_integration_platform.secret_destruction_tasks import execute_secret_destruction

router = APIRouter(tags=["secret-destructions"])


@router.post(
    "/secret-destructions",
    response_model=SecretDestructionRequestResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_secret_destruction(
    request: CreateSecretDestructionRequest,
    actor: ActorId,
    trace_id: TraceId,
    session: AsyncSession = Depends(get_session),
) -> SecretDestructionRequestResponse:
    """发起销毁申请；待销毁凭据当前若仍在使用则直接拒绝。"""
    async with session.begin():
        row = await SecretDestructionService().request(
            session,
            connection_id=request.connection_id,
            actor_id=actor,
            target=request.target,
            rotation_request_id=request.rotation_request_id,
            reason=request.reason,
            trace_id=trace_id,
        )
    return SecretDestructionRequestResponse.model_validate(row)


@router.get("/secret-destructions/{request_id}", response_model=SecretDestructionRequestResponse)
async def get_secret_destruction(
    request_id: uuid.UUID,
    actor: ActorId,
    session: AsyncSession = Depends(get_session),
) -> SecretDestructionRequestResponse:
    row = await SecretDestructionService().get(session, request_id=request_id, actor_id=actor)
    return SecretDestructionRequestResponse.model_validate(row)


@router.get(
    "/connections/{connection_id}/secret-destructions",
    response_model=list[SecretDestructionRequestResponse],
)
async def list_secret_destructions(
    connection_id: uuid.UUID,
    actor: ActorId,
    session: AsyncSession = Depends(get_session),
) -> list[SecretDestructionRequestResponse]:
    rows = await SecretDestructionService().list_for_connection(
        session, connection_id=connection_id, actor_id=actor
    )
    return [SecretDestructionRequestResponse.model_validate(row) for row in rows]


@router.post(
    "/secret-destructions/{request_id}/decisions",
    response_model=SecretDestructionRequestResponse,
)
async def decide_secret_destruction(
    request_id: uuid.UUID,
    request: HarnessDecisionRequest,
    actor: ActorId,
    trace_id: TraceId,
    session: AsyncSession = Depends(get_session),
) -> SecretDestructionRequestResponse:
    """具名审批；通过后投递销毁任务（执行前会重新求证未被使用）。"""
    service = SecretDestructionService()
    async with session.begin():
        row = await service.decide(
            session,
            request_id=request_id,
            actor_id=actor,
            decision=request.decision,
            reason=request.reason,
            role=request.role,
            trace_id=trace_id,
        )
        snapshot = SecretDestructionRequestResponse.model_validate(row)
    # 判据是事实而不是请求里的意图：多槽位审批里投一票不等于批准。
    if snapshot.status is SecretDestructionStatus.APPROVED:
        await dispatch(execute_secret_destruction.delay, str(request_id))
    return snapshot
