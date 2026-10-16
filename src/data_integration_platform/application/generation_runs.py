"""生成运行的受理、受管上下文与结论落地（生成链路的 API / Worker 接线）。

三件事分开：

1. **受理**（`start` / `answer`，API 侧调用）：只校验前置条件、推进状态、落一条
   `AgentRun` 事实，然后由调用方投递任务。API **不跑模型**（FR-15 的同一条口径）。
2. **上下文装配**（`build_context`，Worker 侧调用）：把 Profile 事实、连接器类型与
   工程师声明的契约拼成受管输入。模型只看得到字段、类型、主键这类**结构摘要**，
   看不到任何原始业务行 —— 脱敏样本留在库里，不进提示词。
3. **结论落地**（`settle`，Worker 侧调用）：状态机跑完后回写阶段、候选与修复计数；
   `WAIT_FOR_REVIEW` 意味着**确定性门禁与真实引擎检查都已通过**，此时才冻结版本
   （FR-35）。

**为什么冻结放在 Worker 而不是单独开一个端点**：FR-35 要求「通过静态门禁后经真实
SeaTunnel 检查，通过后才允许冻结」。冻结与门禁的先后顺序如果是两个独立入口，
就得靠调用方记得先跑门禁；放在状态机出边之后，这条顺序是**结构上成立**的。
"""

import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from data_integration_platform.application.access import require_project_roles
from data_integration_platform.application.audit import record_audit_event
from data_integration_platform.application.generation import GeneratedCandidate
from data_integration_platform.application.policy import (
    SourceFacts,
    TargetFacts,
    facts_from_profile,
    facts_from_target,
)
from data_integration_platform.application.profiling import require_profile_usable
from data_integration_platform.application.versions import PipelineVersionService
from data_integration_platform.domain.enums import (
    AgentRunStatus,
    AuditEventType,
    ConnectorType,
    DataEndpointRole,
    PipelineVersionStatus,
    ProjectRole,
)
from data_integration_platform.domain.errors import DomainError, ResourceNotFound
from data_integration_platform.domain.harness import canonical_json
from data_integration_platform.domain.state_machine import (
    require_agent_run_transition,
    require_pipeline_transition,
)
from data_integration_platform.infrastructure.models import (
    AgentRun,
    DataConnection,
    DataProfile,
    Pipeline,
    PipelineVersion,
)

WRITABLE = frozenset({ProjectRole.ADMIN, ProjectRole.DATA_ENGINEER})
MAX_PROFILES = 20


@dataclass(frozen=True, slots=True)
class GenerationContext:
    """一次生成所需的全部受管输入；**全部来自数据库事实**，不由调用方拼装。"""

    run_id: uuid.UUID
    version_id: uuid.UUID
    project_id: uuid.UUID
    thread_id: str
    requirement: str
    source_summary: str
    target_summary: str
    profile_context: str
    source: SourceFacts
    target: TargetFacts


@dataclass(frozen=True, slots=True)
class RunSettlement:
    """状态机跑完之后的结论；由 Worker 回写到 `AgentRun` 与版本。"""

    phase: str
    status: AgentRunStatus
    candidates: int
    repairs: int
    pending_question: str | None = None
    candidate: GeneratedCandidate | None = None
    gate_findings: tuple[Mapping[str, Any], ...] = ()
    diagnostics: tuple[Mapping[str, Any], ...] = ()
    failure_reason: str | None = None
    # 只有外部依赖不可用才值得让 Celery 把任务标记成失败待重投；
    # 业务规则违反重试多少次都是同一个结果。
    retryable: bool = False


