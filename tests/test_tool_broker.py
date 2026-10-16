"""Tool Broker 的「无绕过路径」（T-27 / FR-44）。

证据不是「返回了错误」，而是**执行器的调用次数是 0**：
未注册工具、输入越界、资源指纹对不上、策略拒绝、令牌过期、跨环境、
重放 —— 每种情况都断言执行器一次都没被调到。
最后再证一次正向路径：全部通过时执行器恰好被调一次。
"""

import uuid
from collections.abc import Mapping
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from data_integration_platform.application.harness.broker import (
    ToolBroker,
    ToolCall,
)
from data_integration_platform.application.harness.capability import (
    CapabilityExpectation,
    SignedCapability,
    issue_capability,
)
from data_integration_platform.application.harness.grants import record
from data_integration_platform.application.harness.pdp import (
    PolicyBundle,
    build_policy_bundle,
)
from data_integration_platform.application.harness.tools import (
    ToolRegistry,
    ToolSpec,
    build_tool_registry,
)
from data_integration_platform.domain.errors import PermissionDenied
from data_integration_platform.domain.harness import (
    DataSensitivity,
    ResourceScope,
    RiskLevel,
    ToolEffect,
    ToolIntent,
)
from data_integration_platform.infrastructure.ed25519_signing import (
    Ed25519KeyRingVerifier,
    Ed25519Signer,
    generate_keypair,
    parse_public_keys,
)
from data_integration_platform.infrastructure.models import (
    ExecutionPreparation,
    PipelineVersion,
    Project,
)
from tests.conftest import ProjectContext

NOW = datetime(2026, 10, 16, 2, 0, tzinfo=UTC)
TTL = 300
PROJECT = "11111111-1111-1111-1111-111111111111"
SUBJECT = "22222222-2222-2222-2222-222222222222"
PREPARATION_ID = "66666666-6666-6666-6666-666666666666"
ENGINE_VERSION = "2.3.13"
KEY_ID = "harness-local-1"

SCOPE = ResourceScope(project_id=PROJECT, objects=("dw.orders_wide",))
JOB_PAYLOAD: dict[str, Any] = {
    "preparation_id": PREPARATION_ID,
    "artifact_sha256": "a" * 64,
    "job_name": "orders-sync",
    "config_format": "HOCON",
}


class RecordingExecutor:
    """记录被调用的执行器；「没有绕过路径」全靠它的计数来证明。"""

    def __init__(self) -> None:
        self.calls: list[tuple[str, Mapping[str, Any]]] = []

    async def __call__(self, *, spec: ToolSpec, payload: Mapping[str, Any]) -> Mapping[str, Any]:
        self.calls.append((spec.manifest.name, payload))
        return {"accepted": True, "reference": "job-1"}


@pytest.fixture(scope="module")
def keys() -> tuple[Ed25519Signer, Ed25519KeyRingVerifier]:
    private, public = generate_keypair()
    return (
        Ed25519Signer(key_id=KEY_ID, private_key=private),
        Ed25519KeyRingVerifier(parse_public_keys(f"{KEY_ID}:{public}")),
    )


@pytest.fixture
def registry() -> ToolRegistry:
    return build_tool_registry(seatunnel_version=ENGINE_VERSION)


@pytest.fixture
def bundle() -> PolicyBundle:
    return build_policy_bundle(version="harness-v1-20261016")


@pytest.fixture
def executor() -> RecordingExecutor:
    return RecordingExecutor()


@pytest.fixture
def broker(
    registry: ToolRegistry,
    bundle: PolicyBundle,
    keys: tuple[Ed25519Signer, Ed25519KeyRingVerifier],
    executor: RecordingExecutor,
) -> ToolBroker:
    return ToolBroker(registry=registry, bundle=bundle, verifier=keys[1], executor=executor)


