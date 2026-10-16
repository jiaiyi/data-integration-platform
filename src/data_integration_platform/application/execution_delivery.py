"""执行结果的受管交付（T-40）。

**交付什么**：引擎目前只回作业状态，不回结果数据（数据直接落在 Doris 里），
所以这里交付的是**执行报告**——这次执行自己产生的那份聚合事实：
状态与时间线、指标、失败归类与诊断、监督动作、质量结论。它不是一个「导出数据」
功能，而是「把这次执行发生过什么留成一份可取回、可校验的文件」。

**为什么用受管文件对象**：一份落在共享盘上的 JSON 没有任何约束；落成 `FileAsset`
之后，它就带上项目归属、大小与行数上限校验、内容摘要、审计记录，并且能按执行反查。

**三条约束**：

1. **只在终态交付**。跑着的执行没有结论，交付一份「还没跑完」的报告等于给审计
   一个会过期的答案。
2. **同一份事实只交付一次**。`(execution_run_id, name)` 上有唯一约束，重复交付返回
   已有的那一份 —— 落两份内容相同的文件，事后没人分得清哪份是权威。
3. **脱敏在写入之前**（FR-08、G-07）。诊断与原因文本可能带驱动抛出的连接串，
   逐字段过一遍 `scrub_secrets`；**只擦字符串值、不动键名** —— 整段文本丢给
   关键词正则会把 `"api_key": "..."` 改写成 `api_key=***`，产出一份不是 JSON 的「JSON」。

报告用**排序键 + 固定缩进**的 JSON 落盘：既人能读，又让同一份事实重复交付得到
同一个摘要 —— 摘要可复现是审计物该有的性质。
"""

import json
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from data_integration_platform.application.access import require_project_roles
from data_integration_platform.application.connections import WRITABLE
from data_integration_platform.application.executions import get_run
from data_integration_platform.application.file_assets import FileAssetService
from data_integration_platform.application.harness.side_effect_targets import project_of_run
from data_integration_platform.application.masking import scrub_secrets
from data_integration_platform.domain.enums import ExecutionStatus
from data_integration_platform.domain.errors import DomainError
from data_integration_platform.infrastructure.models import (
    ExecutionPreparation,
    ExecutionRun,
    FileAsset,
    QualityReport,
)

# 交付报告的内容版本；字段增删改语义时同步改它，让旧报告读起来仍有确定的解释。
REPORT_SCHEMA_VERSION = 1

# 有结论的状态。`CANCELLING` 刻意不在里面：那时引擎未必停了，报告里那句话会变成假话。
TERMINAL = frozenset({ExecutionStatus.SUCCEEDED, ExecutionStatus.FAILED, ExecutionStatus.CANCELLED})


def report_name(run_id: uuid.UUID) -> str:
    """交付文件名；**由执行 id 推导**，所以同名即同一份交付物。"""
    return f"execution-report-{run_id}.json"


async def deliver_report(
    session: AsyncSession,
    *,
    run_id: uuid.UUID,
    actor_id: uuid.UUID,
    settings_root: Path | None = None,
    trace_id: str | None = None,
    now: datetime | None = None,
) -> FileAsset:
    """把一次执行的结果交付成受管文件资产；重复调用返回同一份。"""
    run = await get_run(session, run_id=run_id, actor_id=actor_id)
    project_id = await project_of_run(session, run)
    # 交付是一次写动作（往受管目录落文件），所以按写入口校验。
    await require_project_roles(
        session, project_id=project_id, actor_id=actor_id, allowed=WRITABLE, write=True
    )
    if run.status not in TERMINAL:
        raise DomainError(f"执行当前状态为 {run.status.value}，只有跑出结论的执行才能交付报告")

    name = report_name(run.id)
    existing = await find_delivery(session, run_id=run.id, name=name)
    if existing is not None:
        # 重复交付返回已有那一份，而不是再落一份内容相同的文件。
        return existing

    payload = scrub_report(
        await _report_payload(
            session,
            run=run,
            project_id=project_id,
            delivered_at=now if now is not None else datetime.now(UTC),
        )
    )
    data = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False
    ).encode("utf-8")
    return await FileAssetService(settings_root).store_generated(
        session,
        project_id=project_id,
        name=name,
        data=data,
        execution_run_id=run.id,
        actor_id=actor_id,
        trace_id=trace_id,
    )


