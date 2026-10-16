"""证据账本的完整性验证（T-31 / FR-48、US-22）。

证据是**四种篡改都要被发现**：删除、插入、改写、伪造检查点。
每一种都在这里对着数据库真改一行，然后要求验证器报出**对应类别**的异常 ——
只断言「校验没过」是不够的：排查时要能一眼看出是被删了还是被改了。
"""

import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from data_integration_platform.application.harness.ledger import (
    CHECKPOINT_EVENT,
    GENESIS_HASH,
    EvidenceAnomaly,
    append,
    record_checkpoint,
    verify_all,
    verify_stream,
)
from data_integration_platform.application.harness.signing import signing_payload
from data_integration_platform.domain.harness import RiskLevel, canonical_sha256
from data_integration_platform.infrastructure.ed25519_signing import (
    Ed25519KeyRingVerifier,
    Ed25519Signer,
    generate_keypair,
    parse_public_keys,
)
from data_integration_platform.infrastructure.models import (
    HarnessEvidenceEvent,
    HarnessEvidenceStream,
)

AGGREGATE_TYPE = "execution"
KEY_ID = "harness-local-1"


@pytest.fixture(scope="module")
def keys() -> tuple[Ed25519Signer, Ed25519KeyRingVerifier]:
    private, public = generate_keypair()
    return (
        Ed25519Signer(key_id=KEY_ID, private_key=private),
        Ed25519KeyRingVerifier(parse_public_keys(f"{KEY_ID}:{public}")),
    )


def event_payload(index: int) -> dict[str, object]:
    return {"step": index, "at": datetime(2026, 10, 16, 2, 0, tzinfo=UTC).isoformat()}


async def stream_with_events(
    session: AsyncSession, signer: Ed25519Signer, *, count: int = 3
) -> str:
    aggregate_id = uuid.uuid4().hex
    for index in range(count):
        await append(
            session,
            aggregate_type=AGGREGATE_TYPE,
            aggregate_id=aggregate_id,
            event_type="EXECUTION_STEP",
            risk_level=RiskLevel.P2,
            payload=event_payload(index),
            signer=signer,
            trace_id="trace-0001",
        )
    return aggregate_id


async def events_of(session: AsyncSession, aggregate_id: str) -> list[HarnessEvidenceEvent]:
    stream = await session.scalar(
        select(HarnessEvidenceStream).where(HarnessEvidenceStream.aggregate_id == aggregate_id)
    )
    assert stream is not None
    rows = await session.scalars(
        select(HarnessEvidenceEvent)
        .where(HarnessEvidenceEvent.stream_id == stream.id)
        .order_by(HarnessEvidenceEvent.sequence)
    )
    return list(rows.all())


def anomalies(verification: object) -> set[EvidenceAnomaly]:
    return {finding.anomaly for finding in verification.findings}  # type: ignore[attr-defined]


# ------------------------------------------------------------------ 完好路径
async def test_intact_chain_verifies(
    session: AsyncSession, keys: tuple[Ed25519Signer, Ed25519KeyRingVerifier]
) -> None:
    aggregate_id = await stream_with_events(session, keys[0])

    verification = await verify_stream(
        session,
        aggregate_type=AGGREGATE_TYPE,
        aggregate_id=aggregate_id,
        verifier=keys[1],
    )

    assert verification.intact
    assert verification.verified_events == 3


async def test_chain_links_each_event_to_its_predecessor(
    session: AsyncSession, keys: tuple[Ed25519Signer, Ed25519KeyRingVerifier]
) -> None:
    aggregate_id = await stream_with_events(session, keys[0])
    events = await events_of(session, aggregate_id)

    assert [event.sequence for event in events] == [1, 2, 3]
    assert events[0].previous_hash == GENESIS_HASH
    assert events[1].previous_hash == events[0].event_hash
    assert events[2].previous_hash == events[1].event_hash


async def test_unknown_stream_verifies_as_empty(session: AsyncSession) -> None:
    """从来没记过证据的聚合不算被篡改。"""
    verification = await verify_stream(
        session,
        aggregate_type=AGGREGATE_TYPE,
        aggregate_id="没有这个聚合",
        verifier=Ed25519KeyRingVerifier({}),
    )

    assert verification.intact
    assert verification.verified_events == 0


# ------------------------------------------------------------------ 删除
async def test_deleted_event_is_detected(
    session: AsyncSession, keys: tuple[Ed25519Signer, Ed25519KeyRingVerifier]
) -> None:
    """删掉中间一条：序号出现断档，同时后续事件的前驱哈希也对不上。"""
    aggregate_id = await stream_with_events(session, keys[0])
    middle = (await events_of(session, aggregate_id))[1]
    await session.execute(delete(HarnessEvidenceEvent).where(HarnessEvidenceEvent.id == middle.id))
    await session.flush()

    verification = await verify_stream(
        session, aggregate_type=AGGREGATE_TYPE, aggregate_id=aggregate_id, verifier=keys[1]
    )

    assert not verification.intact
    assert EvidenceAnomaly.MISSING_EVENT in anomalies(verification)


