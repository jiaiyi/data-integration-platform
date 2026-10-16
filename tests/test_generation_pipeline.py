"""生成链路的 Worker 侧：受理 → 跑状态机 → 冻结（FR-15、FR-25 ~ FR-35）。

用剧本化的假模型与假引擎跑，因为要验的恰恰是**边界**：
模型反问时停在等澄清、澄清后从同一 thread_id 恢复、门禁通过才冻结、
**引擎不可用时冻结必须被阻止**（FR-35 的验收口径）。
真模型、真引擎的对接口径分别由 `tests/test_engine_check.py` 与
`tests/integration/test_engine_check_live.py` 覆盖。
"""

import json
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from langgraph.checkpoint.memory import InMemorySaver
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from data_integration_platform.application.generation_runs import GenerationRunService
from data_integration_platform.application.policy import GateFinding, GateKind
from data_integration_platform.domain.enums import (
    AgentRunStatus,
    ConnectionStatus,
    ConnectorType,
    DataEndpointRole,
    DataProfileStatus,
    PipelineVersionStatus,
    ProjectRole,
)
from data_integration_platform.domain.errors import DomainError, ResourceNotFound
from data_integration_platform.generation_tasks import _run_generation
from data_integration_platform.infrastructure.models import (
    AgentRun,
    DataConnection,
    DataProfile,
    Pipeline,
    PipelineVersion,
    Project,
)
from tests.conftest import ProjectContext
from tests.test_generation_graph import ScriptedAdapter

PASSWORD = "s3cret-mysql-password"
QUALITY_CODE = "E_POSITIVE"

SOURCE_COLUMNS = [
    {"name": "id", "type": "bigint", "nullable": False},
    {"name": "amount", "type": "decimal(18,2)", "nullable": True},
    {"name": "updated_at", "type": "datetime", "nullable": True},
]
SINK_COLUMNS = [
    {"name": "order_id", "type": "bigint", "nullable": False},
    {"name": "amount", "type": "decimal(18,2)", "nullable": True},
]

PLAN: dict[str, Any] = {
    "source": {"schema_name": "source_db", "table_name": "orders"},
    "target": {"schema_name": "dw", "table_name": "orders_wide", "primary_key": ["order_id"]},
    "mappings": [
        {"source_column": "id", "target_column": "order_id"},
        {"source_column": "amount", "target_column": "amount"},
    ],
    "transforms": [],
    "quality_rules": [{"column": "amount", "kind": "POSITIVE", "error_code": QUALITY_CODE}],
    "incremental": {"mode": "BATCH"},
    "dedup_keys": ["order_id"],
}

HOCON = f"""env {{
  job.mode = "BATCH"
}}
source {{
  Jdbc {{
    url = "jdbc:mysql://mysql:3306/source_db"
    user = "etl"
    password = "${{MYSQL_PASSWORD}}"
  }}
}}
sink {{
  Doris {{
    table = "orders_wide"
    {QUALITY_CODE} = true
  }}
}}
"""


class FakeEngine:
    """可切换结论的假引擎；默认放行。"""

    def __init__(self, finding: GateFinding | None = None) -> None:
        self.finding = finding or GateFinding(GateKind.ENGINE_CHECK, True, "引擎已接受")
        self.calls = 0

    async def check(self, *, hocon: str) -> GateFinding:
        self.calls += 1
        return self.finding


def candidate_json() -> str:
    return json.dumps({"plan": PLAN, "hocon": HOCON}, ensure_ascii=False)


def question_json(text: str = "增量水位列未指定，请指定") -> str:
    return json.dumps({"question": text}, ensure_ascii=False)


async def _connection(
    session: AsyncSession,
    *,
    project: ProjectContext,
    name: str,
    connector: ConnectorType,
    role: DataEndpointRole,
) -> DataConnection:
    connection = DataConnection(
        project_id=project.project_id,
        name=name,
        connector_type=connector,
        endpoint_role=role,
        status=ConnectionStatus.READY,
        created_by=project.engineer_id,
    )
    session.add(connection)
    await session.flush()
    return connection