@pytest.fixture
async def preparation(session: AsyncSession, project: ProjectContext) -> ExecutionPreparation:
    pipeline = Project(name=f"管道-{uuid.uuid4().hex[:6]}")
    session.add(pipeline)
    await session.flush()
    version = PipelineVersion(
        pipeline_id=pipeline.id,
        version_number=1,
        requirement="把订单同步到宽表",
        created_by=project.engineer_id,
    )
    session.add(version)
    await session.flush()
    row = ExecutionPreparation(
        pipeline_version_id=version.id,
        risk_level=RiskLevel.P0,
        policy_bundle_version="harness-v1-20261016",
        environment="local",
        tool_name="seatunnel.submit_job",
        tool_version=ENGINE_VERSION,
        artifact_sha256="a" * 64,
        input_fingerprint="b" * 64,
        resource_fingerprint=SCOPE.fingerprint(),
        supervision_contract_sha256="d" * 64,
        data_classification_sha256="e" * 64,
        idempotency_key=f"prep-{uuid.uuid4().hex[:12]}",
        expires_at=NOW + timedelta(days=1),
        created_by=project.engineer_id,
    )
    session.add(row)
    await session.flush()
    return row


def intent(**overrides: object) -> ToolIntent:
    values: dict[str, object] = {
        "tool_name": "seatunnel.submit_job",
        "tool_version": ENGINE_VERSION,
        "effect": ToolEffect.WRITE,
        "subject_id": SUBJECT,
        "subject_project_id": PROJECT,
        "scope": SCOPE,
        "environment": "local",
        "data_sensitivity": DataSensitivity.INTERNAL,
    }
    values.update(overrides)
    return ToolIntent(**values)  # type: ignore[arg-type]


def expectation(**overrides: object) -> CapabilityExpectation:
    values: dict[str, object] = {
        "tool_name": "seatunnel.submit_job",
        "tool_version": ENGINE_VERSION,
        "subject_id": SUBJECT,
        "environment": "local",
        "preparation_id": PREPARATION_ID,
        "artifact_sha256": "a" * 64,
        "input_fingerprint": "b" * 64,
        "resource_fingerprint": SCOPE.fingerprint(),
        "supervision_contract_sha256": "d" * 64,
        "data_classification_sha256": "e" * 64,
    }
    values.update(overrides)
    return CapabilityExpectation(**values)  # type: ignore[arg-type]


def issue(signer: Any, **overrides: object) -> SignedCapability:
    values: dict[str, object] = {
        "preparation_id": PREPARATION_ID,
        "tool_name": "seatunnel.submit_job",
        "tool_version": ENGINE_VERSION,
        "subject_id": SUBJECT,
        "environment": "local",
        "artifact_sha256": "a" * 64,
        "input_fingerprint": "b" * 64,
        "resource_fingerprint": SCOPE.fingerprint(),
        "supervision_contract_sha256": "d" * 64,
        "data_classification_sha256": "e" * 64,
        "policy_bundle_version": "harness-v1-20261016",
        "risk_level": RiskLevel.P0,
        "ttl_seconds": TTL,
        "signer": signer,
        "now": NOW,
    }
    values.update(overrides)
    return issue_capability(**values)  # type: ignore[arg-type]


def call(**overrides: object) -> ToolCall:
    values: dict[str, object] = {
        "intent": intent(),
        "payload": dict(JOB_PAYLOAD),
        "expectation": expectation(),
    }
    values.update(overrides)
    return ToolCall(**values)  # type: ignore[arg-type]


async def prepared(
    session: AsyncSession,
    preparation: ExecutionPreparation,
    issued: SignedCapability,
) -> None:
    """把令牌登记好 —— 没登记的令牌连重放表都进不去。"""
    await record(session, preparation_id=preparation.id, token=issued)


# ------------------------------------------------------------------ 正向路径
async def test_valid_call_reaches_the_executor_exactly_once(
    session: AsyncSession,
    broker: ToolBroker,
    executor: RecordingExecutor,
    keys: tuple[Ed25519Signer, Ed25519KeyRingVerifier],
    preparation: ExecutionPreparation,
) -> None:
    issued = issue(keys[0])
    await prepared(session, preparation, issued)

    receipt = await broker.invoke(
        session,
        call=call(),
        token=issued,
        execution_run_id=uuid.uuid4(),
        now=NOW,
    )

    assert len(executor.calls) == 1
    assert receipt.tool_name == "seatunnel.submit_job"
    assert receipt.risk_level is RiskLevel.P2
    assert receipt.matched_rules[0] == "PLAIN_WRITE"
    assert receipt.token_id == issued.claims.token_id


