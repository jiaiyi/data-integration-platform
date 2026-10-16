"""执行任务的执行侧（T-37 / FR-44、FR-45、FR-47）。

**验签必须在读 Secret 与接触引擎之前**（SPEC §7.4 的关键不变量）。所以这里的顺序是死的：

```
① 验签（事务内，不碰任何外部系统）
② 读 Secret 并把占位符物化成真实配置
③ 提交给引擎
④ 回写事实
```

②③ 只有在 ① 通过之后才可能被调用到。这条不是靠注释保证的：测试用会记账的假引擎与假
Secret Provider，断言令牌不成立时**两者的调用次数都是 0**。

物化出来的配置里是明文凭据，因此它**只在本次运行的作用域里存在**：不落库、不进日志、
不进证据账本。证据里只记「用了哪些占位符名」。

失败分三类，处置刻意不同：

- **验签/物化失败**：重试多少次都是同一个结果（令牌不成立、占位符没登记），记为终态失败；
- **引擎不可用**：交给 Outbox 的租约重投，次数由 `attempts` 兜住；
- **引擎明确拒绝**（配置不合法等）：重投也没用，记为终态失败。

执行记录与待投递命令在 Commit 时已同事务建好，这里只推进它们的状态。
"""

import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from data_integration_platform.application.audit import record_audit_event
from data_integration_platform.application.harness import outbox
from data_integration_platform.application.harness.capability import (
    CapabilityExpectation,
    CapabilityMismatch,
    token_from_grant,
    verify_capability,
)
from data_integration_platform.application.harness.engine import ExecutionEnginePort
from data_integration_platform.application.harness.placeholders import (
    PlaceholderTable,
    build_placeholder_table,
)
from data_integration_platform.application.harness.signing import SignatureError, VerifierPort
from data_integration_platform.application.masking import scrub_secrets
from data_integration_platform.application.secrets import SecretProviderPort
from data_integration_platform.application.versions import PipelineVersionService
from data_integration_platform.asyncio_compat import run_async
from data_integration_platform.celery_app import celery_app
from data_integration_platform.domain.enums import (
    AuditEventType,
    DataEndpointRole,
    ExecutionStatus,
    FailureCategory,
    FailureStage,
)
from data_integration_platform.domain.errors import (
    DomainError,
    ExternalServiceUnavailable,
    ResourceNotFound,
)
from data_integration_platform.domain.harness import HarnessOutboxStatus
from data_integration_platform.infrastructure.database import SessionFactory
from data_integration_platform.infrastructure.ed25519_signing import create_capability_verifier
from data_integration_platform.infrastructure.models import (
    DataConnection,
    ExecutionPreparation,
    ExecutionRun,
    HarnessCapabilityGrant,
    HarnessCommandOutbox,
    PipelineVersion,
)
from data_integration_platform.infrastructure.seatunnel_execution import create_execution_engine
from data_integration_platform.infrastructure.secret_factory import create_secret_provider

FROZEN_HOCON_ARTIFACT = "HOCON"


@dataclass(frozen=True, slots=True)
class ExecutionOutcome:
    run_id: uuid.UUID
    status: ExecutionStatus
    engine_job_id: str | None = None
    reason: str | None = None
    retryable: bool = False


@celery_app.task(name="data_integration_platform.execution_tasks.submit_execution")
def submit_execution(outbox_id: str) -> dict[str, object]:
    """处理一条待投递命令；未验签时**不会**读 Secret，也不会碰引擎。"""
    outcome = run_async(_submit(uuid.UUID(outbox_id)))
    if outcome.retryable:
        raise ExternalServiceUnavailable(outcome.reason or "引擎暂时不可用")
    return {
        "run_id": str(outcome.run_id),
        "status": outcome.status.value,
        "engine_job_id": outcome.engine_job_id,
    }


