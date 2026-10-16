"""数据源连接与元数据探查接口（SPEC §6.1：8 个端点 / FR-07 ~ FR-15）。

API 侧**不连外部数据库**：连通性测试与探查都只落状态再投递任务，
真正的只读查询在 Worker 进程里执行（FR-15）。
"""

import uuid

from fastapi import APIRouter, Depends, status
from sqlalchemy.ext.asyncio import AsyncSession

from data_integration_platform.api.dependencies import ActorId, TraceId
from data_integration_platform.api.schemas import (
    CreateConnectionRequest,
    CreateProfileRequest,
    DataConnectionResponse,
    DataProfileResponse,
    UpdateConnectionRequest,
    UpdateCredentialRotationPolicyRequest,
)
from data_integration_platform.application.connections import ConnectionService
from data_integration_platform.application.profiling import (
    ProfileService,
    connection_is_probeable,
)
from data_integration_platform.application.secrets import ConnectionCredentials
from data_integration_platform.celery_app import dispatch
from data_integration_platform.connection_tasks import profile_table, test_data_connection
from data_integration_platform.domain.errors import ResourceNotFound
from data_integration_platform.infrastructure.database import get_session
from data_integration_platform.infrastructure.models import DataConnection, DataProfile
from data_integration_platform.infrastructure.secret_factory import create_secret_provider

router = APIRouter(tags=["connections"])


@router.get("/projects/{project_id}/connections", response_model=list[DataConnectionResponse])
async def list_connections(
    project_id: uuid.UUID,
    actor: ActorId,
    session: AsyncSession = Depends(get_session),
) -> list[DataConnectionResponse]:
    service = ConnectionService(create_secret_provider(session))
    connections = await service.list_for_project(session, project_id=project_id, actor_id=actor)
    return [DataConnectionResponse.model_validate(item) for item in connections]


@router.post(
    "/connections", response_model=DataConnectionResponse, status_code=status.HTTP_201_CREATED
)
async def create_connection(
    request: CreateConnectionRequest,
    actor: ActorId,
    trace_id: TraceId,
    session: AsyncSession = Depends(get_session),
) -> DataConnectionResponse:
    """登记连接并把凭据交给 SecretProvider 加密保存；响应与审计都不含明文。"""
    service = ConnectionService(create_secret_provider(session))
    credentials = (
        ConnectionCredentials.model_validate(request.credentials.model_dump())
        if request.credentials is not None
        else None
    )
    async with session.begin():
        connection = await service.create(
            session,
            project_id=request.project_id,
            actor_id=actor,
            name=request.name,
            connector_type=request.connector_type,
            endpoint_role=request.endpoint_role,
            host=request.host,
            port=request.port,
            database_name=request.database_name,
            options=request.options,
            credentials=credentials,
            trace_id=trace_id,
        )
    return DataConnectionResponse.model_validate(connection)


@router.get("/connections/{connection_id}", response_model=DataConnectionResponse)
async def get_connection(
    connection_id: uuid.UUID,
    actor: ActorId,
    session: AsyncSession = Depends(get_session),
) -> DataConnectionResponse:
    service = ConnectionService(create_secret_provider(session))
    connection = await service.get(session, connection_id=connection_id, actor_id=actor)
    return DataConnectionResponse.model_validate(connection)


@router.put("/connections/{connection_id}", response_model=DataConnectionResponse)
async def update_connection(
    connection_id: uuid.UUID,
    request: UpdateConnectionRequest,
    actor: ActorId,
    trace_id: TraceId,
    session: AsyncSession = Depends(get_session),
) -> DataConnectionResponse:
    """更新非凭据配置；配置一变，之前的连通性结论即失效。"""
    service = ConnectionService(create_secret_provider(session))
    async with session.begin():
        connection = await service.update(
            session,
            connection_id=connection_id,
            actor_id=actor,
            name=request.name,
            host=request.host,
            port=request.port,
            database_name=request.database_name,
            options=request.options,
            endpoint_role=request.endpoint_role,
            trace_id=trace_id,
        )
    return DataConnectionResponse.model_validate(connection)