async def test_truncated_tail_is_detected(
    session: AsyncSession, keys: tuple[Ed25519Signer, Ed25519KeyRingVerifier]
) -> None:
    """只删尾部不会留下断号，但流速记的链尾与实际最后一条对不上。"""
    aggregate_id = await stream_with_events(session, keys[0])
    last = (await events_of(session, aggregate_id))[-1]
    await session.execute(delete(HarnessEvidenceEvent).where(HarnessEvidenceEvent.id == last.id))
    await session.flush()

    verification = await verify_stream(
        session, aggregate_type=AGGREGATE_TYPE, aggregate_id=aggregate_id, verifier=keys[1]
    )

    assert EvidenceAnomaly.STREAM_HEAD_MISMATCH in anomalies(verification)


# ------------------------------------------------------------------ 插入
async def test_inserted_event_is_detected(
    session: AsyncSession, keys: tuple[Ed25519Signer, Ed25519KeyRingVerifier]
) -> None:
    """往中间插一条：它的前驱哈希必然与上游算出来的对不上。

    序号上有唯一约束，所以现实中的「插入」总是「换掉一条再多一条」——
    这里先把中间那条删掉，再在同号位置塞进一条伪造的。
    """
    aggregate_id = await stream_with_events(session, keys[0])
    victim = (await events_of(session, aggregate_id))[1]
    await session.execute(delete(HarnessEvidenceEvent).where(HarnessEvidenceEvent.id == victim.id))
    await session.flush()
    forged = HarnessEvidenceEvent(
        stream_id=victim.stream_id,
        sequence=victim.sequence,
        event_type="EXECUTION_STEP",
        risk_level=RiskLevel.P2,
        payload={"step": "伪造"},
        payload_sha256=canonical_sha256({"step": "伪造"}),
        previous_hash="d" * 64,
        event_hash="f" * 64,
        signature_algorithm="Ed25519",
        signature_key_id=KEY_ID,
        signature_value="伪造的签名",
    )
    session.add(forged)
    await session.flush()

    verification = await verify_stream(
        session, aggregate_type=AGGREGATE_TYPE, aggregate_id=aggregate_id, verifier=keys[1]
    )

    assert EvidenceAnomaly.BROKEN_CHAIN in anomalies(verification)
    assert EvidenceAnomaly.TAMPERED_PAYLOAD in anomalies(verification)
    assert EvidenceAnomaly.INVALID_SIGNATURE in anomalies(verification)


# ------------------------------------------------------------------ 改写
async def test_rewritten_payload_is_detected(
    session: AsyncSession, keys: tuple[Ed25519Signer, Ed25519KeyRingVerifier]
) -> None:
    """改一条事件的载荷：摘要与重算结果对不上。"""
    aggregate_id = await stream_with_events(session, keys[0])
    target = (await events_of(session, aggregate_id))[0]
    target.payload = {"step": "被改过", "at": "2026-10-16T02:00:00+00:00"}
    await session.flush()

    verification = await verify_stream(
        session, aggregate_type=AGGREGATE_TYPE, aggregate_id=aggregate_id, verifier=keys[1]
    )

    assert EvidenceAnomaly.TAMPERED_PAYLOAD in anomalies(verification)


async def test_rewritten_event_hash_is_detected(
    session: AsyncSession, keys: tuple[Ed25519Signer, Ed25519KeyRingVerifier]
) -> None:
    """只改事件哈希、不动载荷：重算之后仍然对不上。"""
    aggregate_id = await stream_with_events(session, keys[0])
    target = (await events_of(session, aggregate_id))[0]
    target.event_hash = "e" * 64
    await session.flush()

    verification = await verify_stream(
        session, aggregate_type=AGGREGATE_TYPE, aggregate_id=aggregate_id, verifier=keys[1]
    )

    assert EvidenceAnomaly.TAMPERED_PAYLOAD in anomalies(verification)


async def test_rewritten_signature_is_detected(
    session: AsyncSession, keys: tuple[Ed25519Signer, Ed25519KeyRingVerifier]
) -> None:
    """签名被换成别的：验签不过。"""
    aggregate_id = await stream_with_events(session, keys[0])
    target = (await events_of(session, aggregate_id))[0]
    target.signature_value = "不是签名"
    await session.flush()

    verification = await verify_stream(
        session, aggregate_type=AGGREGATE_TYPE, aggregate_id=aggregate_id, verifier=keys[1]
    )

    assert EvidenceAnomaly.INVALID_SIGNATURE in anomalies(verification)


async def test_whole_chain_recomputed_by_an_outsider_is_detected(
    session: AsyncSession, keys: tuple[Ed25519Signer, Ed25519KeyRingVerifier]
) -> None:
    """**攻击者重写整条链**：哈希能自洽（它自己算的），但签不出原来的签名。"""
    aggregate_id = await stream_with_events(session, keys[0])
    stranger_private, _ = generate_keypair()
    stranger = Ed25519Signer(key_id="harness-stranger", private_key=stranger_private)
    events = await events_of(session, aggregate_id)
    for index, event in enumerate(events):
        event.payload = {"step": f"被改写 {index}"}
        event.payload_sha256 = canonical_sha256(event.payload)
        event.signature_key_id = stranger.key_id
        event.signature_value = stranger.sign(
            signing_payload(
                {"event_hash": event.event_hash, "payload_sha256": event.payload_sha256}
            )
        )
    await session.flush()

    verification = await verify_stream(
        session, aggregate_type=AGGREGATE_TYPE, aggregate_id=aggregate_id, verifier=keys[1]
    )

    # 哈希链被重算过所以自洽，但公钥映射里没有攻击者那把钥匙 —— 伪造在签名这一环暴露。
    assert EvidenceAnomaly.INVALID_SIGNATURE in anomalies(verification)


