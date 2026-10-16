"""调度配置接口（SPEC §6.1：3 个端点 / T-23 的 HTTP 面）。

**到点触发不在这里**：`PipelineScheduleService.advance` 由 Beat 的调度扫描推进，
触发那一次照样生成一份待审批的准备单（FR-65）—— 自动只是「谁按下了申请键」的差别，
不是绕过审批。所以这个路由只做配置、启停与查看，没有「立即执行」这种入口。

cron 表达式按 PRD 的写法解析：**秒在最前**（`0 0 2 * * *` 是每天 2 点）。
`croniter` 默认把秒放在最后，不显式传参会被读成「每月 2 日 0 点」——任务照跑、
时间全错、不报任何错（踩过一次）。这条在服务层已固定，这里不重复实现。
"""

import uuid

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from data_integration_platform.api.dependencies import ActorId, TraceId
from data_integration_platform.api.schemas import (
    ConfigureScheduleRequest,
    ScheduleResponse,
    SetScheduleEnabledRequest,
)
from data_integration_platform.application.schedules import PipelineScheduleService
from data_integration_platform.domain.errors import ResourceNotFound
from data_integration_platform.infrastructure.database import get_session

router = APIRouter(tags=["schedules"])


@router.get(
    "/pipeline-versions/{version_id}/schedule",
    response_model=ScheduleResponse | None,
)
async def get_schedule(
    version_id: uuid.UUID,
    actor: ActorId,
    session: AsyncSession = Depends(get_session),
) -> ScheduleResponse | None:
    """查看调度配置；没配过返回 `null`（「没配」与「配了但关着」要能区分开）。"""
    row = await PipelineScheduleService().get_for_version(
        session, version_id=version_id, actor_id=actor
    )
    return ScheduleResponse.model_validate(row) if row is not None else None


@router.put(
    "/pipeline-versions/{version_id}/schedule",
    response_model=ScheduleResponse,
)
async def configure_schedule(
    version_id: uuid.UUID,
    request: ConfigureScheduleRequest,
    actor: ActorId,
    trace_id: TraceId,
    session: AsyncSession = Depends(get_session),
) -> ScheduleResponse:
    """配置（或重配）调度；重配会按当下重算下一次触发时刻。"""
    async with session.begin():
        row = await PipelineScheduleService().configure(
            session,
            version_id=version_id,
            actor_id=actor,
            mode=request.mode,
            cron_expression=request.cron_expression,
            timezone=request.timezone,
            misfire_policy=request.misfire_policy,
            watermark_column=request.watermark_column,
            trace_id=trace_id,
        )
    return ScheduleResponse.model_validate(row)


@router.post(
    "/pipeline-versions/{version_id}/schedule/enabled",
    response_model=ScheduleResponse,
)
async def set_schedule_enabled(
    version_id: uuid.UUID,
    request: SetScheduleEnabledRequest,
    actor: ActorId,
    trace_id: TraceId,
    session: AsyncSession = Depends(get_session),
) -> ScheduleResponse:
    """启停调度。启用时按「此刻」重算下一次触发，避免停用期间攒出的失火一次性爆发。"""
    async with session.begin():
        row = await PipelineScheduleService().set_enabled(
            session,
            version_id=version_id,
            actor_id=actor,
            enabled=request.enabled,
            trace_id=trace_id,
        )
    if row is None:
        raise ResourceNotFound("该版本还没有调度配置")
    return ScheduleResponse.model_validate(row)
