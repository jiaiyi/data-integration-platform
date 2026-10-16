"""证据账本：追加式哈希链与签名检查点（T-31 / FR-48、US-22）。

链条把每条事件和它前一条绑在一起：

```
event_hash = sha256(stream_id, sequence, event_type, risk_level,
                    payload_sha256, previous_hash, trace_id)
```

于是改一条事件的载荷、往中间插一条、或者删掉一条，后续所有 `previous_hash` 都对不上。

**但链条本身挡不住「同时能改数据库的人」**：他可以重算整条链。真正让改写留下痕迹的是
每条事件上的签名 —— 没有私钥就签不出新的 `event_hash`。所以两者都要有，
少任何一半都只是「看起来有防篡改」。

完整性验证逐条重算，并把异常**分类报出来**（缺号 / 断链 / 载荷被改 / 签名无效 /
链尾对不上），而不是笼统地返回「校验失败」：排查时要能一眼看出是被删了还是被改了。

⚠️ **部署注意**：本模块只要求一个 `SignerPort`，当前沿用与能力令牌同一把密钥。
等 Worker 开始产生执行证据（M5）时，**必须为证据单独配一把密钥** ——
否则为了让 Worker 能签证据，就得把签发能力的私钥也交给它，
「Worker 在结构上无法自行签发能力」这条边界会直接消失。
"""

import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from data_integration_platform.application.harness.signing import (
    SIGNATURE_ALGORITHM,
    SignatureError,
    SignerPort,
    VerifierPort,
    signing_payload,
)
from data_integration_platform.domain.harness import RiskLevel, canonical_sha256
from data_integration_platform.infrastructure.models import (
    HarnessEvidenceEvent,
    HarnessEvidenceStream,
)

# 链首的前驱哈希；用固定值而不是 NULL，链条的每一环形状才一致。
GENESIS_HASH = "0" * 64
CHECKPOINT_EVENT = "CHECKPOINT"


class EvidenceAnomaly(StrEnum):
    """完整性验证发现的异常类型；分类报出来才好排查。"""

    MISSING_EVENT = "MISSING_EVENT"  # 序号不连续：中间被删
    BROKEN_CHAIN = "BROKEN_CHAIN"  # 前驱哈希对不上：中间被插或被重排
    TAMPERED_PAYLOAD = "TAMPERED_PAYLOAD"  # 载荷摘要或事件哈希对不上：内容被改
    INVALID_SIGNATURE = "INVALID_SIGNATURE"  # 签名验不过：整条事件是伪造的
    STREAM_HEAD_MISMATCH = "STREAM_HEAD_MISMATCH"  # 链尾与流速记的 head 不一致：尾部被删


@dataclass(frozen=True, slots=True)
class EvidenceFinding:
    aggregate_type: str
    aggregate_id: str
    sequence: int | None
    anomaly: EvidenceAnomaly
    detail: str


@dataclass(frozen=True, slots=True)
class EvidenceVerification:
    aggregate_type: str
    aggregate_id: str
    verified_events: int
    findings: tuple[EvidenceFinding, ...] = ()

    @property
    def intact(self) -> bool:
        return not self.findings


async def append(
    session: AsyncSession,
    *,
    aggregate_type: str,
    aggregate_id: str,
    event_type: str,
    risk_level: RiskLevel,
    payload: Mapping[str, Any],
    signer: SignerPort,
    trace_id: str | None = None,
) -> HarnessEvidenceEvent:
    """追加一条签名事件，并把流头推进到它。

    追加是**在调用方的事务里**发生的：证据与它所记录的业务事实同生共死，
    不然会出现「业务回滚了、证据留下了」或反过来的错配。
    """
    stream = await _stream(session, aggregate_type=aggregate_type, aggregate_id=aggregate_id)
    sequence = stream.sequence + 1
    previous_hash = stream.head_hash or GENESIS_HASH
    digest = canonical_sha256(dict(payload))
    event_hash = _event_hash(
        stream_id=stream.id,
        sequence=sequence,
        event_type=event_type,
        risk_level=risk_level,
        payload_sha256=digest,
        previous_hash=previous_hash,
        trace_id=trace_id,
    )
    event = HarnessEvidenceEvent(
        stream_id=stream.id,
        sequence=sequence,
        event_type=event_type,
        risk_level=risk_level,
        payload=dict(payload),
        payload_sha256=digest,
        previous_hash=previous_hash,
        event_hash=event_hash,
        signature_algorithm=SIGNATURE_ALGORITHM,
        signature_key_id=signer.key_id,
        signature_value=signer.sign(signing_payload(_signed_payload(event_hash, digest))),
        trace_id=trace_id,
    )
    session.add(event)
    stream.sequence = sequence
    stream.head_hash = event_hash
    stream.key_id = signer.key_id
    await session.flush()
    return event


