"""三阶段协议与执行接口的契约（API-05 ~ API-08 / FR-51、FR-55、FR-56、FR-65）。

接口层只做三件事：**鉴权、把事实落库、投递任务**。所以这里验的是「端点为什么长这样」：

- **API-05 只收一个幂等键**：多给一个 `risk_level` 就直接 422。FR-55 的验收不是
  「服务端会忽略客户端的风险等级」，而是**客户端根本没有地方填**。
- **API-07 无入参**：事实全部重建，所以「审批之后替换内容」在 HTTP 面上不成立。
- **API-08 只给聚合指标**：响应体里不该出现原始行、凭据或物化后的配置。
- **取消走通用副作用入口**：发起取消只建准备单，审批齐备才提交并投递执行 Worker。

Worker 的真实行为由 `tests/test_side_effect_protocol.py` 与 `tests/test_execution_worker.py`
覆盖，这里用拦下的投递把两层分开。

本模块的夹具事务是**显式提交**的：一次用例要发多次 HTTP 请求，而每个请求结束时
应用层的事务会结束 —— 没提交的事实下一次请求就读不到了（`session` 夹具的
flush-only 语义在这里不够用）。
"""

import hashlib
import json
import uuid
from pathlib import Path
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from data_integration_platform.api.routes import executions as executions_routes
from data_integration_platform.application.file_assets import FileAssetService
from data_integration_platform.application.versions import PipelineVersionService
from data_integration_platform.domain.enums import (
    ApprovalDecisionValue,
    ExecutionStatus,
    FailureCategory,
    FailureStage,
    PipelineVersionStatus,
    ProjectRole,
    ScheduleMisfirePolicy,
    ScheduleMode,
)
from data_integration_platform.domain.harness import HarnessRole
from data_integration_platform.infrastructure.models import (
    ExecutionPreparation,
    ExecutionRun,
    FileAsset,
    HarnessCommandOutbox,
    PipelineVersion,
    ProjectMembership,
    ProjectRoleGrant,
    User,
)
from tests.conftest import ProjectContext
from tests.test_pipeline_versions import candidate, ensure_execution_endpoints

EXECUTE_SIDE_EFFECT = "data_integration_platform.side_effect_tasks.execute_side_effect"


@pytest.fixture
def dispatched(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, tuple[Any, ...]]]:
    """拦下 Celery 投递；接口契约测试不该依赖 Redis 与 Worker。"""
    recorded: list[tuple[str, tuple[Any, ...]]] = []

    async def fake_dispatch(delay: Any, *args: Any) -> None:
        task = getattr(delay, "__self__", None)
        recorded.append((getattr(task, "name", repr(delay)), args))

    monkeypatch.setattr(executions_routes, "dispatch", fake_dispatch)
    return recorded


def headers(actor: uuid.UUID) -> dict[str, str]:
    return {"X-Actor-Id": str(actor)}


async def _make_approver(
    session: AsyncSession, project: ProjectContext, role: HarnessRole
) -> uuid.UUID:
    user = User(subject=f"approver-{uuid.uuid4().hex[:8]}", display_name="审批人")
    session.add(user)
    await session.flush()
    session.add(
        ProjectMembership(project_id=project.project_id, user_id=user.id, role=ProjectRole.APPROVER)
    )
    session.add(ProjectRoleGrant(project_id=project.project_id, user_id=user.id, role=role))
    await session.flush()
    return user.id


async def make_approver(
    factory: async_sessionmaker[AsyncSession], project: ProjectContext, role: HarnessRole
) -> uuid.UUID:
    """造一个「能进审批台 + 持有该槽位」的主体，并**提交**它。"""
    async with factory() as session:
        async with session.begin():
            return await _make_approver(session, project, role)