async def _profile(
    session: AsyncSession,
    *,
    connection: DataConnection,
    role: DataEndpointRole,
    table_name: str,
    columns: list[dict[str, Any]],
    expires_in: timedelta = timedelta(days=1),
    status: DataProfileStatus = DataProfileStatus.READY,
) -> DataProfile:
    profile = DataProfile(
        connection_id=connection.id,
        schema_name="source_db" if role is DataEndpointRole.SOURCE else "dw",
        table_name=table_name,
        endpoint_role=role,
        status=status,
        columns=columns,
        primary_keys=["id"] if role is DataEndpointRole.SOURCE else ["order_id"],
        # 脱敏样本里是假数据，测试要证明它**没有**进入提示词。
        masked_sample=[{"id": "1", "amount": "***"}],
        sample_size=1,
        expires_at=datetime.now(UTC) + expires_in,
    )
    session.add(profile)
    await session.flush()
    return profile


class PipelineFixture:
    def __init__(
        self,
        *,
        project: ProjectContext,
        version: PipelineVersion,
        profiles: list[DataProfile],
        source: DataProfile,
        sink: DataProfile,
    ) -> None:
        self.project = project
        self.version = version
        self.profiles = profiles
        self.source = source
        self.sink = sink

    @property
    def profile_ids(self) -> list[uuid.UUID]:
        return [profile.id for profile in self.profiles]


@pytest.fixture
async def pipeline(session: AsyncSession, project: ProjectContext) -> PipelineFixture:
    """一个已冻结之前、可供生成的版本：两张表 + 各自的连接与 Profile。"""
    source_connection = await _connection(
        session,
        project=project,
        name=f"mysql-{uuid.uuid4().hex[:6]}",
        connector=ConnectorType.MYSQL,
        role=DataEndpointRole.SOURCE,
    )
    sink_connection = await _connection(
        session,
        project=project,
        name=f"doris-{uuid.uuid4().hex[:6]}",
        connector=ConnectorType.DORIS,
        role=DataEndpointRole.SINK,
    )
    source = await _profile(
        session,
        connection=source_connection,
        role=DataEndpointRole.SOURCE,
        table_name="orders",
        columns=SOURCE_COLUMNS,
    )
    sink = await _profile(
        session,
        connection=sink_connection,
        role=DataEndpointRole.SINK,
        table_name="orders_wide",
        columns=SINK_COLUMNS,
    )
    pipeline_row = Pipeline(project_id=project.project_id, name=f"sync-{uuid.uuid4().hex[:6]}")
    session.add(pipeline_row)
    await session.flush()
    version = PipelineVersion(
        pipeline_id=pipeline_row.id,
        version_number=1,
        status=PipelineVersionStatus.DRAFT,
        requirement="把订单同步到宽表，金额必须为正",
        created_by=project.engineer_id,
    )
    session.add(version)
    await session.flush()
    return PipelineFixture(
        project=project,
        version=version,
        profiles=[source, sink],
        source=source,
        sink=sink,
    )


async def start(session: AsyncSession, pipeline: PipelineFixture, **overrides: Any) -> AgentRun:
    values: dict[str, Any] = {"profile_ids": pipeline.profile_ids}
    values.update(overrides)
    return await GenerationRunService().start(
        session,
        version_id=pipeline.version.id,
        actor_id=pipeline.project.engineer_id,
        **values,
    )


async def run_worker(
    session_factory: async_sessionmaker[AsyncSession],
    run: AgentRun,
    *,
    adapter: Any,
    engine: Any,
    checkpointer: Any,
    answer: str | None = None,
) -> Any:
    """跑一次 Worker；会话工厂注入成内存库，免得依赖本机 PostgreSQL。"""
    return await _run_generation(
        run.id,
        answer=answer,
        checkpointer=checkpointer,
        adapter=adapter,
        engine=engine,
        session_factory=session_factory,
    )


# ------------------------------------------------------------------ 受理
async def test_start_moves_the_version_to_profiling(
    session: AsyncSession, pipeline: PipelineFixture
) -> None:
    run = await start(session, pipeline)

    assert run.status is AgentRunStatus.QUEUED
    assert run.thread_id == str(run.id)
    assert pipeline.version.status is PipelineVersionStatus.PROFILING
    assert run.profile_ids == [str(profile.id) for profile in pipeline.profiles]


