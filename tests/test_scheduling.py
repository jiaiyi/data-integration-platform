"""调度策略（T-23 / FR-40、FR-65）。

要证的证据是 FR-65 那句「超出失火宽限时按跳过或补一次策略处理」，
所以失火的各种边角都在这里穷举 —— 纯计算部分不碰数据库，可以直接喂时刻。
另外钉住一条**回归**：croniter 默认把秒放在**最后**，而 PRD 写的是 `0 0 2 * * *`
（秒在前）。不显式 `second_at_beginning=True` 的话，这个表达式会被读成「每月 2 日 0 点」，
任务照跑、时间全错，而且不会报任何错。
"""

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from data_integration_platform.application.schedules import (
    SCHEDULABLE_STATES,
    PipelineScheduleService,
    ScheduleSpec,
    initial_window,
    next_after,
    plan_tick,
    previous_before,
    validate_cron,
    zone_of,
)
from data_integration_platform.application.versions import PipelineVersionService
from data_integration_platform.domain.enums import (
    PipelineVersionStatus,
    ScheduleMisfirePolicy,
    ScheduleMode,
)
from data_integration_platform.domain.errors import DomainError, ResourceNotFound
from data_integration_platform.infrastructure.models import Pipeline, PipelineVersion
from tests.conftest import ProjectContext
from tests.test_pipeline_versions import candidate

SHANGHAI = "Asia/Shanghai"
GRACE = 120
DAILY_2AM = "0 0 2 * * *"
# 2026-10-16 02:00:00 +08:00 == 2026-10-15 18:00:00 UTC
DUE_UTC = datetime(2026, 10, 15, 18, 0, tzinfo=UTC)


def watermark_spec(
    *,
    policy: ScheduleMisfirePolicy = ScheduleMisfirePolicy.SKIP,
    cron: str = DAILY_2AM,
    timezone: str = SHANGHAI,
) -> ScheduleSpec:
    return ScheduleSpec(
        mode=ScheduleMode.WATERMARK_INCREMENTAL,
        cron_expression=cron,
        timezone=timezone,
        misfire_policy=policy,
    )


def batch_spec(*, policy: ScheduleMisfirePolicy = ScheduleMisfirePolicy.SKIP) -> ScheduleSpec:
    return ScheduleSpec(
        mode=ScheduleMode.BATCH,
        cron_expression=DAILY_2AM,
        timezone=SHANGHAI,
        misfire_policy=policy,
    )


# ------------------------------------------------------------------ cron 与时区
def test_cron_is_parsed_with_seconds_first() -> None:
    """`0 0 2 * * *` 必须是每天 02:00:00（秒在前），不是每月 2 日 0 点。"""
    spec = watermark_spec()
    anchor = datetime(2026, 10, 16, 12, 0, tzinfo=UTC)

    assert next_after(spec, after=anchor) == datetime(2026, 10, 16, 18, 0, tzinfo=UTC)
    assert previous_before(spec, before=anchor) == datetime(2026, 10, 15, 18, 0, tzinfo=UTC)


def test_cron_is_evaluated_in_the_configured_timezone() -> None:
    """同一份表达式在不同时区对应不同的 UTC 时刻；时区不是装饰品。"""
    shanghai = watermark_spec()
    utc = watermark_spec(timezone="UTC")
    anchor = datetime(2026, 10, 16, 3, 0, tzinfo=UTC)

    assert next_after(shanghai, after=anchor) == datetime(2026, 10, 16, 18, 0, tzinfo=UTC)
    assert next_after(utc, after=anchor) == datetime(2026, 10, 17, 2, 0, tzinfo=UTC)


def test_occurrence_lookup_is_strict_on_both_sides() -> None:
    """恰好等于触发时刻时，next 取下一次、prev 取上一次，不返回自己。"""
    spec = watermark_spec()

    assert next_after(spec, after=DUE_UTC) == datetime(2026, 10, 16, 18, 0, tzinfo=UTC)
    assert previous_before(spec, before=DUE_UTC) == datetime(2026, 10, 14, 18, 0, tzinfo=UTC)


def test_invalid_expressions_are_rejected_at_configuration_time() -> None:
    with pytest.raises(DomainError, match="cron 表达式不合法"):
        validate_cron("每天凌晨两点")
    with pytest.raises(DomainError, match="未知时区"):
        zone_of(watermark_spec(timezone="Mars/Olympus"))


# ------------------------------------------------------------------ 正常触发
def test_not_yet_due_produces_no_tick() -> None:
    tick = plan_tick(
        watermark_spec(),
        next_run_at=DUE_UTC,
        window_start=DUE_UTC - timedelta(days=1),
        now=DUE_UTC - timedelta(seconds=1),
        grace_seconds=GRACE,
    )

    assert tick is None