async def make_outsider(factory: async_sessionmaker[AsyncSession]) -> uuid.UUID:
    async with factory() as session:
        async with session.begin():
            user = User(subject=f"outsider-{uuid.uuid4().hex[:8]}", display_name="外人")
            session.add(user)
            await session.flush()
            return user.id


async def promote(
    factory: async_sessionmaker[AsyncSession], project: ProjectContext, role: HarnessRole
) -> None:
    """把申请人本身提成「能进审批台 + 持有该槽」，用于验 SOD-01。

    不这么做的话，拦下他的会是「你没有这个槽位」——测到的就不是自批了。
    """
    async with factory() as session:
        async with session.begin():
            # 改既有成员行的角色，而不是再插一条：申请人本来就是项目成员，
            # 插第二条会撞上 (project_id, user_id) 的唯一约束。
            membership = await session.scalar(
                select(ProjectMembership).where(
                    ProjectMembership.project_id == project.project_id,
                    ProjectMembership.user_id == project.engineer_id,
                )
            )
            assert membership is not None
            membership.role = ProjectRole.APPROVER
            session.add(
                ProjectRoleGrant(
                    project_id=project.project_id, user_id=project.engineer_id, role=role
                )
            )
            await session.flush()


async def approved_version(
    factory: async_sessionmaker[AsyncSession],
    project: ProjectContext,
    tmp_path: Path,
    *,
    approve: bool = True,
) -> PipelineVersion:
    """冻结并（可选）批准一个版本；未批准的那个用来验「准备单只接受已批准的内容」。"""
    async with factory() as session:
        async with session.begin():
            await ensure_execution_endpoints(session, project)
            versions = PipelineVersionService(root=tmp_path / "artifacts")
            version = await versions.create(
                session,
                project_id=project.project_id,
                actor_id=project.engineer_id,
                name=f"orders-{uuid.uuid4().hex[:6]}",
                requirement="把订单同步到宽表，金额必须为正",
            )
            version.status = PipelineVersionStatus.GENERATED
            await session.flush()
            await versions.freeze(
                session,
                version_id=version.id,
                actor_id=project.engineer_id,
                candidate=candidate(),
            )
            request = await versions.submit_for_approval(
                session, version_id=version.id, actor_id=project.engineer_id
            )
            if approve:
                approver = await _make_approver(session, project, HarnessRole.DATA_APPROVER)
                await versions.decide_approval(
                    session,
                    approval_id=request.id,
                    actor_id=approver,
                    decision=ApprovalDecisionValue.APPROVE,
                    reason="核对无误",
                )
            return version


async def version_row(
    factory: async_sessionmaker[AsyncSession], version_id: uuid.UUID
) -> PipelineVersion:
    async with factory() as session:
        row = await session.get(PipelineVersion, version_id)
        assert row is not None
        return row


async def prepare_via_api(
    client: AsyncClient, version_id: uuid.UUID, actor: uuid.UUID, *, key: str | None = None
) -> dict[str, Any]:
    response = await client.post(
        f"/api/v1/pipeline-versions/{version_id}/execution-preparations",
        json={"idempotency_key": key or f"key-{uuid.uuid4().hex[:12]}"},
        headers=headers(actor),
    )
    assert response.status_code == 201, response.text
    return response.json()


async def approve(
    client: AsyncClient, preparation_id: str, approver: uuid.UUID, *, role: str = "DATA_APPROVER"
) -> dict[str, Any]:
    response = await client.post(
        f"/api/v1/execution-preparations/{preparation_id}/decisions",
        json={"role": role, "decision": "APPROVE", "reason": "核对无误"},
        headers=headers(approver),
    )
    assert response.status_code == 200, response.text
    return response.json()


