"""Pipeline 与版本接口（SPEC §6.1、API-01 / API-04）。

覆盖：创建版本、查看设计审查视图、创建 V+1 修订、提交审批。
**生成**走 `agent_runs` 路由；**冻结**由 Worker 在门禁通过后完成（FR-35 的顺序约束），
这里不提供「先冻结再补检查」的入口。
"""

import uuid

from fastapi import APIRouter, Depends, status
from sqlalchemy.ext.asyncio import AsyncSession

from data_integration_platform.api.dependencies import ActorId, TraceId
from data_integration_platform.api.schemas import (
    ApprovalRequestResponse,
    CreatePipelineRequest,
    PipelineDesignResponse,
    PipelineVersionResponse,
    RevisePipelineVersionRequest,
)
from data_integration_platform.application.versions import PipelineVersionService
from data_integration_platform.infrastructure.database import get_session

router = APIRouter(tags=["pipelines"])


@router.post(
    "/pipelines", response_model=PipelineVersionResponse, status_code=status.HTTP_201_CREATED
)
async def create_pipeline_version(
    request: CreatePipelineRequest,
    actor: ActorId,
    trace_id: TraceId,
    session: AsyncSession = Depends(get_session),
) -> PipelineVersionResponse:
    """创建管道与它的第一个版本（API-01）；此时还没有任何内容可冻结。"""
    async with session.begin():
        version = await PipelineVersionService().create(
            session,
            project_id=request.project_id,
            actor_id=actor,
            name=request.name,
            requirement=request.requirement,
        )
    return PipelineVersionResponse.model_validate(version)


@router.post(
    "/pipeline-versions/{version_id}/revisions",
    response_model=PipelineVersionResponse,
    status_code=status.HTTP_201_CREATED,
)
async def revise_pipeline_version(
    version_id: uuid.UUID,
    request: RevisePipelineVersionRequest,
    actor: ActorId,
    session: AsyncSession = Depends(get_session),
) -> PipelineVersionResponse:
    """需求变更创建 V+1，父版本原封不动（FR-39）。"""
    async with session.begin():
        revision = await PipelineVersionService().revise(
            session, version_id=version_id, actor_id=actor, requirement=request.requirement
        )
    return PipelineVersionResponse.model_validate(revision)


@router.get("/pipelines/versions/{version_id}/design", response_model=PipelineDesignResponse)
async def get_pipeline_design(
    version_id: uuid.UUID,
    actor: ActorId,
    session: AsyncSession = Depends(get_session),
) -> PipelineDesignResponse:
    """设计审查视图（API-04）：内容从**冻结制品**读回，与执行阶段拿到的是同一份字节。"""
    view = await PipelineVersionService().design_view(
        session, version_id=version_id, actor_id=actor
    )
    return PipelineDesignResponse(
        pipeline_version_id=view.version_id,
        status=view.status,
        artifact_sha256=view.artifact_sha256,
        plan=view.plan,
        hocon=view.hocon,
    )


@router.post(
    "/pipeline-versions/{version_id}/approval-requests",
    response_model=ApprovalRequestResponse,
    status_code=status.HTTP_201_CREATED,
)
async def submit_for_approval(
    version_id: uuid.UUID,
    actor: ActorId,
    trace_id: TraceId,
    session: AsyncSession = Depends(get_session),
) -> ApprovalRequestResponse:
    """提交审批：审批单绑定冻结的制品摘要（SOD-04）。

    审批的**决策**（通过 / 驳回）不在本接口，属 M5 的审批链路。
    """
    async with session.begin():
        request = await PipelineVersionService().submit_for_approval(
            session, version_id=version_id, actor_id=actor, trace_id=trace_id
        )
    return ApprovalRequestResponse.model_validate(request)
