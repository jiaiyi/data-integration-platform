"""调度策略：cron、时区、模式与失火处理（T-23 / FR-40、FR-65）。

三件事：

1. **配置即校验**：cron 表达式与 IANA 时区在写入前验，不留到扫描时才炸。
2. **冻结触发时刻与水位窗口**：`next_run_at`（UTC）与水位窗口一起落在调度行上；
   扫描与执行只读冻结值，不在别处重算（FR-40：触发时刻与水位窗口被冻结并写入执行记录）。
3. **失火处理**：触发时刻过去超过宽限即失火，按策略二选一 —— `SKIP` 跳到未来第一个
   时刻，`FIRE_ONCE` **只补一次**（不是错过几个补几次），补跑的窗口覆盖整个错过的区间，
   水位增量模式下因此不丢数据。

水位窗口的不变量：窗口始终是**即将发生的那次触发**要读的区间 `[起点, 触发时刻)`，
起点是上一次触发时刻，所以 `窗口末端 == next_run_at` 恒成立。按 `SKIP` 跳过时**不推进起点** ——
被跳过的区间并进下一次触发的窗口，不会凭空消失。

**cron 用 6 段（秒在前）**：与 PRD 里 `0 0 2 * * *` 的写法一致，croniter 默认把秒放在最后，
所以要显式 `second_at_beginning=True`，否则 `0 0 2 * * *` 会被读成「每月 2 日 0 点」。

本模块的判定全是纯计算（cron 求值 + 策略判定），不写库；落库由 `PipelineScheduleService` 做。
"""

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from croniter import croniter
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from data_integration_platform.application.access import require_project_roles
from data_integration_platform.application.audit import record_audit_event
from data_integration_platform.application.secret_governance import as_utc
from data_integration_platform.config import get_settings
from data_integration_platform.domain.enums import (
    AuditEventType,
    PipelineVersionStatus,
    ProjectRole,
    ScheduleMisfirePolicy,
    ScheduleMode,
)
from data_integration_platform.domain.errors import DomainError, ResourceNotFound
from data_integration_platform.infrastructure.models import (
    Pipeline,
    PipelineSchedule,
    PipelineVersion,
)

WRITABLE = frozenset({ProjectRole.ADMIN, ProjectRole.DATA_ENGINEER})
READABLE = frozenset(
    {ProjectRole.ADMIN, ProjectRole.DATA_ENGINEER, ProjectRole.APPROVER, ProjectRole.VIEWER}
)

# 允许配置调度的版本状态：冻结之后的内容才算数，DRAFT/PROFILING 还给不出要调度什么。
SCHEDULABLE_STATES = frozenset(
    {
        PipelineVersionStatus.VALIDATED,
        PipelineVersionStatus.WAITING_APPROVAL,
        PipelineVersionStatus.APPROVED,
        PipelineVersionStatus.REJECTED,
    }
)


# ------------------------------------------------------------------ 纯计算
@dataclass(frozen=True, slots=True)
class ScheduleSpec:
    """调度策略本身；与持久化解耦，好让判定能脱离数据库被穷举测试。"""

    mode: ScheduleMode
    cron_expression: str
    timezone: str
    misfire_policy: ScheduleMisfirePolicy

    @classmethod
    def of(cls, row: PipelineSchedule) -> "ScheduleSpec":
        return cls(
            mode=row.mode,
            cron_expression=row.cron_expression,
            timezone=row.timezone,
            misfire_policy=row.misfire_policy,
        )


@dataclass(frozen=True, slots=True)
class WatermarkWindow:
    """水位增量模式下一次触发要读取的区间，左闭右开。"""

    start: datetime
    end: datetime


@dataclass(frozen=True, slots=True)
class ScheduleTick:
    """一次调度扫描的结论。

    `window` 是**本次触发**（`fire` 为真时）要读的水位区间；
    `next_run_at` / `next_window` 是处理完之后重新冻结的调度状态。
    """

    scheduled_for: datetime
    misfired: bool
    fire: bool
    window: WatermarkWindow | None
    next_run_at: datetime
    next_window: WatermarkWindow | None


