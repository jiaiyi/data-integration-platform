"""通用副作用准备单与审批查询接口（SPEC §6.1：3 个端点 / FR-57）。

**申请与审批不在这里**：九类操作各有自己的业务入口（凭据迁移/轮换/回退/销毁走
`secret-*` 路由，执行取消与 Doris 回滚/清理由执行侧路由发起），而它们内部都经
`application/harness/side_effects.py` 这一套入口。这里只提供**跨操作统一的查看面**：
「这条副作用被冻结成了什么、谁批的、执行到哪一步」——不然排查一次凭据销毁得先知道
它属于哪张业务表，才能找到对应的接口。

能力令牌与声明体**不出现在响应里**：它是执行侧验签用的凭据，控制台没有理由拿到它。
"""

import uuid

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from data_integration_platform.api.dependencies import ActorId
from data_integration_platform.api.schemas import (
    SideEffectApprovalResponse,
    SideEffectPreparationResponse,
)
from data_integration_platform.application.harness import side_effects
from data_integration_platform.domain.harness import ExecutionPreparationStatus
from data_integration_platform.domain.side_effects import SideEffectOperation
from data_integration_platform.infrastructure.database import get_session

router = APIRouter(tags=["side-effects"])


@router.get(
    "/projects/{project_id}/side-effect-preparations",
    response_model=list[SideEffectPreparationResponse],
)
async def list_side_effect_preparations(
    project_id: uuid.UUID,
    actor: ActorId,
    operation: SideEffectOperation | None = Query(default=None),
    preparation_status: ExecutionPreparationStatus | None = Query(default=None, alias="status"),
    session: AsyncSession = Depends(get_session),
) -> list[SideEffectPreparationResponse]:
    """列出项目内的副作用准备单；可按操作与状态过滤。

    默认**不过滤状态**：待审批的和已经执行过的都要能看到 —— 只看待办会让人以为
    一条失败或被驳回的申请从未发生过。
    """
    rows = await side_effects.list_for_project(
        session,
        project_id=project_id,
        actor_id=actor,
        operation=operation,
        status=preparation_status,
    )
    return [SideEffectPreparationResponse.model_validate(row) for row in rows]


@router.get(
    "/side-effect-preparations/{preparation_id}",
    response_model=SideEffectPreparationResponse,
)
async def get_side_effect_preparation(
    preparation_id: uuid.UUID,
    actor: ActorId,
    session: AsyncSession = Depends(get_session),
) -> SideEffectPreparationResponse:
    row = await side_effects.get(session, preparation_id=preparation_id, actor_id=actor)
    return SideEffectPreparationResponse.model_validate(row)


@router.get(
    "/side-effect-preparations/{preparation_id}/approvals",
    response_model=list[SideEffectApprovalResponse],
)
async def list_side_effect_approvals(
    preparation_id: uuid.UUID,
    actor: ActorId,
    session: AsyncSession = Depends(get_session),
) -> list[SideEffectApprovalResponse]:
    """列出这张准备单上的具名审批记录（按决定时刻排序）。

    先经 `get` 走一次项目读权限：审批记录里的审批人身份不该被项目外的人枚举出来。
    """
    await side_effects.get(session, preparation_id=preparation_id, actor_id=actor)
    rows = await side_effects.approvals_of(session, preparation_id=preparation_id)
    return [SideEffectApprovalResponse.model_validate(row) for row in rows]