async def _submit(
    outbox_id: uuid.UUID,
    *,
    session_factory: async_sessionmaker[AsyncSession] | None = None,
    engine: ExecutionEnginePort | None = None,
    verifier: VerifierPort | None = None,
    secrets_factory: Callable[[AsyncSession], SecretProviderPort] | None = None,
    now: datetime | None = None,
) -> ExecutionOutcome:
    """四段式执行；除 `outbox_id` 外的参数都是测试缝。

    **不能在协程里用 `run_in_transaction`**：它内部走 `run_async`，而本协程已经在
    那条事件循环上跑着。会话边界直接在这里开。
    """
    factory = session_factory if session_factory is not None else SessionFactory
    moment = now if now is not None else datetime.now(UTC)
    resolved_engine = engine if engine is not None else create_execution_engine()

    try:
        async with factory() as session:
            async with session.begin():
                command = await session.get(HarnessCommandOutbox, outbox_id)
                if command is None:
                    raise ResourceNotFound("待投递命令不存在")
                context = await _verified_context(
                    session, command=command, verifier=verifier, engine=resolved_engine, now=moment
                )

        # —— 到这里为止没读过任何 Secret，也没碰过引擎 ——
        async with factory() as session:
            async with session.begin():
                materialized = await _materialize(
                    session, context=context, secrets_factory=secrets_factory
                )
    except DomainError as exc:
        return await _record_terminal_failure(
            factory,
            outbox_id=outbox_id,
            reason=scrub_secrets(str(exc)),
            category=FailureCategory.CREDENTIALS,
            now=moment,
        )

    try:
        job_id = await context.engine.submit(
            config=materialized.config, job_name=materialized.job_name
        )
    except ExternalServiceUnavailable as exc:
        return await _record_unavailable(
            factory, outbox_id=outbox_id, run_id=context.run_id, reason=str(exc), now=moment
        )
    except DomainError as exc:
        return await _record_terminal_failure(
            factory,
            outbox_id=outbox_id,
            run_id=context.run_id,
            reason=scrub_secrets(str(exc)),
            category=FailureCategory.CONFIGURATION,
            now=moment,
        )

    async with factory() as session:
        async with session.begin():
            run = await session.get(ExecutionRun, context.run_id)
            assert run is not None
            run.status = ExecutionStatus.SUBMITTED
            run.seatunnel_job_id = job_id
            run.engine_status = "SUBMITTED"
            run.submitted_at = moment
            run.last_observed_at = moment
            await outbox.mark_dispatched(session, outbox_id=outbox_id, now=moment)
            record_audit_event(
                session,
                event_type=AuditEventType.EXECUTION_SUBMITTED,
                target_type="execution_run",
                target_id=str(run.id),
                change_summary={
                    "engine_job_id": job_id,
                    "artifact_sha256": context.artifact_sha256,
                    # 只记**占位符名字**；物化后的配置留在这段作用域里，不落库。
                    "materialized_placeholders": list(materialized.placeholder_names),
                },
            )
    return ExecutionOutcome(
        run_id=context.run_id, status=ExecutionStatus.SUBMITTED, engine_job_id=job_id
    )


@dataclass(frozen=True, slots=True)
class _VerifiedContext:
    run_id: uuid.UUID
    preparation_id: uuid.UUID
    version_id: uuid.UUID
    engine: ExecutionEnginePort
    artifact_sha256: str
    job_name: str
    connections: Mapping[str, str]


@dataclass(frozen=True, slots=True)
class _Materialized:
    config: str
    job_name: str
    placeholder_names: tuple[str, ...]


async def _verified_context(
    session: AsyncSession,
    *,
    command: HarnessCommandOutbox,
    verifier: VerifierPort | None,
    engine: ExecutionEnginePort,
    now: datetime,
) -> _VerifiedContext:
    """① 验签：确认这条命令确实由持钥方授权过。

    任何不成立都在这里以 `DomainError` 结束，并且**早于**任何 Secret 读取与引擎接触。
    """
    outbox.require_payload_intact(command)
    payload = command.command_payload
    run = await session.get(ExecutionRun, uuid.UUID(str(payload.get("run_id"))))
    if run is None:
        raise ResourceNotFound("执行记录不存在")
    preparation = await session.get(ExecutionPreparation, run.harness_preparation_id)
    if preparation is None:
        raise ResourceNotFound("准备单不存在")
    if await session.get(PipelineVersion, run.pipeline_version_id) is None:
        raise ResourceNotFound("管道版本不存在")

    grant = await session.scalar(
        select(HarnessCapabilityGrant).where(HarnessCapabilityGrant.execution_run_id == run.id)
    )
    if grant is None:
        raise DomainError("这条命令没有对应的能力令牌，拒绝执行")
    token = token_from_grant(
        claims_payload=grant.claims,
        algorithm=grant.claims_signature_algorithm,
        key_id=grant.key_id,
        signature=grant.claims_signature_value,
    )
    try:
        verify_capability(
            token,
            expectation=_expectation(preparation=preparation),
            verifier=verifier if verifier is not None else create_capability_verifier(),
            now=now,
        )
    except (SignatureError, CapabilityMismatch, DomainError) as exc:
        # `CapabilityMismatch` 刻意不继承 `DomainError`（它是安全边界上的拒绝），
        # 所以这里必须显式接住，否则它会直接穿透 Worker 变成一个没有结论的异常。
        raise DomainError(f"能力令牌校验未通过，拒绝执行：{exc}") from exc

    connections = dict(preparation.impact_report.get("connections") or {})
    if "source" not in connections or "target" not in connections:
        raise DomainError("准备单没有冻结源/目标端连接，无法在验签后取凭据")
    return _VerifiedContext(
        run_id=run.id,
        preparation_id=preparation.id,
        version_id=run.pipeline_version_id,
        engine=engine,
        artifact_sha256=preparation.artifact_sha256,
        job_name=str(payload.get("job_name") or "dip-run"),
        connections=connections,
    )