def zone_of(spec: ScheduleSpec) -> ZoneInfo:
    try:
        return ZoneInfo(spec.timezone)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise DomainError(f"未知时区：{spec.timezone}") from exc


def validate_cron(expression: str) -> None:
    """表达式的合法性在配置时判定，不留到扫描时才失败。

    `is_valid` 也必须显式传 `second_at_beginning` —— 它默认按「秒在末尾」校验，
    与这里的解析口径不一致时，会把 `0 30 3 * * *`（每天 03:30）判成非法，
    反过来也可能放过解析阶段才炸的表达式。
    """
    if not croniter.is_valid(expression, second_at_beginning=True):
        raise DomainError(f"cron 表达式不合法：{expression}")


def next_after(spec: ScheduleSpec, *, after: datetime) -> datetime:
    """严格晚于 `after` 的下一次触发时刻（UTC）。"""
    return _occurrence(spec, after, forward=True)


def previous_before(spec: ScheduleSpec, *, before: datetime) -> datetime:
    """严格早于 `before` 的上一次触发时刻（UTC）。"""
    return _occurrence(spec, before, forward=False)


def plan_tick(
    spec: ScheduleSpec,
    *,
    next_run_at: datetime | None,
    window_start: datetime | None,
    now: datetime,
    grace_seconds: int,
) -> ScheduleTick | None:
    """判定这一刻该不该触发、失火怎么处理；未到触发时刻返回 `None`。

    扫描永远把 `next_run_at` 推进到**未来第一个**时刻，不补一串积压 ——
    积压由 `FIRE_ONCE` 一次性覆盖，`SKIP` 则连这一次也不要。
    """
    if next_run_at is None:
        return None
    due = as_utc(next_run_at)
    moment = as_utc(now)
    if moment < due:
        return None

    misfired = (moment - due).total_seconds() > grace_seconds
    fire = not (misfired and spec.misfire_policy is ScheduleMisfirePolicy.SKIP)
    following = next_after(spec, after=moment)

    return ScheduleTick(
        scheduled_for=due,
        misfired=misfired,
        fire=fire,
        window=_window(spec, start=window_start, end=due),
        next_run_at=following,
        # 跳过时不推进起点：被跳过的区间并进下一次触发的窗口。
        next_window=_window(
            spec,
            start=due if fire else window_start,
            end=following,
        ),
    )


def initial_window(spec: ScheduleSpec, *, next_run_at: datetime) -> WatermarkWindow | None:
    """首次冻结：窗口起点取上一次触发时刻。

    刻意不取「配置时刻」：cron 触发时刻是确定的，配置时刻取决于人什么时候点的按钮，
    用后者会让同一份配置产生不同的冻结事实，复现率（PRD G-05）就没了。
    """
    return _window(spec, start=previous_before(spec, before=next_run_at), end=next_run_at)


def _window(spec: ScheduleSpec, *, start: datetime | None, end: datetime) -> WatermarkWindow | None:
    """全量模式没有水位可言，返回 `None` 而不是造一个空区间冒充。"""
    if spec.mode is not ScheduleMode.WATERMARK_INCREMENTAL or start is None:
        return None
    return WatermarkWindow(start=as_utc(start), end=as_utc(end))


def _occurrence(spec: ScheduleSpec, anchor: datetime, *, forward: bool) -> datetime:
    local = as_utc(anchor).astimezone(zone_of(spec))
    iterator = croniter(spec.cron_expression, local, second_at_beginning=True)
    found: datetime = iterator.get_next(datetime) if forward else iterator.get_prev(datetime)
    return found.astimezone(UTC)