async def record_checkpoint(
    session: AsyncSession,
    *,
    aggregate_type: str,
    aggregate_id: str,
    signer: SignerPort,
    risk_level: RiskLevel = RiskLevel.P2,
) -> HarnessEvidenceEvent:
    """记一个签名检查点：对**当前**链尾做一次带签名的背书。

    检查点本身也是一条普通事件，因此它在链里同样可被检验；它额外的价值是留下
    「截至此刻，链尾是 H」这句由持钥方签过的话 —— 事后有人把整条链重算一遍，
    也对不上这条已经签出去的背书。
    """
    stream = await _stream(session, aggregate_type=aggregate_type, aggregate_id=aggregate_id)
    return await append(
        session,
        aggregate_type=aggregate_type,
        aggregate_id=aggregate_id,
        event_type=CHECKPOINT_EVENT,
        risk_level=risk_level,
        payload={
            "attested_head": stream.head_hash or GENESIS_HASH,
            "attested_sequence": stream.sequence,
            "attested_at": datetime.now(UTC).isoformat(),
        },
        signer=signer,
    )


async def verify_stream(
    session: AsyncSession,
    *,
    aggregate_type: str,
    aggregate_id: str,
    verifier: VerifierPort,
) -> EvidenceVerification:
    """逐条重算并验签；返回**所有**异常，而不是遇到第一个就退出。"""
    stream = await session.scalar(
        select(HarnessEvidenceStream).where(
            HarnessEvidenceStream.aggregate_type == aggregate_type,
            HarnessEvidenceStream.aggregate_id == aggregate_id,
        )
    )
    if stream is None:
        return EvidenceVerification(
            aggregate_type=aggregate_type, aggregate_id=aggregate_id, verified_events=0
        )

    events = list(
        (
            await session.scalars(
                select(HarnessEvidenceEvent)
                .where(HarnessEvidenceEvent.stream_id == stream.id)
                .order_by(HarnessEvidenceEvent.sequence)
            )
        ).all()
    )
    findings: list[EvidenceFinding] = []
    expected_sequence = 1
    running_hash = GENESIS_HASH

    for event in events:
        findings.extend(
            _verify_event(
                event,
                aggregate_type=aggregate_type,
                aggregate_id=aggregate_id,
                expected_sequence=expected_sequence,
                running_hash=running_hash,
                verifier=verifier,
            )
        )
        expected_sequence = event.sequence + 1
        running_hash = event.event_hash

    if running_hash != (stream.head_hash or GENESIS_HASH):
        findings.append(
            EvidenceFinding(
                aggregate_type=aggregate_type,
                aggregate_id=aggregate_id,
                sequence=None,
                anomaly=EvidenceAnomaly.STREAM_HEAD_MISMATCH,
                detail=(
                    f"流记录的链尾为 {stream.head_hash!r}，实际最后一条事件哈希为 {running_hash!r}"
                ),
            )
        )
    return EvidenceVerification(
        aggregate_type=aggregate_type,
        aggregate_id=aggregate_id,
        verified_events=len(events),
        findings=tuple(findings),
    )


