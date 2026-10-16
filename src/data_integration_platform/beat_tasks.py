"""Beat 定时任务（T-39 / FR-65）。

四个按间隔跑的扫描，各自的间隔见 `config.py`（PRD 决策 Q5：秒数属可调实现参数，
验收只看「按间隔触发」这个行为，不核具体秒数）：

| 任务 | 作用 |
|---|---|
| `worker_heartbeat` | 让「Worker 还活着」成为一条可查的事实 |
| `scan_credential_expiry_alerts` | 凭据到期告警（T-15 已实现，这里只是登记进 Beat） |
| `scan_pipeline_schedules` | 到点的调度推进一格，并把这次触发**送进受管入口** |
| `dispatch_pending_commands` | 把 Outbox 里待投递的命令交给执行 Worker |

**调度触发同样走受管入口**（FR-65）：到点不是直接执行，而是生成一份准备单 ——
它照样要过审批与 Commit。自动只是「谁按下了申请键」的差别，不是绕过边界。

心跳只写一个**带 TTL 的标记**而不是台账：它表达的是「此刻有没有活的 Worker」，
这种事实过一会儿就不再成立，写成台账反而要额外维护过期语义。
"""

import socket
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from data_integration_platform.application.harness.outbox import claim
from data_integration_platform.application.harness.preparation import prepare
from data_integration_platform.application.schedules import PipelineScheduleService
from data_integration_platform.asyncio_compat import run_async
from data_integration_platform.celery_app import celery_app
from data_integration_platform.config import get_settings
from data_integration_platform.domain.enums import PipelineVersionStatus
from data_integration_platform.domain.errors import DomainError
from data_integration_platform.infrastructure.database import SessionFactory
from data_integration_platform.infrastructure.models import PipelineSchedule, PipelineVersion

HEARTBEAT_KEY_PREFIX = "dip:worker-heartbeat"
# 心跳的有效期给三倍间隔：一次抖动不该让 Worker 被误判成失联。
HEARTBEAT_TTL_MULTIPLIER = 3

DISPATCH_CLAIM_LIMIT = 32


@celery_app.task(name="data_integration_platform.beat_tasks.worker_heartbeat")
def worker_heartbeat() -> dict[str, Any]:
    """记一次心跳；键带 TTL，读的时候按「最近有没有跳」判断 Worker 是否在场。"""
    settings = get_settings()
    key = f"{HEARTBEAT_KEY_PREFIX}:{socket.gethostname()}"
    ttl = settings.worker_heartbeat_interval_seconds * HEARTBEAT_TTL_MULTIPLIER
    run_async(_write_heartbeat(key=key, ttl=ttl))
    return {"key": key, "ttl_seconds": ttl}


@celery_app.task(name="data_integration_platform.beat_tasks.dispatch_pending_commands")
def dispatch_pending_commands() -> dict[str, Any]:
    """认领 Outbox 里的待投递命令并交给执行 Worker。

    认领用租约（T-30）：Worker 崩掉后租约到期，同一批命令会被重新认领，
    命令不会因为「有人领走了但没做完」而消失。
    """
    claimed = run_async(_claim_commands())
    for command_id in claimed:
        celery_app.send_task(
            "data_integration_platform.execution_tasks.submit_execution",
            args=[command_id],
        )
    return {"claimed": len(claimed)}


@celery_app.task(name="data_integration_platform.beat_tasks.scan_pipeline_schedules")
def scan_pipeline_schedules() -> dict[str, Any]:
    """推进到点的调度；真正触发的那一次生成一份待审批的准备单。"""
    outcome = run_async(_scan_schedules())
    return {"fired": outcome.fired, "prepared": outcome.prepared, "skipped": outcome.skipped}


# ------------------------------------------------------------------ 实现
async def _write_heartbeat(*, key: str, ttl: int) -> None:
    from redis.asyncio import Redis

    client = Redis.from_url(get_settings().redis_url, decode_responses=True)
    try:
        await client.set(key, datetime.now(UTC).isoformat(), ex=ttl)
    finally:
        await client.aclose()


async def _claim_commands(
    *, session_factory: async_sessionmaker[AsyncSession] | None = None
) -> list[str]:
    factory = session_factory if session_factory is not None else SessionFactory
    async with factory() as session:
        async with session.begin():
            rows = await claim(
                session,
                owner=f"beat:{socket.gethostname()}",
                limit=DISPATCH_CLAIM_LIMIT,
            )
            return [str(row.id) for row in rows]


@dataclass(frozen=True, slots=True)
class ScheduleScanOutcome:
    fired: int
    prepared: int
    skipped: int


async def _scan_schedules(
    *, session_factory: async_sessionmaker[AsyncSession] | None = None
) -> ScheduleScanOutcome:
    factory = session_factory if session_factory is not None else SessionFactory
    service = PipelineScheduleService()
    fired = prepared = skipped = 0
    async with factory() as session:
        schedule_ids = (
            await session.scalars(
                select(PipelineSchedule.id).where(PipelineSchedule.enabled.is_(True))
            )
        ).all()
    for schedule_id in schedule_ids:
        async with factory() as session:
            async with session.begin():
                tick = await service.advance(session, schedule_id=schedule_id)
                if tick is None or not tick.fire:
                    continue
                fired += 1
                if await _prepare_for_tick(
                    session, schedule_id=schedule_id, scheduled_for=tick.scheduled_for
                ):
                    prepared += 1
                else:
                    skipped += 1
    return ScheduleScanOutcome(fired=fired, prepared=prepared, skipped=skipped)


async def _prepare_for_tick(
    session: AsyncSession, *, schedule_id: uuid.UUID, scheduled_for: datetime
) -> bool:
    """为一次调度触发生成准备单；生成不了就跳过这一次，并把原因留下。

    跳过而不是抛出去：一次调度触发失败不该让整轮扫描停住，后面还有别的调度要推进。
    """
    schedule = await session.get(PipelineSchedule, schedule_id)
    if schedule is None:
        return False
    version = await session.get(PipelineVersion, schedule.version_id)
    if version is None or version.status is not PipelineVersionStatus.APPROVED:
        # 未批准的版本不该被执行准备引用：调度先到、审批后到是正常的，等它批完再来。
        return False
    try:
        await prepare(
            session,
            version_id=version.id,
            actor_id=version.created_by,
            # 幂等键带上触发时刻：同一时刻重复扫描不会造出第二份准备单，
            # 而下一个触发时刻自然是一份新的。
            idempotency_key=f"schedule:{schedule.id}:{scheduled_for.isoformat()}",
        )
    except DomainError:
        return False
    return True
