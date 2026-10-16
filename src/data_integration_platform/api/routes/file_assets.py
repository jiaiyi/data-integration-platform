"""受管文件资产接口（SPEC §6.1：3 个端点 / FR-09）。

上传即完成大小、格式、行数三道校验；超限文件**不落盘**。
"""

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, File, Form, UploadFile, status
from sqlalchemy.ext.asyncio import AsyncSession

from data_integration_platform.api.dependencies import ActorId, TraceId
from data_integration_platform.api.schemas import FileAssetResponse
from data_integration_platform.application.file_assets import FileAssetService
from data_integration_platform.config import get_settings
from data_integration_platform.domain.enums import DataEndpointRole
from data_integration_platform.domain.errors import DomainError
from data_integration_platform.infrastructure.database import get_session

router = APIRouter(tags=["file-assets"])


@router.post("/file-assets", response_model=FileAssetResponse, status_code=status.HTTP_201_CREATED)
async def upload_file_asset(
    actor: ActorId,
    trace_id: TraceId,
    project_id: Annotated[uuid.UUID, Form()],
    file: Annotated[UploadFile, File()],
    endpoint_role: Annotated[DataEndpointRole, Form()] = DataEndpointRole.SOURCE,
    session: AsyncSession = Depends(get_session),
) -> FileAssetResponse:
    """上传受管文件资产。

    读取时就设上限：超过单文件上限即中断，不把超大文件整个读进内存再拒绝。
    """
    limit = get_settings().file_asset_max_bytes
    data = await file.read(limit + 1)
    if len(data) > limit:
        raise DomainError(f"文件超过上限 {limit} 字节，已拒绝上传")

    service = FileAssetService()
    async with session.begin():
        asset = await service.upload(
            session,
            project_id=project_id,
            actor_id=actor,
            filename=file.filename or "",
            data=data,
            endpoint_role=endpoint_role,
            trace_id=trace_id,
        )
    return FileAssetResponse.model_validate(asset)


@router.get("/projects/{project_id}/file-assets", response_model=list[FileAssetResponse])
async def list_file_assets(
    project_id: uuid.UUID,
    actor: ActorId,
    session: AsyncSession = Depends(get_session),
) -> list[FileAssetResponse]:
    assets = await FileAssetService().list_for_project(
        session, project_id=project_id, actor_id=actor
    )
    return [FileAssetResponse.model_validate(item) for item in assets]


@router.get("/file-assets/{asset_id}", response_model=FileAssetResponse)
async def get_file_asset(
    asset_id: uuid.UUID,
    actor: ActorId,
    session: AsyncSession = Depends(get_session),
) -> FileAssetResponse:
    asset = await FileAssetService().get(session, asset_id=asset_id, actor_id=actor)
    return FileAssetResponse.model_validate(asset)