class GenerationRunService:
    # -------------------------------------------------------------- 受理
    async def start(
        self,
        session: AsyncSession,
        *,
        version_id: uuid.UUID,
        actor_id: uuid.UUID,
        profile_ids: Sequence[uuid.UUID],
        quality_contract: Mapping[str, Any] | None = None,
        incremental_contract: Mapping[str, Any] | None = None,
        row_llm_contract: Mapping[str, Any] | None = None,
        trace_id: str | None = None,
    ) -> AgentRun:
        """受理一次生成：校验 Profile 可用、推进版本状态、落 `AgentRun`。"""
        version = await self._require_version(session, version_id)
        project_id = await self._require_writable(session, version, actor_id)
        if not profile_ids:
            raise DomainError("发起生成至少要给出一个 Profile")
        if len(profile_ids) > MAX_PROFILES:
            raise DomainError(f"一次生成最多引用 {MAX_PROFILES} 个 Profile")

        profiles = await self._load_profiles(session, project_id=project_id, ids=profile_ids)
        # 门禁要拿源端与目标端的事实，两个角色缺一不可；缺了就在这里说清楚，
        # 而不是让状态机跑到门禁才发现。
        source = _first_of_role(profiles, DataEndpointRole.SOURCE)
        target = _first_of_role(profiles, DataEndpointRole.SINK)

        # 版本从 DRAFT 进入 PROFILING；跳步（例如已审批的版本重新生成）由状态机拒绝。
        require_pipeline_transition(version.status, PipelineVersionStatus.PROFILING)
        run_id = uuid.uuid4()
        run = AgentRun(
            id=run_id,
            version_id=version.id,
            status=AgentRunStatus.QUEUED,
            # 澄清恢复靠同一个 thread_id：一次生成一个线程，重跑才换线程。
            thread_id=str(run_id),
            profile_ids=[str(profile.id) for profile in profiles],
            # 源端/目标端在**受理时**定档：Worker 不再重新决定拿哪个 Profile 当哪一端，
            # 否则同一份输入在不同时刻可能装出不同的上下文。下划线前缀表示平台自用，
            # 不进提示词（见 `_without_pins`）。
            quality_contract={
                **(_without_pins(quality_contract) if quality_contract else {}),
                "_source_profile_id": str(source.id),
                "_target_profile_id": str(target.id),
            },
            incremental_contract=dict(incremental_contract) if incremental_contract else None,
            row_llm_contract=dict(row_llm_contract) if row_llm_contract else None,
            created_by=actor_id,
        )
        session.add(run)
        version.status = PipelineVersionStatus.PROFILING
        await session.flush()

        record_audit_event(
            session,
            event_type=AuditEventType.AGENT_RUN_REQUESTED,
            target_type="agent_run",
            project_id=project_id,
            actor_id=actor_id,
            target_id=str(run.id),
            change_summary={
                "version_id": str(version.id),
                "profile_ids": run.profile_ids,
                "thread_id": run.thread_id,
            },
            trace_id=trace_id,
        )
        return run

    async def answer(
        self,
        session: AsyncSession,
        *,
        run_id: uuid.UUID,
        actor_id: uuid.UUID,
        answer: str,
    ) -> AgentRun:
        """回答澄清问题：状态回到 `QUEUED`，由调用方投递带答案的恢复任务。

        答案随任务载荷走到 Worker（`Command(resume=...)`），**不落业务库** ——
        澄清内容属于生成过程的中间状态，它的归属地是 Checkpoint 库，不是业务真相库。
        """
        run = await self._require_run(session, run_id)
        version = await self._require_version(session, run.version_id)
        await self._require_writable(session, version, actor_id)
        require_agent_run_transition(run.status, AgentRunStatus.QUEUED)

        run.status = AgentRunStatus.QUEUED
        run.clarification_rounds += 1
        await session.flush()
        return run

    async def get(
        self, session: AsyncSession, *, run_id: uuid.UUID, actor_id: uuid.UUID
    ) -> AgentRun:
        run = await self._require_run(session, run_id)
        version = await self._require_version(session, run.version_id)
        await self._require_readable(session, version, actor_id)
        return run

    # -------------------------------------------------------------- Worker 侧
    async def build_context(self, session: AsyncSession, *, run_id: uuid.UUID) -> GenerationContext:
        """装配受管上下文；`profile_ids` 已在受理时定档，这里只做投影。"""
        run = await self._require_run(session, run_id)
        version = await self._require_version(session, run.version_id)
        pipeline = await self._require_pipeline(session, version.pipeline_id)
        profiles = await self._load_profiles(
            session,
            project_id=pipeline.project_id,
            ids=[uuid.UUID(profile_id) for profile_id in run.profile_ids],
        )
        source = _first_of_role(profiles, DataEndpointRole.SOURCE)
        target = _first_of_role(profiles, DataEndpointRole.SINK)

        return GenerationContext(
            run_id=run.id,
            version_id=version.id,
            project_id=pipeline.project_id,
            thread_id=run.thread_id,
            requirement=version.requirement,
            source_summary=_table_summary(source),
            target_summary=_table_summary(target),
            profile_context=_profile_context(
                source=source,
                target=target,
                quality_contract=run.quality_contract,
                incremental_contract=run.incremental_contract,
                row_llm_contract=run.row_llm_contract,
            ),
            source=facts_from_profile(
                connector_type=await self._connector_of(session, source),
                columns=source.columns,
            ),
            target=facts_from_target(
                connector_type=await self._connector_of(session, target),
                columns=target.columns,
            ),
        )

    async def settle(
        self,
        session: AsyncSession,
        *,
        run_id: uuid.UUID,
        settlement: RunSettlement,
        trace_id: str | None = None,
    ) -> AgentRun:
        """把状态机的结论落成事实；`WAIT_FOR_REVIEW` 时顺带冻结版本。"""
        run = await self._require_run(session, run_id)
        version = await self._require_version(session, run.version_id)
        pipeline = await self._require_pipeline(session, version.pipeline_id)

        run.generation_phase = settlement.phase
        run.generation_candidates = settlement.candidates
        run.generation_repairs = settlement.repairs
        run.pending_question = settlement.pending_question
        run.generation_diagnostics = {
            "findings": [dict(finding) for finding in settlement.gate_findings],
            "diagnostics": [dict(item) for item in settlement.diagnostics],
        }
        run.finished_at = datetime.now(UTC)
        run.error = settlement.failure_reason

        if settlement.candidate is not None:
            _record_contracts(run, settlement.candidate)

        if settlement.status is AgentRunStatus.WAITING_APPROVAL:
            await self._freeze(
                session,
                run=run,
                version=version,
                project_id=pipeline.project_id,
                candidate=settlement.candidate,
            )

        run.status = settlement.status
        await session.flush()
        record_audit_event(
            session,
            event_type=AuditEventType.AGENT_RUN_SETTLED,
            target_type="agent_run",
            project_id=pipeline.project_id,
            actor_id=run.created_by,
            target_id=str(run.id),
            change_summary={
                "version_id": str(version.id),
                "status": settlement.status.value,
                "phase": settlement.phase,
                "candidates": settlement.candidates,
                "repairs": settlement.repairs,
                "failure_reason": settlement.failure_reason,
            },
            trace_id=trace_id,
        )
        return run

    async def mark_running(self, session: AsyncSession, *, run_id: uuid.UUID) -> AgentRun:
        """任务开始执行；重复投递（Broker 重试）时保持 RUNNING 而不是报错。"""
        run = await self._require_run(session, run_id)
        if run.status is not AgentRunStatus.RUNNING:
            require_agent_run_transition(run.status, AgentRunStatus.RUNNING)
            run.status = AgentRunStatus.RUNNING
            run.started_at = datetime.now(UTC)
        await session.flush()
        return run

    # -------------------------------------------------------------- 内部
    async def _freeze(
        self,
        session: AsyncSession,
        *,
        run: AgentRun,
        version: PipelineVersion,
        project_id: uuid.UUID,
        candidate: GeneratedCandidate | None,
    ) -> None:
        if candidate is None:
            raise DomainError("生成到达待审批阶段却没有候选，无法冻结")
        if version.status is PipelineVersionStatus.PROFILING:
            require_pipeline_transition(version.status, PipelineVersionStatus.GENERATED)
            version.status = PipelineVersionStatus.GENERATED
        await PipelineVersionService().freeze(
            session, version_id=version.id, actor_id=run.created_by, candidate=candidate
        )

    async def _connector_of(self, session: AsyncSession, profile: DataProfile) -> ConnectorType:
        connection = await session.get(DataConnection, profile.connection_id)
        if connection is None:
            raise ResourceNotFound("数据连接不存在")
        return connection.connector_type

    async def _load_profiles(
        self, session: AsyncSession, *, project_id: uuid.UUID, ids: Sequence[uuid.UUID]
    ) -> list[DataProfile]:
        found = list(
            (
                await session.scalars(
                    select(DataProfile)
                    .join(DataConnection, DataConnection.id == DataProfile.connection_id)
                    .where(DataProfile.id.in_(ids), DataConnection.project_id == project_id)
                    .order_by(DataProfile.created_at)
                )
            ).all()
        )
        missing = {str(found_id) for found_id in ids} - {str(profile.id) for profile in found}
        if missing:
            # 跨项目的 Profile 与不存在的 Profile 给同一个响应：不泄漏「它存在但你看不到」。
            raise ResourceNotFound("Profile 不存在或不属于该项目")
        for profile in found:
            require_profile_usable(profile)
        return found

    async def _require_writable(
        self, session: AsyncSession, version: PipelineVersion, actor_id: uuid.UUID
    ) -> uuid.UUID:
        pipeline = await self._require_pipeline(session, version.pipeline_id)
        await require_project_roles(
            session,
            project_id=pipeline.project_id,
            actor_id=actor_id,
            allowed=WRITABLE,
            write=True,
        )
        return pipeline.project_id

    async def _require_readable(
        self, session: AsyncSession, version: PipelineVersion, actor_id: uuid.UUID
    ) -> uuid.UUID:
        pipeline = await self._require_pipeline(session, version.pipeline_id)
        readable = frozenset(
            {
                ProjectRole.ADMIN,
                ProjectRole.DATA_ENGINEER,
                ProjectRole.APPROVER,
                ProjectRole.VIEWER,
            }
        )
        await require_project_roles(
            session, project_id=pipeline.project_id, actor_id=actor_id, allowed=readable
        )
        return pipeline.project_id

    async def _require_pipeline(self, session: AsyncSession, pipeline_id: uuid.UUID) -> Pipeline:
        pipeline = await session.get(Pipeline, pipeline_id)
        if pipeline is None:
            raise ResourceNotFound("管道不存在")
        return pipeline

    async def _require_version(
        self, session: AsyncSession, version_id: uuid.UUID
    ) -> PipelineVersion:
        version = await session.get(PipelineVersion, version_id)
        if version is None:
            raise ResourceNotFound("管道版本不存在")
        return version

    async def _require_run(self, session: AsyncSession, run_id: uuid.UUID) -> AgentRun:
        run = await session.get(AgentRun, run_id)
        if run is None:
            raise ResourceNotFound("生成记录不存在")
        return run


