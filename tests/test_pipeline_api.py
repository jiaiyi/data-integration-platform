"""管道版本与生成接口的契约（API-01 ~ API-04）。

接口层只做三件事：**鉴权、把事实落库、投递任务**。所以这里验的是：
创建版本与修订的响应契约、设计审查视图读的是**冻结制品**、发起生成会投递任务、
以及「未冻结就提交审批」被拒。

Worker 的行为由 `tests/test_generation_pipeline.py` 覆盖，这里用拦下的投递把两者分开。
"""

import uuid
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from data_integration_platform.api.routes import agent_runs as agent_runs_routes
from data_integration_platform.application.versions import PipelineVersionService
from data_integration_platform.domain.enums import (
    ConnectorType,
    DataEndpointRole,
    PipelineVersionStatus,
)
from data_integration_platform.infrastructure.models import PipelineVersion
from tests.conftest import ProjectContext
from tests.test_generation_pipeline import (
    HOCON,
    SINK_COLUMNS,
    SOURCE_COLUMNS,
    candidate_json,
    make_connection,
    make_profile,
)


@pytest.fixture
def dispatched(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, tuple[Any, ...]]]:
    """拦下 Celery 投递；接口契约测试不该依赖 Redis 与 Worker。"""
    recorded: list[tuple[str, tuple[Any, ...]]] = []

    async def fake_dispatch(delay: Any, *args: Any) -> None:
        # `delay` 是绑定在 Task 上的 `.delay`，任务名在它的 `__self__.name` 上。
        task = getattr(delay, "__self__", None)
        recorded.append((getattr(task, "name", repr(delay)), args))

    monkeypatch.setattr(agent_runs_routes, "dispatch", fake_dispatch)
    return recorded


RUN_GENERATION = "data_integration_platform.generation_tasks.run_generation"


def headers(actor: uuid.UUID) -> dict[str, str]:
    return {"X-Actor-Id": str(actor)}


async def create_pipeline(client: AsyncClient, project: ProjectContext) -> dict[str, Any]:
    response = await client.post(
        "/api/v1/pipelines",
        json={
            "project_id": str(project.project_id),
            "name": f"orders-sync-{uuid.uuid4().hex[:6]}",
            "requirement": "把订单同步到宽表，金额必须为正",
        },
        headers=headers(project.engineer_id),
    )
    assert response.status_code == 201, response.text
    return response.json()


# ------------------------------------------------------------------ API-01
async def test_create_pipeline_returns_the_first_version(
    client: AsyncClient, project: ProjectContext
) -> None:
    body = await create_pipeline(client, project)

    assert body["version_number"] == 1
    assert body["status"] == PipelineVersionStatus.DRAFT.value
    assert body["parent_version_id"] is None
    assert body["artifact_sha256"] is None
    assert body["lock_version"] == 0


async def test_viewer_cannot_create_a_pipeline(
    client: AsyncClient, session: AsyncSession, project: ProjectContext
) -> None:
    from data_integration_platform.domain.enums import ProjectRole
    from data_integration_platform.infrastructure.models import ProjectMembership, User

    viewer = User(subject=f"viewer-{uuid.uuid4().hex[:6]}", display_name="王五")
    session.add(viewer)
    await session.flush()
    session.add(
        ProjectMembership(project_id=project.project_id, user_id=viewer.id, role=ProjectRole.VIEWER)
    )
    await session.flush()

    response = await client.post(
        "/api/v1/pipelines",
        json={
            "project_id": str(project.project_id),
            "name": "不该建成",
            "requirement": "只读角色不该能创建版本",
        },
        headers=headers(viewer.id),
    )

    assert response.status_code == 403


# ------------------------------------------------------------------ 修订
async def test_revision_creates_v_plus_one(client: AsyncClient, project: ProjectContext) -> None:
    first = await create_pipeline(client, project)

    response = await client.post(
        f"/api/v1/pipeline-versions/{first['id']}/revisions",
        json={"requirement": "金额上限改成 100 万"},
        headers=headers(project.engineer_id),
    )

    assert response.status_code == 201, response.text
    revision = response.json()
    assert revision["version_number"] == 2
    assert revision["parent_version_id"] == first["id"]
    assert revision["pipeline_id"] == first["pipeline_id"]
    assert revision["status"] == PipelineVersionStatus.DRAFT.value