async def _materialize(
    session: AsyncSession,
    *,
    context: _VerifiedContext,
    secrets_factory: Callable[[AsyncSession], SecretProviderPort] | None,
) -> _Materialized:
    """② 读 Secret 并替换占位符。**只有验签通过之后才会走到这里。**"""
    provider = (
        secrets_factory(session) if secrets_factory is not None else create_secret_provider(session)
    )
    tables: list[PlaceholderTable] = []
    for role, key in (
        (DataEndpointRole.SOURCE, "source"),
        (DataEndpointRole.SINK, "target"),
    ):
        connection = await session.get(DataConnection, uuid.UUID(context.connections[key]))
        if connection is None:
            raise ResourceNotFound(f"{role.value} 端数据连接不存在")
        credentials = await provider.load(connection.secret_ref) if connection.secret_ref else None
        tables.append(
            build_placeholder_table(role=role, connection=connection, credentials=credentials)
        )

    values = {name: value for table in tables for name, value in table.values.items()}
    frozen = await _frozen_hocon(session, version_id=context.version_id)
    table = PlaceholderTable(values=values)
    return _Materialized(
        config=table.substitute(frozen),
        job_name=context.job_name,
        placeholder_names=tuple(sorted(table.names())),
    )


async def _frozen_hocon(session: AsyncSession, *, version_id: uuid.UUID) -> str:
    """取冻结在盘上的 HOCON 制品 —— 执行用的必须是审批时看到的那份字节。"""
    service = PipelineVersionService()
    artifacts = await service.artifacts_of(session, version_id=version_id)
    artifact = artifacts.get(FROZEN_HOCON_ARTIFACT)
    if artifact is None:
        raise DomainError("版本没有冻结的 HOCON 制品，拒绝执行")
    return (await service.read_artifact(artifact)).decode("utf-8")


def _expectation(*, preparation: ExecutionPreparation) -> CapabilityExpectation:
    """期望值全部取自**服务端事实**（准备单），不来自命令载荷。

    主体取 `preparation.committed_by`：那是 Commit 时写下的事实，与令牌里的主体同源。
    执行记录没有主体列，所以不靠它。
    """
    return CapabilityExpectation(
        tool_name=preparation.tool_name,
        tool_version=preparation.tool_version,
        subject_id=str(preparation.committed_by or ""),
        environment=preparation.environment,
        preparation_id=str(preparation.id),
        artifact_sha256=preparation.artifact_sha256,
        input_fingerprint=preparation.input_fingerprint,
        resource_fingerprint=preparation.resource_fingerprint,
        supervision_contract_sha256=preparation.supervision_contract_sha256,
        data_classification_sha256=preparation.data_classification_sha256,
    )


async def _record_unavailable(
    factory: async_sessionmaker[AsyncSession],
    *,
    outbox_id: uuid.UUID,
    run_id: uuid.UUID,
    reason: str,
    now: datetime,
) -> ExecutionOutcome:
    """引擎不可用：交给 Outbox 重投；重投次数用尽才把运行判失败。"""
    async with factory() as session:
        async with session.begin():
            row = await outbox.mark_failed(
                session, outbox_id=outbox_id, error=scrub_secrets(reason)
            )
            exhausted = row.status is HarnessOutboxStatus.FAILED
            if exhausted:
                await _fail_run(
                    session,
                    run_id=run_id,
                    reason=f"引擎持续不可用，已用尽重投次数：{scrub_secrets(reason)}",
                    category=FailureCategory.CONNECTIVITY,
                )
    return ExecutionOutcome(
        run_id=run_id,
        status=ExecutionStatus.FAILED if exhausted else ExecutionStatus.QUEUED,
        reason=scrub_secrets(reason),
        retryable=not exhausted,
    )


async def _record_terminal_failure(
    factory: async_sessionmaker[AsyncSession],
    *,
    outbox_id: uuid.UUID,
    reason: str,
    category: FailureCategory,
    now: datetime,
    run_id: uuid.UUID | None = None,
) -> ExecutionOutcome:
    """重投也没用的失败：命令（与运行）一起记为终态，不再占用扫描。"""
    resolved_run_id = run_id
    async with factory() as session:
        async with session.begin():
            row = await outbox.mark_failed(session, outbox_id=outbox_id, error=reason)
            if resolved_run_id is None:
                payload_run = row.command_payload.get("run_id")
                resolved_run_id = uuid.UUID(str(payload_run)) if payload_run else None
            if resolved_run_id is not None:
                await _fail_run(session, run_id=resolved_run_id, reason=reason, category=category)
    assert resolved_run_id is not None
    return ExecutionOutcome(run_id=resolved_run_id, status=ExecutionStatus.FAILED, reason=reason)


async def _fail_run(
    session: AsyncSession,
    *,
    run_id: uuid.UUID,
    reason: str,
    category: FailureCategory,
) -> None:
    run = await session.get(ExecutionRun, run_id)
    if run is None:
        return
    run.status = ExecutionStatus.FAILED
    run.failure_stage = FailureStage.SUBMISSION
    run.failure_category = category
    run.retryable = False
    run.diagnosis = {"reason": reason}