# ------------------------------------------------------------------ 上下文装配
def _first_of_role(profiles: Sequence[DataProfile], role: DataEndpointRole) -> DataProfile:
    for profile in profiles:
        if profile.endpoint_role is role:
            return profile
    raise DomainError(f"缺少 {role.value} 端 Profile：门禁需要源端与目标端各自的结构事实")


def _table_summary(profile: DataProfile) -> str:
    return f"{profile.schema_name}.{profile.table_name}（{len(profile.columns)} 个字段）"


def _column_line(column: Mapping[str, Any]) -> str:
    name = str(column.get("name", "?"))
    declared = str(column.get("type") or column.get("data_type") or "unknown")
    nullable = column.get("nullable")
    suffix = "" if nullable in (None, True) else " NOT NULL"
    return f"{name} {declared}{suffix}"


def _profile_context(
    *,
    source: DataProfile,
    target: DataProfile,
    quality_contract: Mapping[str, Any] | None,
    incremental_contract: Mapping[str, Any] | None,
    row_llm_contract: Mapping[str, Any] | None,
) -> str:
    """受管上下文文本。

    **只给结构，不给样本**：脱敏样本留在库里供审批人对比，不随提示词出站。
    """
    lines = [
        "# 源端结构",
        f"{source.schema_name}.{source.table_name}",
        *(_column_line(column) for column in source.columns),
        "# 源端主键",
        ", ".join(source.primary_keys) or "（无）",
        "",
        "# 目标端结构",
        f"{target.schema_name}.{target.table_name}",
        *(_column_line(column) for column in target.columns),
        "# 目标端主键",
        ", ".join(target.primary_keys) or "（无）",
    ]
    declared = {
        "工程师声明的质量契约": quality_contract,
        "工程师声明的增量契约": incremental_contract,
        "工程师声明的逐行 LLM 契约": row_llm_contract,
    }
    for title, payload in declared.items():
        if payload:
            lines.extend(["", f"# {title}", canonical_json(_without_pins(payload))])
    return "\n".join(lines)


def _without_pins(payload: Mapping[str, Any]) -> dict[str, Any]:
    """去掉内部定档字段（下划线开头），它们是平台自己记的，不该出现在提示词里。"""
    return {key: value for key, value in payload.items() if not key.startswith("_")}


def _record_contracts(run: AgentRun, candidate: GeneratedCandidate) -> None:
    """把候选里的契约回写到 AgentRun，便于审批人按契约维度筛查。"""
    plan = candidate.plan
    run.quality_contract = {
        **(run.quality_contract or {}),
        "rules": [rule.model_dump(mode="json") for rule in plan.quality_rules],
    }
    run.incremental_contract = {
        **(run.incremental_contract or {}),
        **plan.incremental.model_dump(mode="json"),
    }
    run.row_llm_contract = {
        **(run.row_llm_contract or {}),
        **plan.row_llm.model_dump(mode="json"),
    }