async def submitted_run(
    factory: async_sessionmaker[AsyncSession], run_id: uuid.UUID, *, job_id: str = "job-1"
) -> None:
    """把运行推进到「已提交给引擎」。

    真实的这一步由执行 Worker 完成（`tests/test_execution_worker.py` 覆盖）；
    取消这一类用例只关心「作业已经在跑了之后怎么取消」，用不着再跑一遍提交链路。
    """
    async with factory() as session:
        async with session.begin():
            run = await session.get(ExecutionRun, run_id)
            assert run is not None
            run.status = ExecutionStatus.SUBMITTED
            run.seatunnel_job_id = job_id
            run.engine_status = "RUNNING"
            await session.flush()


async def committed_run(
    client: AsyncClient,
    factory: async_sessionmaker[AsyncSession],
    project: ProjectContext,
    tmp_path: Path,
) -> tuple[dict[str, Any], uuid.UUID]:
    """准备好一次执行并提交，返回 `(run, 审批人)`。"""
    version = await approved_version(factory, project, tmp_path)
    preparation = await prepare_via_api(client, version.id, project.engineer_id)
    approver = await make_approver(factory, project, HarnessRole.DATA_APPROVER)
    await approve(client, preparation["id"], approver)
    # 提交由**申请人**发起：审批人批的是内容，申请人负责任务的推进。
    # 两者在项目角色上是分开的（审批人不在 WRITABLE 里），这条分工由服务端强制。
    response = await client.post(
        f"/api/v1/execution-preparations/{preparation['id']}/commit",
        headers=headers(project.engineer_id),
    )
    assert response.status_code == 200, response.text
    return response.json(), approver


# ------------------------------------------------------------------ API-05
async def test_prepare_takes_only_an_idempotency_key(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    project: ProjectContext,
    tmp_path: Path,
) -> None:
    """准备单的全部事实由服务端推导，请求里多一个字段就是契约违规。"""
    version = await approved_version(session_factory, project, tmp_path)

    body = await prepare_via_api(client, version.id, project.engineer_id)
    assert body["status"] == "WAITING_APPROVAL"
    assert (
        body["artifact_sha256"] == (await version_row(session_factory, version.id)).artifact_sha256
    )
    assert body["required_roles"] == [HarnessRole.DATA_APPROVER.value]
    assert body["required_approvals"] == 1
    # 影响面与回滚方案都在准备单上，审批人据此判断「这次动什么」。
    assert body["impact_report"]["source"]["table_name"]
    assert body["rollback_plan"]["shadow_table"]

    injected = await client.post(
        f"/api/v1/pipeline-versions/{version.id}/execution-preparations",
        json={"idempotency_key": f"key-{uuid.uuid4().hex[:12]}", "risk_level": "P3"},
        headers=headers(project.engineer_id),
    )
    assert injected.status_code == 422


async def test_prepare_is_idempotent_by_key(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    project: ProjectContext,
    tmp_path: Path,
) -> None:
    """同一个幂等键重试只会拿到同一张准备单，不会造出第二张待审批的单子。"""
    version = await approved_version(session_factory, project, tmp_path)
    key = f"key-{uuid.uuid4().hex[:12]}"

    first = await prepare_via_api(client, version.id, project.engineer_id, key=key)
    again = await prepare_via_api(client, version.id, project.engineer_id, key=key)

    assert first["id"] == again["id"]
    async with session_factory() as session:
        rows = (
            await session.scalars(
                select(ExecutionPreparation).where(ExecutionPreparation.idempotency_key == key)
            )
        ).all()
    assert len(rows) == 1


async def test_prepare_rejects_unapproved_version(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    project: ProjectContext,
    tmp_path: Path,
) -> None:
    """还没被人批准过的内容不该先进到执行准备里。"""
    version = await approved_version(session_factory, project, tmp_path, approve=False)

    response = await client.post(
        f"/api/v1/pipeline-versions/{version.id}/execution-preparations",
        json={"idempotency_key": f"key-{uuid.uuid4().hex[:12]}"},
        headers=headers(project.engineer_id),
    )
    assert response.status_code == 400
    assert "只有已批准" in response.json()["detail"]