@router.put(
    "/connections/{connection_id}/credential-rotation-policy",
    response_model=DataConnectionResponse,
)
async def update_credential_rotation_policy(
    connection_id: uuid.UUID,
    request: UpdateCredentialRotationPolicyRequest,
    actor: ActorId,
    trace_id: TraceId,
    session: AsyncSession = Depends(get_session),
) -> DataConnectionResponse:
    """配置轮换策略；到期告警据此推导，不修改凭据本身。"""
    service = ConnectionService(create_secret_provider(session))
    async with session.begin():
        connection = await service.update_credential_rotation_policy(
            session,
            connection_id=connection_id,
            actor_id=actor,
            enabled=request.enabled,
            max_age_days=request.max_age_days,
            warning_days=request.warning_days,
            trace_id=trace_id,
        )
    return DataConnectionResponse.model_validate(connection)


@router.post("/connections/{connection_id}/tests", response_model=DataConnectionResponse)
async def test_connection(
    connection_id: uuid.UUID,
    actor: ActorId,
    session: AsyncSession = Depends(get_session),
) -> DataConnectionResponse:
    """置为「测试中」并投递连通性任务；API 不阻塞等待外部数据库。"""
    service = ConnectionService(create_secret_provider(session))
    async with session.begin():
        connection = await service.mark_testing(
            session, connection_id=connection_id, actor_id=actor
        )
        connection_id_value = connection.id
    await dispatch(test_data_connection.delay, str(connection_id_value))
    return DataConnectionResponse.model_validate(connection)


@router.get("/connections/{connection_id}/profiles", response_model=list[DataProfileResponse])
async def list_profiles(
    connection_id: uuid.UUID,
    actor: ActorId,
    session: AsyncSession = Depends(get_session),
) -> list[DataProfileResponse]:
    service = ConnectionService(create_secret_provider(session))
    profiles = await service.list_profiles(session, connection_id=connection_id, actor_id=actor)
    return [DataProfileResponse.model_validate(item) for item in profiles]


@router.post(
    "/connections/{connection_id}/profiles",
    response_model=DataProfileResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def create_profile(
    connection_id: uuid.UUID,
    request: CreateProfileRequest,
    actor: ActorId,
    trace_id: TraceId,
    session: AsyncSession = Depends(get_session),
) -> DataProfileResponse:
    """受理表级探查；只执行受限期查询与受限采样（FR-10），不做无界全表 COUNT。"""
    connection_service = ConnectionService(create_secret_provider(session))
    # 授权与前置校验必须在 session.begin() **之内**：AsyncSession 在首次查询时会自动
    # 开启事务，先查一次再 begin() 会直接抛「A transaction is already begun on this Session」。
    async with session.begin():
        connection = await connection_service.get(
            session, connection_id=connection_id, actor_id=actor
        )
        connection_is_probeable(connection)
        profile = await ProfileService().request(
            session,
            connection_id=connection_id,
            actor_id=actor,
            schema_name=request.schema_name,
            table_name=request.table_name,
            endpoint_role=request.endpoint_role,
        )
        profile_id = profile.id
        snapshot = DataProfileResponse.model_validate(profile)
    await dispatch(profile_table.delay, str(profile_id), request.sample_limit)
    return snapshot


@router.get("/data-profiles/{profile_id}", response_model=DataProfileResponse)
async def get_profile(
    profile_id: uuid.UUID,
    actor: ActorId,
    session: AsyncSession = Depends(get_session),
) -> DataProfileResponse:
    """读取 Profile；样本列一律为脱敏结果，不提供原文查看入口（FR-12）。"""
    profile = await session.get(DataProfile, profile_id)
    if profile is None:
        raise ResourceNotFound("Profile 不存在")
    connection = await session.get(DataConnection, profile.connection_id)
    if connection is None:
        raise ResourceNotFound("数据连接不存在")
    await ConnectionService(create_secret_provider(session)).get(
        session, connection_id=connection.id, actor_id=actor
    )
    return DataProfileResponse.model_validate(profile)