def test_on_time_trigger_fires_with_its_own_window() -> None:
    tick = plan_tick(
        watermark_spec(),
        next_run_at=DUE_UTC,
        window_start=DUE_UTC - timedelta(days=1),
        now=DUE_UTC + timedelta(seconds=30),
        grace_seconds=GRACE,
    )

    assert tick is not None
    assert (tick.fire, tick.misfired) == (True, False)
    assert tick.scheduled_for == DUE_UTC
    assert (tick.window.start, tick.window.end) == (DUE_UTC - timedelta(days=1), DUE_UTC)
    # 下一次触发是未来第一个时刻，且窗口跟着推进到 [本次触发, 下一次触发)。
    assert tick.next_run_at == datetime(2026, 10, 16, 18, 0, tzinfo=UTC)
    assert (tick.next_window.start, tick.next_window.end) == (DUE_UTC, tick.next_run_at)


def test_tick_is_exactly_a_day_later_after_a_normal_run() -> None:
    tick = plan_tick(
        watermark_spec(),
        next_run_at=DUE_UTC,
        window_start=DUE_UTC - timedelta(days=1),
        now=DUE_UTC,
        grace_seconds=GRACE,
    )

    assert tick is not None
    assert tick.next_run_at - tick.scheduled_for == timedelta(days=1)


# ------------------------------------------------------------------ 失火
def test_misfire_with_skip_drops_the_run_and_keeps_the_window_start() -> None:
    """跳过时**不推进窗口起点**：被跳过的区间并进下一次触发的窗口，不会凭空消失。"""
    start = DUE_UTC - timedelta(days=1)
    tick = plan_tick(
        watermark_spec(policy=ScheduleMisfirePolicy.SKIP),
        next_run_at=DUE_UTC,
        window_start=start,
        now=DUE_UTC + timedelta(hours=6),
        grace_seconds=GRACE,
    )

    assert tick is not None
    assert (tick.fire, tick.misfired) == (False, True)
    assert tick.next_run_at == datetime(2026, 10, 16, 18, 0, tzinfo=UTC)
    assert tick.next_window.start == start


def test_misfire_with_fire_once_catches_up_exactly_once() -> None:
    """「补一次」就是字面意思：错过的那次补跑一遍，不是把积压逐条补回来。"""
    tick = plan_tick(
        watermark_spec(policy=ScheduleMisfirePolicy.FIRE_ONCE),
        next_run_at=DUE_UTC,
        window_start=DUE_UTC - timedelta(days=1),
        now=DUE_UTC + timedelta(hours=6),
        grace_seconds=GRACE,
    )

    assert tick is not None
    assert (tick.fire, tick.misfired) == (True, True)
    assert tick.scheduled_for == DUE_UTC
    # 只补一次：下一次直接落到未来第一个时刻，中间错过的那些不再各自补跑。
    assert tick.next_run_at == datetime(2026, 10, 16, 18, 0, tzinfo=UTC)
    # 补跑的窗口覆盖整个错过的区间，水位增量模式下因此不丢数据。
    assert tick.next_window.start == DUE_UTC


def test_fire_once_does_not_replay_a_long_backlog() -> None:
    """停机一周再上来，也只补一次 —— 逐条重放会把上游打垮。"""
    tick = plan_tick(
        watermark_spec(policy=ScheduleMisfirePolicy.FIRE_ONCE),
        next_run_at=DUE_UTC,
        window_start=DUE_UTC - timedelta(days=1),
        now=DUE_UTC + timedelta(days=7),
        grace_seconds=GRACE,
    )

    assert tick is not None and tick.fire
    assert tick.scheduled_for == DUE_UTC
    assert tick.next_run_at > tick.scheduled_for + timedelta(days=6)


def test_grace_boundary_itself_is_not_a_misfire() -> None:
    """宽限是「超过才算」，正好卡在边界上仍按正常触发处理。"""
    tick = plan_tick(
        watermark_spec(policy=ScheduleMisfirePolicy.SKIP),
        next_run_at=DUE_UTC,
        window_start=None,
        now=DUE_UTC + timedelta(seconds=GRACE),
        grace_seconds=GRACE,
    )

    assert tick is not None
    assert tick.misfired is False and tick.fire is True


def test_naive_stored_timestamps_are_normalized_before_comparison() -> None:
    """SQLite 读回的 `DateTime(timezone=True)` 不带 tzinfo，直接比会抛 TypeError。"""
    tick = plan_tick(
        watermark_spec(),
        next_run_at=DUE_UTC.replace(tzinfo=None),
        window_start=(DUE_UTC - timedelta(days=1)).replace(tzinfo=None),
        now=DUE_UTC,
        grace_seconds=GRACE,
    )

    assert tick is not None and tick.fire