# ------------------------------------------------------------------ API-06
async def test_decide_requires_the_right_slot_and_no_self_approval(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    project: ProjectContext,
    tmp_path: Path,
) -> None:
    version = await approved_version(session_factory, project, tmp_path)
    preparation = await prepare_via_api(client, version.id, project.engineer_id)
    wrong_slot = await make_approver(session_factory, project, HarnessRole.SECURITY_APPROVER)
    await promote(session_factory, project, HarnessRole.DATA_APPROVER)

    self_approval = await client.post(
        f"/api/v1/execution-preparations/{preparation['id']}/decisions",
        json={"role": "DATA_APPROVER", "decision": "APPROVE", "reason": "自己批"},
        headers=headers(project.engineer_id),
    )
    assert self_approval.status_code == 403
    assert "SOD-01" in self_approval.json()["detail"]

    unauthorized = await client.post(
        f"/api/v1/execution-preparations/{preparation['id']}/decisions",
        json={"role": "DATA_APPROVER", "decision": "APPROVE", "reason": "拿错槽"},
        headers=headers(wrong_slot),
    )
    assert unauthorized.status_code == 403
    assert "职责槽" in unauthorized.json()["detail"]


async def test_decide_completes_the_preparation(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    project: ProjectContext,
    tmp_path: Path,
) -> None:
    version = await approved_version(session_factory, project, tmp_path)
    preparation = await prepare_via_api(client, version.id, project.engineer_id)
    approver = await make_approver(session_factory, project, HarnessRole.DATA_APPROVER)

    body = await approve(client, preparation["id"], approver)
    assert body["status"] == "READY"


# ------------------------------------------------------------------ API-07
async def test_commit_creates_run_and_outbox_without_arguments(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    project: ProjectContext,
    tmp_path: Path,
) -> None:
    """提交**无入参**；执行记录与待投递命令在同一事务里落库（SPEC §7.4）。"""
    run, _ = await committed_run(client, session_factory, project, tmp_path)

    assert run["status"] == ExecutionStatus.QUEUED.value
    assert run["seatunnel_job_id"] is None
    async with session_factory() as session:
        outbox = (
            await session.scalars(
                select(HarnessCommandOutbox).where(
                    HarnessCommandOutbox.preparation_id == uuid.UUID(run["harness_preparation_id"])
                )
            )
        ).all()
    assert len(outbox) == 1
    assert outbox[0].status.value == "PENDING"
    # 命令载荷里没有配置内容，也没有凭据 —— 物化发生在 Worker 里。
    assert "password" not in str(outbox[0].command_payload)


async def test_commit_before_approval_is_refused(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    project: ProjectContext,
    tmp_path: Path,
) -> None:
    version = await approved_version(session_factory, project, tmp_path)
    preparation = await prepare_via_api(client, version.id, project.engineer_id)

    response = await client.post(
        f"/api/v1/execution-preparations/{preparation['id']}/commit",
        headers=headers(project.engineer_id),
    )
    assert response.status_code == 400
    assert "只有 READY" in response.json()["detail"]


# ------------------------------------------------------------------ API-08
async def test_execution_run_projection_is_aggregate_only(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    project: ProjectContext,
    tmp_path: Path,
) -> None:
    run, approver = await committed_run(client, session_factory, project, tmp_path)

    fetched = await client.get(f"/api/v1/execution-runs/{run['id']}", headers=headers(approver))
    assert fetched.status_code == 200
    body = fetched.json()
    # 引擎没给指标就是 null，不用 0 冒充（FR-61）。
    assert body["source_rows"] is None
    assert body["sink_rows"] is None
    assert body["failure_category"] is None
    assert body["diagnosis"] is None
    assert "password" not in fetched.text
    assert "hocon" not in fetched.text.lower()

    listed = await client.get(
        f"/api/v1/projects/{project.project_id}/execution-runs", headers=headers(approver)
    )
    assert listed.status_code == 200
    assert [item["id"] for item in listed.json()] == [run["id"]]


