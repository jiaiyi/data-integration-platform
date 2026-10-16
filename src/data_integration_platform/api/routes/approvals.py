"""版本的具名审批（SPEC §6.1 / PRD US-13、FR-56）。

版本审批与「执行准备单的审批」是**两件事**，别看混：

- 这里批的是**内容**：这份冻结制品能不能被执行准备引用。批准之后版本进入 `APPROVED`。
- 准备单那侧批的是**一次具体的执行**：动哪些表、用什么连接、风险几级。

所以这里是「先有内容批准，才谈得上执行准备」的前一步（`prepare()` 会拒绝未批准的版本）。
审批单绑的是制品摘要，内容一旦变化旧审批立即失效（SOD-04）——
这条由 `PipelineVersionService.decide_approval` 守，这里只负责把 HTTP 面接上。
"""

import uuid

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from data_integration_platform.api.dependencies import ActorId, TraceId
from data_integration_platform.api.schemas import (
    ApprovalDecisionRequest,
    ApprovalRequestResponse,
)
from data_integration_platform.application.versions import PipelineVersionService
from data_integration_platform.domain.enums import ApprovalStatus
from data_integration_platform.infrastructure.database import get_session

router = APIRouter(tags=["approvals"])


@router.get(
    "/projects/{project_id}/approval-requests",
    response_model=list[ApprovalRequestResponse],
)
async def list_approval_requests(
    project_id: uuid.UUID,
    actor: ActorId,
    approval_status: ApprovalStatus | None = Query(default=None, alias="status"),
    session: AsyncSession = Depends(get_session),
) -> list[ApprovalRequestResponse]:
    """项目内的版本审批单；默认含已决的，只看待办时传 `?status=PENDING`。"""
    rows = await PipelineVersionService().approval_requests(
        session, project_id=project_id, actor_id=actor, status=approval_status
    )
    return [ApprovalRequestResponse.model_validate(row) for row in rows]


@router.get("/approval-requests/{approval_id}", response_model=ApprovalRequestResponse)
async def get_approval_request(
    approval_id: uuid.UUID,
    actor: ActorId,
    session: AsyncSession = Depends(get_session),
) -> ApprovalRequestResponse:
    row = await PipelineVersionService().approval_request(
        session, approval_id=approval_id, actor_id=actor
    )
    return ApprovalRequestResponse.model_validate(row)


@router.post(
    "/approval-requests/{approval_id}/decisions",
    response_model=ApprovalRequestResponse,
)
async def decide_approval_request(
    approval_id: uuid.UUID,
    request: ApprovalDecisionRequest,
    actor: ActorId,
    trace_id: TraceId,
    session: AsyncSession = Depends(get_session),
) -> ApprovalRequestResponse:
    """具名决策。申请人自批（SOD-01）与摘要不匹配（SOD-04）都会被拒。"""
    async with session.begin():
        row = await PipelineVersionService().decide_approval(
            session,
            approval_id=approval_id,
            actor_id=actor,
            decision=request.decision,
            reason=request.reason,
            trace_id=trace_id,
        )
    return ApprovalRequestResponse.model_validate(row)