async def verify_all(
    session: AsyncSession, *, verifier: VerifierPort
) -> Sequence[EvidenceVerification]:
    """校验全部证据流；返回逐流结论（含完好的那些，便于出示「都查过了」）。"""
    streams = (
        await session.scalars(
            select(HarnessEvidenceStream).order_by(
                HarnessEvidenceStream.aggregate_type, HarnessEvidenceStream.aggregate_id
            )
        )
    ).all()
    return [
        await verify_stream(
            session,
            aggregate_type=stream.aggregate_type,
            aggregate_id=stream.aggregate_id,
            verifier=verifier,
        )
        for stream in streams
    ]


def _verify_event(
    event: HarnessEvidenceEvent,
    *,
    aggregate_type: str,
    aggregate_id: str,
    expected_sequence: int,
    running_hash: str,
    verifier: VerifierPort,
) -> list[EvidenceFinding]:
    findings: list[EvidenceFinding] = []

    if event.sequence != expected_sequence:
        findings.append(
            EvidenceFinding(
                aggregate_type=aggregate_type,
                aggregate_id=aggregate_id,
                sequence=event.sequence,
                anomaly=EvidenceAnomaly.MISSING_EVENT,
                detail=f"期望序号 {expected_sequence}，实际 {event.sequence}",
            )
        )
    if event.previous_hash != running_hash:
        findings.append(
            EvidenceFinding(
                aggregate_type=aggregate_type,
                aggregate_id=aggregate_id,
                sequence=event.sequence,
                anomaly=EvidenceAnomaly.BROKEN_CHAIN,
                detail=f"前驱哈希为 {event.previous_hash!r}，上游算得 {running_hash!r}",
            )
        )

    digest = canonical_sha256(event.payload)
    recomputed = _event_hash(
        stream_id=event.stream_id,
        sequence=event.sequence,
        event_type=event.event_type,
        risk_level=event.risk_level,
        payload_sha256=digest,
        previous_hash=event.previous_hash,
        trace_id=event.trace_id,
    )
    if digest != event.payload_sha256 or recomputed != event.event_hash:
        findings.append(
            EvidenceFinding(
                aggregate_type=aggregate_type,
                aggregate_id=aggregate_id,
                sequence=event.sequence,
                anomaly=EvidenceAnomaly.TAMPERED_PAYLOAD,
                detail="载荷或事件哈希与重算结果不一致",
            )
        )

    try:
        verifier.verify(
            signing_payload(_signed_payload(event.event_hash, event.payload_sha256)),
            signature=event.signature_value,
            key_id=event.signature_key_id,
        )
    except SignatureError as exc:
        findings.append(
            EvidenceFinding(
                aggregate_type=aggregate_type,
                aggregate_id=aggregate_id,
                sequence=event.sequence,
                anomaly=EvidenceAnomaly.INVALID_SIGNATURE,
                detail=str(exc),
            )
        )
    return findings


def _event_hash(
    *,
    stream_id: uuid.UUID,
    sequence: int,
    event_type: str,
    risk_level: RiskLevel,
    payload_sha256: str,
    previous_hash: str,
    trace_id: str | None,
) -> str:
    return canonical_sha256(
        {
            "stream_id": str(stream_id),
            "sequence": sequence,
            "event_type": event_type,
            "risk_level": risk_level.value,
            "payload_sha256": payload_sha256,
            "previous_hash": previous_hash,
            "trace_id": trace_id,
        }
    )


def _signed_payload(event_hash: str, payload_sha256: str) -> dict[str, Any]:
    """签名对象同时含事件哈希与载荷摘要：只签哈希的话，摘要列被改就没人发现。"""
    return {"event_hash": event_hash, "payload_sha256": payload_sha256}


async def _stream(
    session: AsyncSession, *, aggregate_type: str, aggregate_id: str
) -> HarnessEvidenceStream:
    stream = await session.scalar(
        select(HarnessEvidenceStream).where(
            HarnessEvidenceStream.aggregate_type == aggregate_type,
            HarnessEvidenceStream.aggregate_id == aggregate_id,
        )
    )
    if stream is None:
        stream = HarnessEvidenceStream(
            aggregate_type=aggregate_type, aggregate_id=aggregate_id, sequence=0
        )
        session.add(stream)
        await session.flush()
    return stream