async def test_start_rejects_profiles_from_another_project(
    session: AsyncSession, pipeline: PipelineFixture
) -> None:
    """跨项目引用与不存在给同一个 404：不泄漏「它存在但你看不到」。"""
    other = Project(name=f"别的项目-{uuid.uuid4().hex[:6]}")
    session.add(other)
    await session.flush()
    connection = await _connection(
        session,
        project=ProjectContext(project_id=other.id, engineer_id=uuid.uuid4()),
        name=f"foreign-{uuid.uuid4().hex[:6]}",
        connector=ConnectorType.MYSQL,
        role=DataEndpointRole.SOURCE,
    )
    foreign = await _profile(
        session,
        connection=connection,
        role=DataEndpointRole.SOURCE,
        table_name="secret_orders",
        columns=SOURCE_COLUMNS,
    )

    with pytest.raises(ResourceNotFound):
        await start(session, pipeline, profile_ids=[*pipeline.profile_ids, foreign.id])


async def test_start_requires_both_endpoints(
    session: AsyncSession, pipeline: PipelineFixture
) -> None:
    """只有源端 Profile 时，门禁拿不到目标端事实，必须在受理阶段就说清楚。"""
    with pytest.raises(DomainError, match="缺少 SINK 端 Profile"):
        await start(session, pipeline, profile_ids=[pipeline.source.id])


async def test_start_rejects_a_stale_profile(
    session: AsyncSession, pipeline: PipelineFixture
) -> None:
    """Profile 是**有时效的事实**，过期不能降级使用（FR-13）。"""
    stale = await _profile(
        session,
        connection=await session.get(DataConnection, pipeline.sink.connection_id),
        role=DataEndpointRole.SINK,
        table_name="orders_wide_stale",
        columns=SINK_COLUMNS,
        expires_in=timedelta(days=-1),
    )

    with pytest.raises(DomainError, match="已过期"):
        await start(session, pipeline, profile_ids=[pipeline.source.id, stale.id])


# ------------------------------------------------------------------ 受管上下文
async def test_context_carries_structure_but_no_samples(
    session: AsyncSession, pipeline: PipelineFixture
) -> None:
    run = await start(session, pipeline)
    context = await GenerationRunService().build_context(session, run_id=run.id)

    assert "id bigint NOT NULL" in context.profile_context
    assert "amount decimal(18,2)" in context.profile_context
    # 脱敏样本留在库里供审批人对比，不随提示词出站。
    assert "***" not in context.profile_context
    assert (context.source.connector_type, context.target.connector_type) == (
        ConnectorType.MYSQL,
        ConnectorType.DORIS,
    )
    assert context.source.columns == frozenset({"id", "amount", "updated_at"})


# ------------------------------------------------------------------ Worker
async def test_worker_freezes_the_version_once_the_gates_pass(
    session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    pipeline: PipelineFixture,
) -> None:
    """门禁与真实引擎检查都通过 → 版本冻结成 VALIDATED，运行进入待审批。"""
    run = await start(session, pipeline)
    await session.commit()

    settlement = await run_worker(
        session_factory,
        run,
        adapter=ScriptedAdapter([candidate_json()]),
        engine=FakeEngine(),
        checkpointer=InMemorySaver(),
    )

    await session.refresh(run)
    await session.refresh(pipeline.version)
    assert settlement.status is AgentRunStatus.WAITING_APPROVAL
    assert run.status is AgentRunStatus.WAITING_APPROVAL
    assert pipeline.version.status is PipelineVersionStatus.VALIDATED
    assert pipeline.version.artifact_sha256 is not None
    assert run.generation_candidates == 1


async def test_worker_stops_for_clarification_and_resumes_on_the_same_thread(
    session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    pipeline: PipelineFixture,
) -> None:
    """模型反问 → 停在等澄清；补充信息后**从同一 thread_id 恢复**，而不是重跑（FR-27）。"""
    run = await start(session, pipeline)
    await session.commit()
    checkpointer = InMemorySaver()
    adapter = ScriptedAdapter([question_json(), candidate_json()])

    first = await run_worker(
        session_factory, run, adapter=adapter, engine=FakeEngine(), checkpointer=checkpointer
    )
    await session.refresh(run)
    assert first.status is AgentRunStatus.WAITING_INPUT
    assert run.pending_question == "增量水位列未指定，请指定"

    await GenerationRunService().answer(
        session, run_id=run.id, actor_id=pipeline.project.engineer_id, answer="用 updated_at"
    )
    await session.commit()
    assert run.status is AgentRunStatus.QUEUED
    assert run.clarification_rounds == 1

    second = await run_worker(
        session_factory,
        run,
        adapter=adapter,
        engine=FakeEngine(),
        checkpointer=checkpointer,
        answer="用 updated_at",
    )
    await session.refresh(pipeline.version)
    assert second.status is AgentRunStatus.WAITING_APPROVAL
    # 恢复而不是重跑：两次调用一共只用掉两份剧本（提问 + 候选）。
    assert adapter.responses == []
    assert pipeline.version.status is PipelineVersionStatus.VALIDATED
    # 补充的澄清内容确实进了第二次生成的上下文。
    assert "用 updated_at" in adapter.requests[-1].profile_context