# ------------------------------------------------------------------ 持久化
class PipelineScheduleService:
    async def configure(
        self,
        session: AsyncSession,
        *,
        version_id: uuid.UUID,
        actor_id: uuid.UUID,
        mode: ScheduleMode,
        cron_expression: str,
        timezone: str,
        misfire_policy: ScheduleMisfirePolicy,
        watermark_column: str | None = None,
        now: datetime | None = None,
        trace_id: str | None = None,
    ) -> PipelineSchedule:
        """配置（或重配）某版本的调度策略，并重新冻结触发时刻与水位窗口。"""
        version = await self._require_version(session, version_id)
        project_id = await self._require_writable(session, version, actor_id)
        spec = ScheduleSpec(
            mode=mode,
            cron_expression=cron_expression,
            timezone=timezone,
            misfire_policy=misfire_policy,
        )
        validate_cron(cron_expression)
        zone_of(spec)
        _require_watermark_column(mode=mode, watermark_column=watermark_column)

        moment = as_utc(now or datetime.now(UTC))
        next_run_at = next_after(spec, after=moment)
        window = initial_window(spec, next_run_at=next_run_at)

        schedule = await session.scalar(
            select(PipelineSchedule).where(PipelineSchedule.version_id == version.id)
        )
        if schedule is None:
            schedule = PipelineSchedule(version_id=version.id)
            session.add(schedule)
        schedule.mode = mode
        schedule.cron_expression = cron_expression
        schedule.timezone = timezone
        schedule.misfire_policy = misfire_policy
        schedule.watermark_column = watermark_column
        schedule.next_run_at = next_run_at
        schedule.watermark_window_start = window.start if window else None
        schedule.watermark_window_end = window.end if window else None
        await session.flush()

        record_audit_event(
            session,
            event_type=AuditEventType.PIPELINE_SCHEDULE_CONFIGURED,
            target_type="pipeline_schedule",
            project_id=project_id,
            actor_id=actor_id,
            target_id=str(schedule.id),
            change_summary={
                "version_id": str(version.id),
                "mode": mode.value,
                "cron_expression": cron_expression,
                "timezone": timezone,
                "misfire_policy": misfire_policy.value,
                "next_run_at": next_run_at.isoformat(),
            },
            trace_id=trace_id,
        )
        return schedule

    async def set_enabled(
        self,
        session: AsyncSession,
        *,
        version_id: uuid.UUID,
        actor_id: uuid.UUID,
        enabled: bool,
        now: datetime | None = None,
        trace_id: str | None = None,
    ) -> PipelineSchedule:
        """启停调度。

        启用时按「此刻」重算下一次触发：否则停用一周后一启用就会立刻失火，
        而那次失火跟业务无关，只是开关停出来的。
        """
        version = await self._require_version(session, version_id)
        project_id = await self._require_writable(session, version, actor_id)
        schedule = await self._require_schedule(session, version.id)
        spec = ScheduleSpec.of(schedule)

        moment = as_utc(now or datetime.now(UTC))
        if enabled:
            next_run_at = next_after(spec, after=moment)
            window = initial_window(spec, next_run_at=next_run_at)
            schedule.next_run_at = next_run_at
            schedule.watermark_window_start = window.start if window else None
            schedule.watermark_window_end = window.end if window else None
        schedule.enabled = enabled
        await session.flush()

        record_audit_event(
            session,
            event_type=AuditEventType.PIPELINE_SCHEDULE_TOGGLED,
            target_type="pipeline_schedule",
            project_id=project_id,
            actor_id=actor_id,
            target_id=str(schedule.id),
            change_summary={"version_id": str(version.id), "enabled": enabled},
            trace_id=trace_id,
        )
        return schedule

    async def get_for_version(
        self, session: AsyncSession, *, version_id: uuid.UUID, actor_id: uuid.UUID
    ) -> PipelineSchedule | None:
        """某版本的调度配置；没配过返回 `None`。

        刻意不返回空对象：「没配过」与「配了但关着」对调用方是两件事 ——
        前者要引导去配置，后者只要打开开关。
        读取**不校验可调度状态**：版本状态后来变了，已配的调度照样该看得见。
        """
        version = await session.get(PipelineVersion, version_id)
        if version is None:
            raise ResourceNotFound("管道版本不存在")
        pipeline = await session.get(Pipeline, version.pipeline_id)
        if pipeline is None:
            raise ResourceNotFound("管道不存在")
        await require_project_roles(
            session, project_id=pipeline.project_id, actor_id=actor_id, allowed=READABLE
        )
        return await session.scalar(
            select(PipelineSchedule).where(PipelineSchedule.version_id == version_id)
        )

    async def advance(
        self,
        session: AsyncSession,
        *,
        schedule_id: uuid.UUID,
        now: datetime | None = None,
        grace_seconds: int | None = None,
    ) -> ScheduleTick | None:
        """按调度扫描的语义推进一次：返回该不该触发，并落新的冻结状态。

        未到触发时刻或调度未启用时返回 `None`，且**不动**任何字段 ——
        调用方据此区分「没事发生」与「刚推进了一步」。
        """
        schedule = await session.get(PipelineSchedule, schedule_id)
        if schedule is None:
            raise ResourceNotFound("调度不存在")
        if not schedule.enabled:
            return None

        tick = plan_tick(
            ScheduleSpec.of(schedule),
            next_run_at=schedule.next_run_at,
            window_start=schedule.watermark_window_start,
            now=now or datetime.now(UTC),
            grace_seconds=(
                grace_seconds
                if grace_seconds is not None
                else get_settings().schedule_misfire_grace_seconds
            ),
        )
        if tick is None:
            return None

        schedule.next_run_at = tick.next_run_at
        schedule.watermark_window_start = tick.next_window.start if tick.next_window else None
        schedule.watermark_window_end = tick.next_window.end if tick.next_window else None
        if tick.fire:
            schedule.last_run_at = tick.scheduled_for
        await session.flush()
        return tick

    # -------------------------------------------------------------- 内部
    async def _require_writable(
        self, session: AsyncSession, version: PipelineVersion, actor_id: uuid.UUID
    ) -> uuid.UUID:
        pipeline = await session.get(Pipeline, version.pipeline_id)
        if pipeline is None:
            raise ResourceNotFound("管道不存在")
        await require_project_roles(
            session,
            project_id=pipeline.project_id,
            actor_id=actor_id,
            allowed=WRITABLE,
            write=True,
        )
        return pipeline.project_id

    async def _require_version(
        self, session: AsyncSession, version_id: uuid.UUID
    ) -> PipelineVersion:
        version = await session.get(PipelineVersion, version_id)
        if version is None:
            raise ResourceNotFound("管道版本不存在")
        if version.status not in SCHEDULABLE_STATES or version.artifact_sha256 is None:
            raise DomainError(
                f"版本当前状态为 {version.status.value}，只有冻结之后的版本才能配置调度"
            )
        return version

    async def _require_schedule(
        self, session: AsyncSession, version_id: uuid.UUID
    ) -> PipelineSchedule:
        schedule = await session.scalar(
            select(PipelineSchedule).where(PipelineSchedule.version_id == version_id)
        )
        if schedule is None:
            raise ResourceNotFound("该版本还没有调度配置")
        return schedule


def _require_watermark_column(*, mode: ScheduleMode, watermark_column: str | None) -> None:
    """模式与水位列必须成对出现：只声明一半会让语义含糊（与门禁第 5 项同一口径）。"""
    if mode is ScheduleMode.WATERMARK_INCREMENTAL and not watermark_column:
        raise DomainError("水位增量模式必须指定水位列")
    if mode is ScheduleMode.BATCH and watermark_column:
        raise DomainError("全量模式不应声明水位列")
