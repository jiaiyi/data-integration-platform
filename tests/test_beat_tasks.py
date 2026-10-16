"""Beat 定时任务（T-39 / FR-65）。

证据是「四个定时任务按间隔触发」，所以先钉住注册表本身；再验两件行为：
Outbox 扫描会把待投递命令交给执行 Worker，调度扫描会把到点的那一次
**送进受管入口**（生成待审批的准备单），而不是绕过审批直接执行。
"""

import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from data_integration_platform import beat_tasks
from data_integration_platform.application.harness.outbox import enqueue
from data_integration_platform.application.schedules import PipelineScheduleService
from data_integration_platform.application.secret_governance import as_utc
from data_integration_platform.application.versions import PipelineVersionService
from data_integration_platform.celery_app import celery_app
from data_integration_platform.config import get_settings
from data_integration_platform.domain.enums import (
    ApprovalDecisionValue,
    PipelineVersionStatus,
    ProjectRole,
    ScheduleMisfirePolicy,
    ScheduleMode,
)
from data_integration_platform.domain.harness import ExecutionPreparationStatus, HarnessRole
from data_integration_platform.infrastructure.models import (
    ExecutionPreparation,
    HarnessCommandOutbox,
    PipelineSchedule,
    PipelineVersion,
    ProjectMembership,
    ProjectRoleGrant,
    User,
)
from tests.conftest import ProjectContext
from tests.test_pipeline_versions import candidate, ensure_execution_endpoints

NOW = datetime(2026, 10, 16, 12, 0, tzinfo=UTC)


# ------------------------------------------------------------------ 注册表
def test_beat_registers_the_four_scheduled_tasks() -> None:
    """四个定时任务各自按配置的间隔登记；缺一个，那类积压就没人管。"""
    settings = get_settings()
    schedule = celery_app.conf.beat_schedule

    assert schedule["worker-heartbeat"]["task"].endswith("worker_heartbeat")
    assert schedule["schedule-scan"]["task"].endswith("scan_pipeline_schedules")
    assert schedule["outbox-dispatch-scan"]["task"].endswith("dispatch_pending_commands")
    assert "credential-expiry-alert-scan" in schedule

    assert schedule["worker-heartbeat"]["schedule"] == float(
        settings.worker_heartbeat_interval_seconds
    )
    assert schedule["schedule-scan"]["schedule"] == float(settings.schedule_scan_interval_seconds)
    assert schedule["outbox-dispatch-scan"]["schedule"] == float(
        settings.harness_outbox_scan_interval_seconds
    )


def test_beat_intervals_are_positive() -> None:
    """间隔为零会让 Beat 空转打满 CPU —— 配置层就拦住。"""
    settings = get_settings()

    assert settings.worker_heartbeat_interval_seconds > 0
    assert settings.schedule_scan_interval_seconds > 0
    assert settings.harness_outbox_scan_interval_seconds > 0


# ------------------------------------------------------------------ 心跳
async def test_heartbeat_writes_a_key_with_ttl(monkeypatch: pytest.MonkeyPatch) -> None:
    """心跳是一个**带 TTL 的标记**：它表达的是「此刻有没有活的 Worker」。

    写成台账的话还得自己维护过期语义，而这条事实本来就是会过期的。
    """
    recorded: dict[str, object] = {}

    class FakeRedis:
        async def set(self, key: str, value: str, *, ex: int) -> None:
            recorded.update(key=key, value=value, ex=ex)

        async def aclose(self) -> None:
            recorded["closed"] = True

    monkeypatch.setattr("redis.asyncio.Redis.from_url", lambda *args, **kwargs: FakeRedis())
    settings = get_settings()

    await beat_tasks._write_heartbeat(key="dip:worker-heartbeat:test", ttl=30)

    assert recorded["key"] == "dip:worker-heartbeat:test"
    assert recorded["ex"] == 30
    assert recorded["closed"] is True
    # 心跳有效期是间隔的三倍：一次抖动不该把 Worker 判成失联。
    assert beat_tasks.HEARTBEAT_TTL_MULTIPLIER == 3
    assert settings.worker_heartbeat_interval_seconds > 0


# ------------------------------------------------------------------ Outbox 扫描
async def test_dispatch_scan_claims_pending_commands(
    session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    project: ProjectContext,
    tmp_path: Path,
) -> None:
    """扫描认领待投递命令；认领而不是「读出来就删」，崩了还能重投。"""
    from tests.test_preparation_approval import prepared

    preparation = await prepared(session, project, tmp_path, key="beat-prep-1")
    await enqueue(session, preparation_id=preparation.id, command={"tool": "seatunnel.submit_job"})
    await session.commit()

    claimed = await beat_tasks._claim_commands(session_factory=session_factory)

    assert len(claimed) == 1
    row = await session.get(HarnessCommandOutbox, uuid.UUID(claimed[0]))
    assert row is not None
    assert row.status.value == "DISPATCHING"
    assert row.lease_owner is not None


