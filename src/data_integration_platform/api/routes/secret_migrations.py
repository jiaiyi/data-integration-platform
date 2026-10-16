"""Secret Provider 迁移接口（SPEC §6.1：4 个端点 / FR-18）。

审批通过后由 Worker 执行迁移；API 只落事实与投递任务。
"""

import uuid

from fastapi import APIRouter, Depends, status
from sqlalchemy.ext.asyncio import AsyncSession

from data_integration_platform.api.dependencies import ActorId, TraceId
from data_integration_platform.api.schemas import (
    CreateSecretMigrationRequest,
    HarnessDecisionRequest,
    SecretMigrationRequestResponse,
)
from data_integration_platform.application.secret_migrations import SecretMigrationService
from data_integration_platform.celery_app import dispatch
from data_integration_platform.domain.enums import SecretMigrationStatus
from data_integration_platform.infrastructure.database import get_session
from data_integration_platform.secret_migration_tasks import execute_secret_migration

router = APIRouter(tags=["secret-migrations"])


@router.post(
    "/secret-migrations",
    response_model=SecretMigrationRequestResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_secret_migration(
    request: CreateSecretMigrationRequest,
    actor: ActorId,
    trace_id: TraceId,
    session: AsyncSession = Depends(get_session),
) -> SecretMigrationRequestResponse:
    """发起迁移申请。审批前**不碰任何凭据**。"""
    async with session.begin():
        row = await SecretMigrationService().request(
            session,
            connection_id=request.connection_id,
            actor_id=actor,
            target_provider=request.target_provider,
            reason=request.reason,
            trace_id=trace_id,
        )
    return SecretMigrationRequestResponse.model_validate(row)


@router.get("/secret-migrations/{request_id}", response_model=SecretMigrationRequestResponse)
async def get_secret_migration(
    request_id: uuid.UUID,
    actor: ActorId,
    session: AsyncSession = Depends(get_session),
) -> SecretMigrationRequestResponse:
    row = await SecretMigrationService().get(session, request_id=request_id, actor_id=actor)
    return SecretMigrationRequestResponse.model_validate(row)


@router.get(
    "/connections/{connection_id}/secret-migrations",
    response_model=list[SecretMigrationRequestResponse],
)
async def list_secret_migrations(
    connection_id: uuid.UUID,
    actor: ActorId,
    session: AsyncSession = Depends(get_session),
) -> list[SecretMigrationRequestResponse]:
    rows = await SecretMigrationService().list_for_connection(
        session, connection_id=connection_id, actor_id=actor
    )
    return [SecretMigrationRequestResponse.model_validate(row) for row in rows]


@router.post(
    "/secret-migrations/{request_id}/decisions",
    response_model=SecretMigrationRequestResponse,
)
async def decide_secret_migration(
    request_id: uuid.UUID,
    request: HarnessDecisionRequest,
    actor: ActorId,
    trace_id: TraceId,
    session: AsyncSession = Depends(get_session),
) -> SecretMigrationRequestResponse:
    """具名审批；通过后投递执行任务。申请人自批会被 SOD-01 拦截。"""
    service = SecretMigrationService()
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
        snapshot = SecretMigrationRequestResponse.model_validate(row)
    # 判据是**事实**而不是请求里的意图：多槽位审批里投一票不等于批准，
    # 投递要看业务事实有没有真的到「已批准」。
    if snapshot.status is SecretMigrationStatus.APPROVED:
        await dispatch(execute_secret_migration.delay, str(request_id))
    return snapshot