async def find_delivery(session: AsyncSession, *, run_id: uuid.UUID, name: str) -> FileAsset | None:
    return await session.scalar(
        select(FileAsset).where(FileAsset.execution_run_id == run_id, FileAsset.name == name)
    )


async def deliveries_of(
    session: AsyncSession, *, run_id: uuid.UUID, actor_id: uuid.UUID
) -> list[FileAsset]:
    """列出某次执行已交付的文件；先经 `get_run` 走一次项目读权限。"""
    await get_run(session, run_id=run_id, actor_id=actor_id)
    rows = await session.scalars(
        select(FileAsset)
        .where(FileAsset.execution_run_id == run_id)
        .order_by(FileAsset.created_at.desc())
    )
    return list(rows.all())


async def _report_payload(
    session: AsyncSession,
    *,
    run: ExecutionRun,
    project_id: uuid.UUID,
    delivered_at: datetime,
) -> dict[str, Any]:
    preparation = await session.get(ExecutionPreparation, run.harness_preparation_id)
    quality = await session.scalar(select(QualityReport).where(QualityReport.run_id == run.id))
    return {
        "schema_version": REPORT_SCHEMA_VERSION,
        "delivered_at": delivered_at.isoformat(),
        "project_id": str(project_id),
        "execution": {
            "run_id": str(run.id),
            "pipeline_version_id": str(run.pipeline_version_id),
            "preparation_id": str(run.harness_preparation_id),
            "artifact_sha256": preparation.artifact_sha256 if preparation else None,
            "risk_level": preparation.risk_level.value if preparation else None,
            "environment": preparation.environment if preparation else None,
            "status": run.status.value,
            "trigger": run.trigger.value,
            "attempt_number": run.attempt_number,
            "retry_of_run_id": str(run.retry_of_run_id) if run.retry_of_run_id else None,
            "engine_status": run.engine_status,
            "seatunnel_job_id": run.seatunnel_job_id,
            "submitted_at": _iso(run.submitted_at),
            "started_at": _iso(run.started_at),
            "finished_at": _iso(run.finished_at),
            "last_observed_at": _iso(run.last_observed_at),
            "cancel_requested_at": _iso(run.cancel_requested_at),
            "cancel_reason": run.cancel_reason,
        },
        # 指标留空就是 null：**「没量到」与「量到 0 行」不是一回事**（FR-61）。
        "metrics": {
            "source_rows": run.source_rows,
            "sink_rows": run.sink_rows,
            "source_bytes": run.source_bytes,
            "sink_bytes": run.sink_bytes,
            "table_metrics": run.table_metrics,
        },
        "failure": {
            "category": run.failure_category.value if run.failure_category else None,
            "stage": run.failure_stage.value if run.failure_stage else None,
            "retryable": run.retryable,
            "diagnosis": run.diagnosis,
        },
        "supervision": {
            "action": run.supervision_action.value if run.supervision_action else None,
            "contract_sha256": run.supervision_contract_sha256,
            "quarantined_at": _iso(run.quarantined_at),
            "quarantine_reason": run.quarantine_reason,
        },
        # 质量报告属 M6 的 T-41；还没有就交付 null，不编一份空的出来。
        "quality": _quality_payload(quality),
    }


def _quality_payload(quality: QualityReport | None) -> dict[str, Any] | None:
    if quality is None:
        return None
    return {
        "status": quality.status.value,
        "valid_rows": quality.valid_rows,
        "rejected_rows": quality.rejected_rows,
        "error_code_distribution": quality.error_code_distribution,
        "unmatched_rule_samples": quality.unmatched_rule_samples,
        "minimum_rows_required": quality.minimum_rows_required,
        "gate_passed": quality.gate_passed,
        "published_at": _iso(quality.published_at),
    }


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def scrub_report(payload: Any) -> Any:
    """递归擦除字符串值里的疑似凭据；**键名不动**（见模块文档第 3 条）。

    对交付载荷统一走这一遍，而不是逐个字段记得去擦 —— 报告会随着 M6 长出新的
    诊断字段，靠「记得擦」迟早漏一个。
    """
    if isinstance(payload, str):
        return scrub_secrets(payload)
    if isinstance(payload, dict):
        return {key: scrub_report(value) for key, value in payload.items()}
    if isinstance(payload, list):
        return [scrub_report(item) for item in payload]
    return payload