async def test_execution_run_is_not_visible_across_projects(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    project: ProjectContext,
    tmp_path: Path,
) -> None:
    run, _ = await committed_run(client, session_factory, project, tmp_path)
    outsider = await make_outsider(session_factory)

    response = await client.get(f"/api/v1/execution-runs/{run['id']}", headers=headers(outsider))
    assert response.status_code == 403


# ------------------------------------------------------ 受管取消（第 9 类副作用）
async def test_cancellation_is_a_managed_side_effect(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    project: ProjectContext,
    tmp_path: Path,
    dispatched: list[tuple[str, tuple[Any, ...]]],
) -> None:
    """取消不是「直接停作业」：它先建准备单，审批齐备才提交并投递 Worker。"""
    run, approver = await committed_run(client, session_factory, project, tmp_path)
    await submitted_run(session_factory, uuid.UUID(run["id"]))

    requested = await client.post(
        f"/api/v1/execution-runs/{run['id']}/cancellations",
        json={"reason": "源库在维护"},
        headers=headers(project.engineer_id),
    )
    assert requested.status_code == 201, requested.text
    side_effect = requested.json()
    assert side_effect["operation"] == "EXECUTION_CANCEL"
    assert side_effect["status"] == "WAITING_APPROVAL"
    assert side_effect["aggregate_id"] == run["id"]
    # 申请阶段零外部副作用：引擎还没被碰过。
    assert side_effect["execution_status"] == "PENDING"

    decided = await client.post(
        f"/api/v1/execution-runs/{run['id']}/cancellations/decisions",
        json={"role": "DATA_APPROVER", "decision": "APPROVE", "reason": "同意取消"},
        headers=headers(approver),
    )
    assert decided.status_code == 200, decided.text
    assert decided.json()["status"] == "COMMITTED"
    assert dispatched[-1][0] == EXECUTE_SIDE_EFFECT

    # 可以从通用查询面看到，不必先知道它属于哪张业务表。
    listed = await client.get(
        f"/api/v1/projects/{project.project_id}/side-effect-preparations",
        headers=headers(approver),
    )
    assert [item["operation"] for item in listed.json()] == ["EXECUTION_CANCEL"]


async def test_cancellation_requires_a_submitted_job(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    project: ProjectContext,
    tmp_path: Path,
) -> None:
    """还没提交到引擎的执行没有可取消的作业 —— 拒绝，而不是发一条空命令出去。"""
    run, _ = await committed_run(client, session_factory, project, tmp_path)

    response = await client.post(
        f"/api/v1/execution-runs/{run['id']}/cancellations",
        json={"reason": "没有作业可取消"},
        headers=headers(project.engineer_id),
    )
    assert response.status_code == 400
    assert "没有可取消的作业" in response.json()["detail"]


# ------------------------------------------------------ 版本审批与调度（HTTP 面）
async def test_version_approval_decision_is_reachable_over_http(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    project: ProjectContext,
    tmp_path: Path,
) -> None:
    version = await approved_version(session_factory, project, tmp_path, approve=False)
    approver = await make_approver(session_factory, project, HarnessRole.DATA_APPROVER)

    pending = await client.get(
        f"/api/v1/projects/{project.project_id}/approval-requests",
        params={"status": "PENDING"},
        headers=headers(approver),
    )
    assert pending.status_code == 200, pending.text
    approval_id = next(
        item["id"] for item in pending.json() if item["version_id"] == str(version.id)
    )

    decided = await client.post(
        f"/api/v1/approval-requests/{approval_id}/decisions",
        json={"decision": "APPROVE", "reason": "核对无误"},
        headers=headers(approver),
    )
    assert decided.status_code == 200, decided.text
    assert decided.json()["status"] == "APPROVED"
    assert (await version_row(session_factory, version.id)).status is PipelineVersionStatus.APPROVED