async def test_unavailable_engine_blocks_the_freeze(
    session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    pipeline: PipelineFixture,
) -> None:
    """FR-35：引擎不可用时冻结被阻止并明确报告原因，**不允许跳过引擎检查直接冻结**。"""
    run = await start(session, pipeline)
    await session.commit()
    # 引擎一直不可用，修复循环会把候选预算跑满：首发 1 份 + 2 次修复。
    adapter = ScriptedAdapter([candidate_json(), candidate_json(), candidate_json()])

    settlement = await run_worker(
        session_factory,
        run,
        adapter=adapter,
        engine=FakeEngine(GateFinding(GateKind.ENGINE_CHECK, False, "引擎不可用：连接被拒绝")),
        checkpointer=InMemorySaver(),
    )

    await session.refresh(run)
    await session.refresh(pipeline.version)
    assert settlement.status is AgentRunStatus.FAILED
    assert pipeline.version.artifact_sha256 is None
    assert pipeline.version.status is not PipelineVersionStatus.VALIDATED
    assert "引擎不可用" in (run.error or "")
    # 引擎失败与确定性失败共享同一份候选预算（FR-31）：三次候选用完即安全终止。
    assert run.generation_candidates == 3
    # 修复提示要说明是**引擎**失败，而不是笼统的「门禁失败」——两者措辞不同（FR-32）。
    assert "[ENGINE_CHECK]" in (adapter.requests[-1].repair_hint or "")


async def test_worker_fails_when_the_model_is_unreachable(
    session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    pipeline: PipelineFixture,
) -> None:
    from data_integration_platform.domain.errors import ExternalServiceUnavailable

    class BrokenAdapter:
        backend_name = "broken"

        async def generate(self, request: Any) -> str:
            raise ExternalServiceUnavailable("生成能力未就绪：未配置 LLM 接入信息")

    run = await start(session, pipeline)
    await session.commit()

    settlement = await run_worker(
        session_factory,
        run,
        adapter=BrokenAdapter(),
        engine=FakeEngine(),
        checkpointer=InMemorySaver(),
    )

    await session.refresh(run)
    assert settlement.status is AgentRunStatus.FAILED
    assert settlement.retryable is True
    assert run.error == "生成能力未就绪：未配置 LLM 接入信息"
    assert pipeline.version.status is PipelineVersionStatus.PROFILING


async def test_worker_skips_a_run_that_is_already_finished(
    session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    pipeline: PipelineFixture,
) -> None:
    """Broker 重投一条已结束的运行：跳过，而不是把模型再跑一遍。"""
    run = await start(session, pipeline)
    run.status = AgentRunStatus.WAITING_APPROVAL
    await session.commit()
    adapter = ScriptedAdapter([candidate_json()])

    settlement = await run_worker(
        session_factory,
        run,
        adapter=adapter,
        engine=FakeEngine(),
        checkpointer=InMemorySaver(),
    )

    assert "已跳过" in (settlement.failure_reason or "")
    assert adapter.requests == []


async def test_generation_requires_a_writable_role(
    session: AsyncSession, pipeline: PipelineFixture, project: ProjectContext
) -> None:
    from data_integration_platform.domain.errors import PermissionDenied
    from data_integration_platform.infrastructure.models import ProjectMembership, User

    viewer = User(subject=f"viewer-{uuid.uuid4().hex[:6]}", display_name="王五")
    session.add(viewer)
    await session.flush()
    session.add(
        ProjectMembership(project_id=project.project_id, user_id=viewer.id, role=ProjectRole.VIEWER)
    )
    await session.flush()

    with pytest.raises(PermissionDenied):
        await GenerationRunService().start(
            session,
            version_id=pipeline.version.id,
            actor_id=viewer.id,
            profile_ids=pipeline.profile_ids,
        )