# ------------------------------------------------------------------ 模式差异
def test_batch_mode_has_no_watermark_window() -> None:
    """全量模式没有水位可言：返回 None，而不是造一个空区间冒充。"""
    tick = plan_tick(
        batch_spec(),
        next_run_at=DUE_UTC,
        window_start=DUE_UTC - timedelta(days=1),
        now=DUE_UTC,
        grace_seconds=GRACE,
    )

    assert tick is not None
    assert tick.window is None and tick.next_window is None


def test_initial_window_starts_at_the_previous_occurrence() -> None:
    """首次冻结不用「配置时刻」当起点：触发时刻是确定的，配置时刻取决于人什么时候点的按钮。"""
    window = initial_window(watermark_spec(), next_run_at=DUE_UTC)

    assert window is not None
    assert (window.start, window.end) == (
        datetime(2026, 10, 14, 18, 0, tzinfo=UTC),
        DUE_UTC,
    )
    assert initial_window(batch_spec(), next_run_at=DUE_UTC) is None


# ------------------------------------------------------------------ 持久化
@pytest.fixture
async def frozen_version(
    session: AsyncSession, project: ProjectContext, tmp_path: Any
) -> PipelineVersion:
    versions = PipelineVersionService(root=tmp_path / "artifacts")
    created = await versions.create(
        session,
        project_id=project.project_id,
        actor_id=project.engineer_id,
        name=f"orders-sync-{uuid.uuid4().hex[:6]}",
        requirement="把订单同步到宽表",
    )
    created.status = PipelineVersionStatus.GENERATED
    await session.flush()
    frozen = await versions.freeze(
        session,
        version_id=created.id,
        actor_id=project.engineer_id,
        candidate=candidate(),
    )
    return frozen


@pytest.fixture
def schedules() -> PipelineScheduleService:
    return PipelineScheduleService()


async def configure(
    schedules: PipelineScheduleService,
    session: AsyncSession,
    project: ProjectContext,
    version: PipelineVersion,
    **overrides: Any,
) -> Any:
    values: dict[str, Any] = {
        "mode": ScheduleMode.WATERMARK_INCREMENTAL,
        "cron_expression": DAILY_2AM,
        "timezone": SHANGHAI,
        "misfire_policy": ScheduleMisfirePolicy.FIRE_ONCE,
        "watermark_column": "updated_at",
        "now": datetime(2026, 10, 16, 12, 0, tzinfo=UTC),
    }
    values.update(overrides)
    return await schedules.configure(
        session, version_id=version.id, actor_id=project.engineer_id, **values
    )


async def test_configure_freezes_the_next_trigger_and_window(
    schedules: PipelineScheduleService,
    session: AsyncSession,
    project: ProjectContext,
    frozen_version: PipelineVersion,
) -> None:
    schedule = await configure(schedules, session, project, frozen_version)

    assert schedule.next_run_at == datetime(2026, 10, 16, 18, 0, tzinfo=UTC)
    assert schedule.watermark_window_start == datetime(2026, 10, 15, 18, 0, tzinfo=UTC)
    assert schedule.watermark_window_end == schedule.next_run_at
    assert schedule.enabled is False  # 配置不等于启用
    assert schedule.timezone == SHANGHAI


async def test_configure_rejects_half_declared_contracts(
    schedules: PipelineScheduleService,
    session: AsyncSession,
    project: ProjectContext,
    frozen_version: PipelineVersion,
) -> None:
    with pytest.raises(DomainError, match="必须指定水位列"):
        await configure(schedules, session, project, frozen_version, watermark_column=None)
    with pytest.raises(DomainError, match="全量模式不应声明水位列"):
        await configure(
            schedules,
            session,
            project,
            frozen_version,
            mode=ScheduleMode.BATCH,
            watermark_column="updated_at",
        )
    with pytest.raises(DomainError, match="cron 表达式不合法"):
        await configure(schedules, session, project, frozen_version, cron_expression="每小时")
    with pytest.raises(DomainError, match="未知时区"):
        await configure(schedules, session, project, frozen_version, timezone="Mars/Olympus")


async def test_schedule_requires_a_frozen_version(
    schedules: PipelineScheduleService, session: AsyncSession, project: ProjectContext
) -> None:
    """还没冻结就配调度，等于调度一个还不存在的内容。"""
    pipeline = Pipeline(project_id=project.project_id, name=f"draft-{uuid.uuid4().hex[:6]}")
    session.add(pipeline)
    await session.flush()
    draft = PipelineVersion(
        pipeline_id=pipeline.id,
        version_number=1,
        status=PipelineVersionStatus.DRAFT,
        requirement="草稿",
        created_by=project.engineer_id,
    )
    session.add(draft)
    await session.flush()

    with pytest.raises(DomainError, match="只有冻结之后的版本"):
        await configure(schedules, session, project, draft)