# ------------------------------------------------------------------ 无绕过路径
async def test_unregistered_tool_never_reaches_the_executor(
    session: AsyncSession,
    broker: ToolBroker,
    executor: RecordingExecutor,
    keys: tuple[Ed25519Signer, Ed25519KeyRingVerifier],
    preparation: ExecutionPreparation,
) -> None:
    issued = issue(keys[0])
    await prepared(session, preparation, issued)

    with pytest.raises(PermissionDenied, match="工具未注册"):
        await broker.invoke(
            session,
            call=call(intent=intent(tool_name="shell.exec")),
            token=issued,
            execution_run_id=uuid.uuid4(),
            now=NOW,
        )

    assert executor.calls == []


async def test_invalid_input_never_reaches_the_executor(
    session: AsyncSession,
    broker: ToolBroker,
    executor: RecordingExecutor,
    keys: tuple[Ed25519Signer, Ed25519KeyRingVerifier],
    preparation: ExecutionPreparation,
) -> None:
    issued = issue(keys[0])
    await prepared(session, preparation, issued)

    with pytest.raises(PermissionDenied, match="不符合契约"):
        await broker.invoke(
            session,
            call=call(payload={**JOB_PAYLOAD, "sneaky": "extra"}),
            token=issued,
            execution_run_id=uuid.uuid4(),
            now=NOW,
        )

    assert executor.calls == []


async def test_volume_above_manifest_ceiling_never_reaches_the_executor(
    session: AsyncSession,
    broker: ToolBroker,
    executor: RecordingExecutor,
    keys: tuple[Ed25519Signer, Ed25519KeyRingVerifier],
    preparation: ExecutionPreparation,
) -> None:
    issued = issue(keys[0])
    await prepared(session, preparation, issued)

    with pytest.raises(PermissionDenied, match="记录上限"):
        await broker.invoke(
            session,
            call=call(records=10_000_001),
            token=issued,
            execution_run_id=uuid.uuid4(),
            now=NOW,
        )

    assert executor.calls == []


async def test_resource_fingerprint_mismatch_never_reaches_the_executor(
    session: AsyncSession,
    broker: ToolBroker,
    executor: RecordingExecutor,
    keys: tuple[Ed25519Signer, Ed25519KeyRingVerifier],
    preparation: ExecutionPreparation,
) -> None:
    """调用方不能声明一份范围、又拿另一份范围的指纹去换令牌。"""
    issued = issue(keys[0])
    await prepared(session, preparation, issued)

    with pytest.raises(PermissionDenied, match="资源指纹与意图"):
        await broker.invoke(
            session,
            call=call(expectation=expectation(resource_fingerprint="9" * 64)),
            token=issued,
            execution_run_id=uuid.uuid4(),
            now=NOW,
        )

    assert executor.calls == []


async def test_policy_denial_never_reaches_the_executor(
    session: AsyncSession,
    broker: ToolBroker,
    executor: RecordingExecutor,
    keys: tuple[Ed25519Signer, Ed25519KeyRingVerifier],
    preparation: ExecutionPreparation,
) -> None:
    """跨项目意图：令牌没被消费，执行器也没被调。"""
    issued = issue(keys[0])
    await prepared(session, preparation, issued)

    with pytest.raises(PermissionDenied, match="策略判定为 DENY"):
        await broker.invoke(
            session,
            call=call(intent=intent(subject_project_id="99999999-9999-9999-9999-999999999999")),
            token=issued,
            execution_run_id=uuid.uuid4(),
            now=NOW,
        )

    assert executor.calls == []