# ------------------------------------------------------------------ 调度扫描
async def _approved_version_with_schedule(
    session: AsyncSession, project: ProjectContext, tmp_path: Path
) -> tuple[PipelineVersion, PipelineSchedule]:
    await ensure_execution_endpoints(session, project)
    versions = PipelineVersionService(root=tmp_path / "artifacts")
    version = await versions.create(
        session,
        project_id=project.project_id,
        actor_id=project.engineer_id,
        name=f"sched-{uuid.uuid4().hex[:6]}",
        requirement="每天同步一次",
    )
    version.status = PipelineVersionStatus.GENERATED
    await session.flush()
    await versions.freeze(
        session, version_id=version.id, actor_id=project.engineer_id, candidate=candidate()
    )
    request = await versions.submit_for_approval(
        session, version_id=version.id, actor_id=project.engineer_id
    )
    approver = User(subject=f"approver-{uuid.uuid4().hex[:6]}", display_name="审批人")
    session.add(approver)
    await session.flush()
    session.add(
        ProjectMembership(
            project_id=project.project_id, user_id=approver.id, role=ProjectRole.APPROVER
        )
    )
    session.add(
        ProjectRoleGrant(
            project_id=project.project_id, user_id=approver.id, role=HarnessRole.DATA_APPROVER
        )
    )
    await session.flush()
    await versions.decide_approval(
        session,
        approval_id=request.id,
        actor_id=approver.id,
        decision=ApprovalDecisionValue.APPROVE,
        reason="通过",
    )
    schedule = await PipelineScheduleService().configure(
        session,
        version_id=version.id,
        actor_id=project.engineer_id,
        mode=ScheduleMode.BATCH,
        cron_expression="0 0 2 * * *",
        timezone="UTC",
        misfire_policy=ScheduleMisfirePolicy.FIRE_ONCE,
        now=NOW,
    )
    await PipelineScheduleService().set_enabled(
        session, version_id=version.id, actor_id=project.engineer_id, enabled=True, now=NOW
    )
    # 扫描用的是「此刻」，所以把这次触发摆到刚刚过去的一分钟 —— 等价于「时间到了」。
    schedule.next_run_at = datetime.now(UTC) - timedelta(minutes=1)
    await session.flush()
    return version, schedule


async def test_schedule_scan_sends_a_due_trigger_into_the_managed_entry(
    session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    project: ProjectContext,
    tmp_path: Path,
) -> None:
    """到点的那一次生成**待审批的准备单**，不是直接执行（FR-65）。"""
    version, schedule = await _approved_version_with_schedule(session, project, tmp_path)
    await session.commit()

    outcome = await beat_tasks._scan_schedules(session_factory=session_factory)

    assert outcome.fired == 1
    assert outcome.prepared == 1
    await session.refresh(schedule)
    # 触发时刻被记成「上一次的 next_run_at」，且下一次已经推到未来。
    assert as_utc(schedule.last_run_at) is not None
    assert as_utc(schedule.next_run_at) > datetime.now(UTC)
    preparations = (
        await session.scalars(
            select(ExecutionPreparation).where(
                ExecutionPreparation.pipeline_version_id == version.id
            )
        )
    ).all()
    assert len(preparations) == 1
    assert preparations[0].status is ExecutionPreparationStatus.WAITING_APPROVAL
    assert preparations[0].idempotency_key.startswith(f"schedule:{schedule.id}:")


async def test_schedule_scan_does_not_duplicate_a_trigger(
    session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    project: ProjectContext,
    tmp_path: Path,
) -> None:
    """同一触发时刻重复扫描不会造出第二份准备单（幂等键带上触发时刻）。"""
    version, _ = await _approved_version_with_schedule(session, project, tmp_path)
    await session.commit()

    first = await beat_tasks._scan_schedules(session_factory=session_factory)
    assert first.prepared == 1
    # 把触发时刻改回同一格再扫一次，模拟重复扫描。
    schedule = await session.scalar(
        select(PipelineSchedule).where(PipelineSchedule.version_id == version.id)
    )
    assert schedule is not None
    # 扫描是在另一把会话里写的，这里要刷新才看得到它推进后的状态。
    await session.refresh(schedule)
    # 重放**同一格**触发：幂等键带上触发时刻，所以同一格不会造出第二份准备单。
    assert schedule.last_run_at is not None
    schedule.next_run_at = schedule.last_run_at
    schedule.enabled = True
    await session.commit()

    await beat_tasks._scan_schedules(session_factory=session_factory)

    preparations = (
        await session.scalars(
            select(ExecutionPreparation).where(
                ExecutionPreparation.pipeline_version_id == version.id
            )
        )
    ).all()
    assert len(preparations) == 1


async def test_schedule_scan_skips_an_unapproved_version(
    session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    project: ProjectContext,
    tmp_path: Path,
) -> None:
    """版本还没批完就跳过这一次，并把调度推进到下一格 —— 一次失败不该卡住整轮扫描。"""
    version, _schedule = await _approved_version_with_schedule(session, project, tmp_path)
    version.status = PipelineVersionStatus.WAITING_APPROVAL
    await session.commit()

    outcome = await beat_tasks._scan_schedules(session_factory=session_factory)

    assert outcome.fired == 1
    assert outcome.prepared == 0
    assert outcome.skipped == 1
    assert (
        await session.scalars(
            select(ExecutionPreparation).where(
                ExecutionPreparation.pipeline_version_id == version.id
            )
        )
    ).all() == []