async def test_configure_is_an_upsert_per_version(
    schedules: PipelineScheduleService,
    session: AsyncSession,
    project: ProjectContext,
    frozen_version: PipelineVersion,
) -> None:
    first = await configure(schedules, session, project, frozen_version)
    second = await configure(
        schedules, session, project, frozen_version, cron_expression="0 30 3 * * *"
    )

    assert first.id == second.id
    assert second.cron_expression == "0 30 3 * * *"


async def test_enabling_recomputes_from_now_instead_of_misfiring(
    schedules: PipelineScheduleService,
    session: AsyncSession,
    project: ProjectContext,
    frozen_version: PipelineVersion,
) -> None:
    """停用一周再启用，不该立刻失火 —— 那次失火只是开关停出来的。"""
    await configure(schedules, session, project, frozen_version)
    later = datetime(2026, 10, 23, 12, 0, tzinfo=UTC)

    schedule = await schedules.set_enabled(
        session, version_id=frozen_version.id, actor_id=project.engineer_id, enabled=True, now=later
    )

    assert schedule.enabled is True
    assert schedule.next_run_at == datetime(2026, 10, 23, 18, 0, tzinfo=UTC)


async def test_advance_does_nothing_while_disabled_or_early(
    schedules: PipelineScheduleService,
    session: AsyncSession,
    project: ProjectContext,
    frozen_version: PipelineVersion,
) -> None:
    schedule = await configure(schedules, session, project, frozen_version)
    before = schedule.next_run_at

    assert await schedules.advance(session, schedule_id=schedule.id, now=before) is None  # 未启用
    await schedules.set_enabled(
        session,
        version_id=frozen_version.id,
        actor_id=project.engineer_id,
        enabled=True,
        now=before - timedelta(hours=6),
    )
    assert schedule.next_run_at == before
    # 差一秒也还没到：不触发，也不留运行记录。
    assert (
        await schedules.advance(session, schedule_id=schedule.id, now=before - timedelta(seconds=1))
        is None
    )
    assert schedule.last_run_at is None


async def test_advance_records_the_trigger_and_advances_the_window(
    schedules: PipelineScheduleService,
    session: AsyncSession,
    project: ProjectContext,
    frozen_version: PipelineVersion,
) -> None:
    schedule = await configure(schedules, session, project, frozen_version)
    await schedules.set_enabled(
        session,
        version_id=frozen_version.id,
        actor_id=project.engineer_id,
        enabled=True,
        now=datetime(2026, 10, 16, 12, 0, tzinfo=UTC),
    )

    tick = await schedules.advance(
        session, schedule_id=schedule.id, now=datetime(2026, 10, 16, 18, 0, tzinfo=UTC)
    )

    assert tick is not None and tick.fire
    assert schedule.last_run_at == tick.scheduled_for
    assert schedule.next_run_at == tick.next_run_at
    assert schedule.watermark_window_start == tick.scheduled_for


async def test_advance_applies_the_misfire_policy_without_recording_a_run(
    schedules: PipelineScheduleService,
    session: AsyncSession,
    project: ProjectContext,
    frozen_version: PipelineVersion,
) -> None:
    """失火且策略为「跳过」：推进冻结状态，但不留下一次运行记录。"""
    schedule = await configure(
        schedules,
        session,
        project,
        frozen_version,
        misfire_policy=ScheduleMisfirePolicy.SKIP,
    )
    await schedules.set_enabled(
        session,
        version_id=frozen_version.id,
        actor_id=project.engineer_id,
        enabled=True,
        now=datetime(2026, 10, 16, 12, 0, tzinfo=UTC),
    )

    tick = await schedules.advance(
        session,
        schedule_id=schedule.id,
        now=datetime(2026, 10, 17, 0, 0, tzinfo=UTC),
        grace_seconds=GRACE,
    )

    assert tick is not None
    assert (tick.fire, tick.misfired) == (False, True)
    assert schedule.last_run_at is None
    assert schedule.next_run_at == datetime(2026, 10, 17, 18, 0, tzinfo=UTC)


async def test_advance_rejects_an_unknown_schedule(
    schedules: PipelineScheduleService, session: AsyncSession
) -> None:
    with pytest.raises(ResourceNotFound):
        await schedules.advance(session, schedule_id=uuid.uuid4())


def test_only_frozen_states_are_schedulable() -> None:
    assert PipelineVersionStatus.DRAFT not in SCHEDULABLE_STATES
    assert PipelineVersionStatus.GENERATED not in SCHEDULABLE_STATES
    assert PipelineVersionStatus.APPROVED in SCHEDULABLE_STATES