# ------------------------------------------------------------------ 检查点
async def test_checkpoint_attests_the_head(
    session: AsyncSession, keys: tuple[Ed25519Signer, Ed25519KeyRingVerifier]
) -> None:
    aggregate_id = await stream_with_events(session, keys[0], count=2)
    head_before = (await events_of(session, aggregate_id))[-1].event_hash

    checkpoint_event = await record_checkpoint(
        session, aggregate_type=AGGREGATE_TYPE, aggregate_id=aggregate_id, signer=keys[0]
    )

    assert checkpoint_event.event_type == CHECKPOINT_EVENT
    assert checkpoint_event.payload["attested_head"] == head_before
    assert checkpoint_event.payload["attested_sequence"] == 2
    assert (
        await verify_stream(
            session, aggregate_type=AGGREGATE_TYPE, aggregate_id=aggregate_id, verifier=keys[1]
        )
    ).intact


async def test_forged_checkpoint_is_detected(
    session: AsyncSession, keys: tuple[Ed25519Signer, Ed25519KeyRingVerifier]
) -> None:
    """伪造检查点：拿别的钥匙签一个「链尾是 H」的背书。"""
    aggregate_id = await stream_with_events(session, keys[0], count=1)
    checkpoint_event = await record_checkpoint(
        session, aggregate_type=AGGREGATE_TYPE, aggregate_id=aggregate_id, signer=keys[0]
    )
    stranger_private, _ = generate_keypair()
    stranger = Ed25519Signer(key_id=KEY_ID, private_key=stranger_private)
    checkpoint_event.payload = {"attested_head": "0" * 64, "attested_sequence": 0}
    checkpoint_event.payload_sha256 = canonical_sha256(checkpoint_event.payload)
    checkpoint_event.signature_value = stranger.sign(
        signing_payload(
            {
                "event_hash": checkpoint_event.event_hash,
                "payload_sha256": checkpoint_event.payload_sha256,
            }
        )
    )
    await session.flush()

    verification = await verify_stream(
        session, aggregate_type=AGGREGATE_TYPE, aggregate_id=aggregate_id, verifier=keys[1]
    )

    assert EvidenceAnomaly.INVALID_SIGNATURE in anomalies(verification)
    assert EvidenceAnomaly.TAMPERED_PAYLOAD in anomalies(verification)


# ------------------------------------------------------------------ 全量校验
async def test_verify_all_covers_every_stream(
    session: AsyncSession, keys: tuple[Ed25519Signer, Ed25519KeyRingVerifier]
) -> None:
    intact_id = await stream_with_events(session, keys[0], count=2)
    broken_id = await stream_with_events(session, keys[0], count=2)
    target = (await events_of(session, broken_id))[0]
    target.payload = {"step": "被改过"}

    verifications = await verify_all(session, verifier=keys[1])

    by_id = {item.aggregate_id: item for item in verifications}
    assert by_id[intact_id].intact
    assert not by_id[broken_id].intact


async def test_verification_reports_every_anomaly_not_just_the_first(
    session: AsyncSession, keys: tuple[Ed25519Signer, Ed25519KeyRingVerifier]
) -> None:
    """一次校验要把问题报全，而不是遇到第一个就退出 —— 排查一次就够。"""
    aggregate_id = await stream_with_events(session, keys[0], count=3)
    events = await events_of(session, aggregate_id)
    events[0].payload = {"step": "改过"}
    events[2].signature_value = "坏的"
    await session.flush()

    verification = await verify_stream(
        session, aggregate_type=AGGREGATE_TYPE, aggregate_id=aggregate_id, verifier=keys[1]
    )

    assert len(verification.findings) >= 2
    assert EvidenceAnomaly.TAMPERED_PAYLOAD in anomalies(verification)
    assert EvidenceAnomaly.INVALID_SIGNATURE in anomalies(verification)


async def test_missing_public_key_makes_verification_fail_closed(
    session: AsyncSession, keys: tuple[Ed25519Signer, Ed25519KeyRingVerifier]
) -> None:
    """没有公钥时证据「验不过」而不是「默认通过」—— 失败关闭。"""
    aggregate_id = await stream_with_events(session, keys[0], count=1)

    verification = await verify_stream(
        session,
        aggregate_type=AGGREGATE_TYPE,
        aggregate_id=aggregate_id,
        verifier=Ed25519KeyRingVerifier({}),
    )

    assert not verification.intact
    assert EvidenceAnomaly.INVALID_SIGNATURE in anomalies(verification)