# ------------------------------------------------------------------ API-04
async def test_design_view_is_empty_before_freeze(
    client: AsyncClient, project: ProjectContext
) -> None:
    """还没冻结就没有内容可审 —— 返回空值，不造一份占位内容冒充。"""
    version = await create_pipeline(client, project)

    response = await client.get(
        f"/api/v1/pipelines/versions/{version['id']}/design",
        headers=headers(project.engineer_id),
    )

    assert response.status_code == 200
    body = response.json()
    assert body["artifact_sha256"] is None
    assert body["plan"] is None
    assert body["hocon"] is None


async def test_design_view_serves_the_frozen_artifacts(
    client: AsyncClient, session: AsyncSession, project: ProjectContext
) -> None:
    """审批人看到的必须是**冻结在盘上的字节**，不是内存里的候选。"""
    version = await create_pipeline(client, project)
    frozen = await freeze(session, version_id=uuid.UUID(version["id"]), project=project)
    await session.commit()

    response = await client.get(
        f"/api/v1/pipelines/versions/{version['id']}/design",
        headers=headers(project.engineer_id),
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == PipelineVersionStatus.VALIDATED.value
    assert body["artifact_sha256"] == frozen.artifact_sha256
    assert body["hocon"] == HOCON
    assert body["plan"]["target"]["table_name"] == "orders_wide"


async def test_design_view_hides_the_version_from_other_projects(
    client: AsyncClient, project: ProjectContext
) -> None:
    version = await create_pipeline(client, project)

    response = await client.get(
        f"/api/v1/pipelines/versions/{version['id']}/design",
        headers=headers(uuid.uuid4()),
    )

    assert response.status_code == 403


# ------------------------------------------------------------------ 提交审批
async def test_submit_for_approval_binds_the_frozen_digest(
    client: AsyncClient, session: AsyncSession, project: ProjectContext
) -> None:
    version = await create_pipeline(client, project)
    frozen = await freeze(session, version_id=uuid.UUID(version["id"]), project=project)
    await session.commit()

    response = await client.post(
        f"/api/v1/pipeline-versions/{version['id']}/approval-requests",
        headers=headers(project.engineer_id),
    )

    assert response.status_code == 201, response.text
    assert response.json()["artifact_sha256"] == frozen.artifact_sha256


async def test_submit_for_approval_requires_a_frozen_version(
    client: AsyncClient, project: ProjectContext
) -> None:
    version = await create_pipeline(client, project)

    response = await client.post(
        f"/api/v1/pipeline-versions/{version['id']}/approval-requests",
        headers=headers(project.engineer_id),
    )

    assert response.status_code == 400
    assert "尚未冻结" in response.json()["detail"]


async def test_submit_for_approval_is_not_repeatable(
    client: AsyncClient, session: AsyncSession, project: ProjectContext
) -> None:
    version = await create_pipeline(client, project)
    await freeze(session, version_id=uuid.UUID(version["id"]), project=project)
    await session.commit()
    first = await client.post(
        f"/api/v1/pipeline-versions/{version['id']}/approval-requests",
        headers=headers(project.engineer_id),
    )
    assert first.status_code == 201

    second = await client.post(
        f"/api/v1/pipeline-versions/{version['id']}/approval-requests",
        headers=headers(project.engineer_id),
    )

    assert second.status_code == 400
    assert "已有待审批" in second.json()["detail"]


# ------------------------------------------------------------------ API-02 / API-03
@pytest.fixture
async def sources(session: AsyncSession, project: ProjectContext) -> list[uuid.UUID]:
    """一对可用的源/目标 Profile；发起生成的前提。"""
    source_connection = await make_connection(
        session,
        project=project,
        name=f"mysql-{uuid.uuid4().hex[:6]}",
        connector=ConnectorType.MYSQL,
        role=DataEndpointRole.SOURCE,
    )
    sink_connection = await make_connection(
        session,
        project=project,
        name=f"doris-{uuid.uuid4().hex[:6]}",
        connector=ConnectorType.DORIS,
        role=DataEndpointRole.SINK,
    )
    source = await make_profile(
        session,
        connection=source_connection,
        role=DataEndpointRole.SOURCE,
        table_name="orders",
        columns=SOURCE_COLUMNS,
    )
    sink = await make_profile(
        session,
        connection=sink_connection,
        role=DataEndpointRole.SINK,
        table_name="orders_wide",
        columns=SINK_COLUMNS,
    )
    return [source.id, sink.id]


async def test_start_agent_run_records_and_dispatches(
    client: AsyncClient,
    session: AsyncSession,
    project: ProjectContext,
    sources: list[uuid.UUID],
    dispatched: list[tuple[str, tuple[Any, ...]]],
) -> None:
    version = await create_pipeline(client, project)
    await session.commit()

    response = await client.post(
        f"/api/v1/pipeline-versions/{version['id']}/agent-runs",
        json={"profile_ids": [str(item) for item in sources]},
        headers=headers(project.engineer_id),
    )

    assert response.status_code == 201, response.text
    body = response.json()
    assert body["status"] == "QUEUED"
    assert body["thread_id"] == body["id"]
    # 投递发生在事务提交之后，且载荷只带运行 ID。
    assert [name for name, _ in dispatched] == [RUN_GENERATION]
    assert dispatched[0][1] == (body["id"],)
    # 版本随即进入 PROFILING：受理与「正在生成」是同一件事的两面。
    detail = await client.get(
        f"/api/v1/pipelines/versions/{version['id']}/design",
        headers=headers(project.engineer_id),
    )
    assert detail.json()["status"] == PipelineVersionStatus.PROFILING.value


async def test_answer_dispatches_a_resume_with_the_answer(
    client: AsyncClient,
    session: AsyncSession,
    project: ProjectContext,
    sources: list[uuid.UUID],
    dispatched: list[tuple[str, tuple[Any, ...]]],
) -> None:
    from data_integration_platform.domain.enums import AgentRunStatus
    from data_integration_platform.infrastructure.models import AgentRun

    version = await create_pipeline(client, project)
    started = await client.post(
        f"/api/v1/pipeline-versions/{version['id']}/agent-runs",
        json={"profile_ids": [str(item) for item in sources]},
        headers=headers(project.engineer_id),
    )
    run_id = started.json()["id"]
    # 模拟 Worker 已跑到澄清中断。
    run = await session.get(AgentRun, uuid.UUID(run_id))
    assert run is not None
    run.status = AgentRunStatus.WAITING_INPUT
    run.pending_question = "增量水位列未指定，请指定"
    await session.commit()

    response = await client.post(
        f"/api/v1/agent-runs/{run_id}/answers",
        json={"answer": "用 updated_at"},
        headers=headers(project.engineer_id),
    )

    assert response.status_code == 200, response.text
    assert response.json()["status"] == AgentRunStatus.QUEUED.value
    assert response.json()["clarification_rounds"] == 1
    assert dispatched[-1] == (RUN_GENERATION, (run_id, "用 updated_at"))


async def test_answer_is_rejected_when_the_run_is_not_waiting(
    client: AsyncClient,
    session: AsyncSession,
    project: ProjectContext,
    sources: list[uuid.UUID],
    dispatched: list[tuple[str, tuple[Any, ...]]],
) -> None:
    version = await create_pipeline(client, project)
    started = await client.post(
        f"/api/v1/pipeline-versions/{version['id']}/agent-runs",
        json={"profile_ids": [str(item) for item in sources]},
        headers=headers(project.engineer_id),
    )

    response = await client.post(
        f"/api/v1/agent-runs/{started.json()['id']}/answers",
        json={"answer": "抢答"},
        headers=headers(project.engineer_id),
    )

    assert response.status_code == 400
    assert "不允许从 QUEUED 转换到 QUEUED" in response.json()["detail"]


async def test_get_agent_run_exposes_the_shared_budget_counters(
    client: AsyncClient,
    session: AsyncSession,
    project: ProjectContext,
    sources: list[uuid.UUID],
    dispatched: list[tuple[str, tuple[Any, ...]]],
) -> None:
    version = await create_pipeline(client, project)
    started = await client.post(
        f"/api/v1/pipeline-versions/{version['id']}/agent-runs",
        json={"profile_ids": [str(item) for item in sources]},
        headers=headers(project.engineer_id),
    )

    response = await client.get(
        f"/api/v1/agent-runs/{started.json()['id']}", headers=headers(project.engineer_id)
    )

    assert response.status_code == 200
    body = response.json()
    assert body["generation_candidates"] == 0
    assert body["generation_repairs"] == 0


# ------------------------------------------------------------------ 辅助
async def freeze(
    session: AsyncSession, *, version_id: uuid.UUID, project: ProjectContext
) -> PipelineVersion:
    """把版本推到可冻结的状态再冻结 —— 门禁与引擎检查由别的测试覆盖。"""
    version = await session.get(PipelineVersion, version_id)
    assert version is not None
    version.status = PipelineVersionStatus.GENERATED
    await session.flush()
    from data_integration_platform.application.generation import GeneratedCandidate

    candidate = GeneratedCandidate.model_validate_json(candidate_json())
    return await PipelineVersionService().freeze(
        session, version_id=version_id, actor_id=project.engineer_id, candidate=candidate
    )