async def test_schedule_configure_read_and_toggle(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    project: ProjectContext,
    tmp_path: Path,
) -> None:
    """调度配置的 HTTP 面：没配过返回 null，配了能读回来，启停按「此刻」重算。"""
    version = await approved_version(session_factory, project, tmp_path)

    empty = await client.get(
        f"/api/v1/pipeline-versions/{version.id}/schedule", headers=headers(project.engineer_id)
    )
    assert empty.status_code == 200
    assert empty.json() is None

    configured = await client.put(
        f"/api/v1/pipeline-versions/{version.id}/schedule",
        json={
            "mode": ScheduleMode.BATCH.value,
            "cron_expression": "0 0 2 * * *",
            "timezone": "Asia/Shanghai",
            "misfire_policy": ScheduleMisfirePolicy.SKIP.value,
        },
        headers=headers(project.engineer_id),
    )
    assert configured.status_code == 200, configured.text
    body = configured.json()
    assert body["enabled"] is False
    # 秒在前的写法必须被读成「每天 2 点」；读成「每月 2 日」会让下一次触发落在别的日子上，
    # 而任务照跑、不报任何错。
    assert body["next_run_at"] is not None

    read_back = await client.get(
        f"/api/v1/pipeline-versions/{version.id}/schedule", headers=headers(project.engineer_id)
    )
    assert read_back.status_code == 200
    assert read_back.json()["id"] == body["id"]
    assert read_back.json()["cron_expression"] == "0 0 2 * * *"

    toggled = await client.post(
        f"/api/v1/pipeline-versions/{version.id}/schedule/enabled",
        json={"enabled": True},
        headers=headers(project.engineer_id),
    )
    assert toggled.status_code == 200, toggled.text
    assert toggled.json()["enabled"] is True


async def test_execution_preparation_detail_is_project_scoped(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    project: ProjectContext,
    tmp_path: Path,
) -> None:
    version = await approved_version(session_factory, project, tmp_path)
    preparation = await prepare_via_api(client, version.id, project.engineer_id)
    approver = await make_approver(session_factory, project, HarnessRole.DATA_APPROVER)
    await approve(client, preparation["id"], approver)

    detail = await client.get(
        f"/api/v1/execution-preparations/{preparation['id']}", headers=headers(approver)
    )
    assert detail.status_code == 200
    assert detail.json()["status"] == "READY"

    outsider = await make_outsider(session_factory)
    denied = await client.get(
        f"/api/v1/execution-preparations/{preparation['id']}", headers=headers(outsider)
    )
    assert denied.status_code == 403


# ------------------------------------------------------ 受管交付（T-40）
async def finish_run(
    factory: async_sessionmaker[AsyncSession],
    run_id: uuid.UUID,
    *,
    status: ExecutionStatus = ExecutionStatus.SUCCEEDED,
) -> None:
    """把运行推进到终态。

    真实的那一步由状态对账完成（M6 的 T-43 交付它）；交付这一类用例只关心
    「跑出结论之后怎么交付」，用不着先把对账实现出来。
    """
    async with factory() as session:
        async with session.begin():
            run = await session.get(ExecutionRun, run_id)
            assert run is not None
            run.status = status
            run.engine_status = "FINISHED"
            run.source_rows = 120
            run.sink_rows = 118
            run.failure_category = FailureCategory.SINK_WRITE
            run.failure_stage = FailureStage.SINK
            run.retryable = False
            run.diagnosis = {"reason": f"连接串 mysql://reader:{'SECRET'}=@db/source 不可用"}
            await session.flush()