async def test_expired_token_never_reaches_the_executor(
    session: AsyncSession,
    broker: ToolBroker,
    executor: RecordingExecutor,
    keys: tuple[Ed25519Signer, Ed25519KeyRingVerifier],
    preparation: ExecutionPreparation,
) -> None:
    issued = issue(keys[0])
    await prepared(session, preparation, issued)

    with pytest.raises(PermissionDenied, match="已过期"):
        await broker.invoke(
            session,
            call=call(),
            token=issued,
            execution_run_id=uuid.uuid4(),
            now=NOW + timedelta(seconds=TTL + 1),
        )

    assert executor.calls == []


async def test_cross_environment_token_never_reaches_the_executor(
    session: AsyncSession,
    broker: ToolBroker,
    executor: RecordingExecutor,
    keys: tuple[Ed25519Signer, Ed25519KeyRingVerifier],
    preparation: ExecutionPreparation,
) -> None:
    issued = issue(keys[0])
    await prepared(session, preparation, issued)

    with pytest.raises(PermissionDenied, match="environment"):
        await broker.invoke(
            session,
            call=call(expectation=expectation(environment="production")),
            token=issued,
            execution_run_id=uuid.uuid4(),
            now=NOW,
        )

    assert executor.calls == []


async def test_tampered_token_never_reaches_the_executor(
    session: AsyncSession,
    broker: ToolBroker,
    executor: RecordingExecutor,
    keys: tuple[Ed25519Signer, Ed25519KeyRingVerifier],
    preparation: ExecutionPreparation,
) -> None:
    issued = issue(keys[0])
    await prepared(session, preparation, issued)
    tampered = replace(issued, claims=replace(issued.claims, artifact_sha256="f" * 64))

    with pytest.raises(PermissionDenied, match="签名与载荷不匹配"):
        await broker.invoke(
            session,
            call=call(),
            token=tampered,
            execution_run_id=uuid.uuid4(),
            now=NOW,
        )

    assert executor.calls == []


async def test_replayed_token_never_reaches_the_executor_twice(
    session: AsyncSession,
    broker: ToolBroker,
    executor: RecordingExecutor,
    keys: tuple[Ed25519Signer, Ed25519KeyRingVerifier],
    preparation: ExecutionPreparation,
) -> None:
    """第一次成功，第二次被拒 —— 且执行器总共只被调了一次。"""
    issued = issue(keys[0])
    await prepared(session, preparation, issued)

    await broker.invoke(session, call=call(), token=issued, execution_run_id=uuid.uuid4(), now=NOW)
    with pytest.raises(PermissionDenied, match="已被使用过"):
        await broker.invoke(
            session, call=call(), token=issued, execution_run_id=uuid.uuid4(), now=NOW
        )

    assert len(executor.calls) == 1


async def test_unregistered_token_never_reaches_the_executor(
    session: AsyncSession,
    broker: ToolBroker,
    executor: RecordingExecutor,
    keys: tuple[Ed25519Signer, Ed25519KeyRingVerifier],
) -> None:
    """没有登记过的令牌：签名是真的，但它不在台账里，照样不放行。"""
    issued = issue(keys[0])

    with pytest.raises(PermissionDenied, match="不存在或未被登记"):
        await broker.invoke(
            session, call=call(), token=issued, execution_run_id=uuid.uuid4(), now=NOW
        )

    assert executor.calls == []


async def test_stranger_signed_token_never_reaches_the_executor(
    session: AsyncSession,
    broker: ToolBroker,
    executor: RecordingExecutor,
    preparation: ExecutionPreparation,
) -> None:
    """自己拿另一把钥匙签一张令牌：签名合法但公钥映射里没有它。"""
    stranger = Ed25519Signer(key_id="harness-stranger", private_key=generate_keypair()[0])
    issued = issue(stranger)
    await prepared(session, preparation, issued)

    with pytest.raises(PermissionDenied, match="未登记的公钥"):
        await broker.invoke(
            session, call=call(), token=issued, execution_run_id=uuid.uuid4(), now=NOW
        )

    assert executor.calls == []