async def test_delivery_writes_a_managed_report_and_is_idempotent(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    project: ProjectContext,
    tmp_path: Path,
) -> None:
    """交付物是受管文件资产：挂在执行上、可校验摘要、重复交付只留一份。"""
    run, _ = await committed_run(client, session_factory, project, tmp_path)
    await finish_run(session_factory, uuid.UUID(run["id"]))

    first = await client.post(
        f"/api/v1/execution-runs/{run['id']}/deliveries", headers=headers(project.engineer_id)
    )
    assert first.status_code == 201, first.text
    asset = first.json()
    assert asset["execution_run_id"] == run["id"]
    assert asset["format"] == "JSON"
    # 交付物是这条链路的产出，不是输入。
    assert asset["endpoint_role"] == "SINK"

    again = await client.post(
        f"/api/v1/execution-runs/{run['id']}/deliveries", headers=headers(project.engineer_id)
    )
    assert again.status_code == 201
    assert again.json()["id"] == asset["id"], "重复交付不该再落一份内容相同的文件"

    listed = await client.get(
        f"/api/v1/execution-runs/{run['id']}/deliveries", headers=headers(project.engineer_id)
    )
    assert [item["id"] for item in listed.json()] == [asset["id"]]

    # 摘要是内容摘要：读回来重算一遍必须一致（文件被改过就能发现）。
    root = FileAssetService().root()
    async with session_factory() as session:
        row = await session.get(FileAsset, uuid.UUID(asset["id"]))
        assert row is not None
    raw = (root / row.storage_ref).read_bytes()
    assert hashlib.sha256(raw).hexdigest() == asset["checksum_sha256"]


async def test_report_keeps_missing_metrics_and_drops_credentials(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    project: ProjectContext,
    tmp_path: Path,
) -> None:
    """报告里「没量到」是 null、不是 0；诊断文本里的凭据被擦掉。"""
    run, _ = await committed_run(client, session_factory, project, tmp_path)
    await finish_run(session_factory, uuid.UUID(run["id"]))

    delivered = await client.post(
        f"/api/v1/execution-runs/{run['id']}/deliveries", headers=headers(project.engineer_id)
    )
    assert delivered.status_code == 201, delivered.text
    asset_id = delivered.json()["id"]

    async with session_factory() as session:
        row = await session.get(FileAsset, uuid.UUID(asset_id))
        assert row is not None
    payload = json.loads((FileAssetService().root() / row.storage_ref).read_text("utf-8"))

    assert payload["schema_version"] == 1
    assert payload["execution"]["status"] == ExecutionStatus.SUCCEEDED.value
    assert payload["metrics"]["source_rows"] == 120
    # 没量到的指标留空，不填 0 冒充已知（FR-61）。
    assert payload["metrics"]["sink_bytes"] is None
    assert payload["failure"]["category"] == FailureCategory.SINK_WRITE.value
    # 质量报告属 M6 的 T-41，还没有就交付 null，不编一份空的出来。
    assert payload["quality"] is None
    # 诊断里的连接串凭据被擦掉，但原因可读。
    assert "SECRET" not in json.dumps(payload)
    assert "不可用" in payload["failure"]["diagnosis"]["reason"]


async def test_delivery_refuses_a_running_execution(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    project: ProjectContext,
    tmp_path: Path,
) -> None:
    """跑着的执行没有结论，交付一份「还没跑完」的报告等于给审计一个会过期的答案。"""
    run, _ = await committed_run(client, session_factory, project, tmp_path)

    response = await client.post(
        f"/api/v1/execution-runs/{run['id']}/deliveries", headers=headers(project.engineer_id)
    )
    assert response.status_code == 400
    assert "只有跑出结论" in response.json()["detail"]


async def test_delivery_is_not_reachable_across_projects(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    project: ProjectContext,
    tmp_path: Path,
) -> None:
    run, _ = await committed_run(client, session_factory, project, tmp_path)
    await finish_run(session_factory, uuid.UUID(run["id"]))
    outsider = await make_outsider(session_factory)

    response = await client.post(
        f"/api/v1/execution-runs/{run['id']}/deliveries", headers=headers(outsider)
    )
    assert response.status_code == 403
