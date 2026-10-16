"""不可变版本与制品冻结（T-22 / FR-36、FR-39）。

冻结把候选固化成三份不可变制品 —— HOCON、EtlPlan、审查视图 —— 并算出摘要；
**冻结之后内容不再改写**。需求变更走新版本 V+1（`revise`），旧版本原封不动，
因此「审批人看到的就是将要执行的内容」（US-09 / US-13）在结构上成立，而不是靠约定。

三条不变量：

1. **冻结只在门禁之后发生**：`freeze` 只接受 `GENERATED`，由状态机迁移到 `VALIDATED`。
   走到这里说明确定性门禁与真实引擎检查都已通过（FR-35）。
2. **摘要只覆盖内容**：`artifact_sha256` 是 `GeneratedCandidate.canonical_payload()`
   的规范摘要，因此同一份候选重复冻结得到同一个值（PRD G-05 复现率 = 100%）。
   审查视图是 plan 与 hocon 的确定性投影，所以摘要覆盖它而不必单独入摘要。
3. **冻结不可重入**：已冻结的版本再冻结直接拒绝，而不是覆盖摘要。

文件落盘**不是**不可变性的执行边界 —— 真正挡住改写的是这里的状态检查与
`pipeline_artifacts(version_id, artifact_type)` 的唯一约束。落盘只负责存放字节。
"""

import hashlib
import json
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from data_integration_platform.application.access import require_project_roles
from data_integration_platform.application.audit import record_audit_event
from data_integration_platform.application.generation import GeneratedCandidate
from data_integration_platform.config import get_settings
from data_integration_platform.domain.enums import (
    ApprovalStatus,
    AuditEventType,
    PipelineVersionStatus,
    ProjectRole,
)
from data_integration_platform.domain.errors import DomainError, ResourceNotFound
from data_integration_platform.domain.harness import canonical_json, canonical_sha256
from data_integration_platform.domain.state_machine import require_pipeline_transition
from data_integration_platform.infrastructure.models import (
    ApprovalRequest,
    Pipeline,
    PipelineArtifact,
    PipelineVersion,
)
from data_integration_platform.infrastructure.probe_support import run_blocking

WRITABLE = frozenset({ProjectRole.ADMIN, ProjectRole.DATA_ENGINEER})
READABLE = frozenset(
    {ProjectRole.ADMIN, ProjectRole.DATA_ENGINEER, ProjectRole.APPROVER, ProjectRole.VIEWER}
)

# 制品类型与冻结时的落盘后缀。审查视图落成 JSON 而不是人类可读文本：
# 审批界面（M7）要逐字段渲染，结构化文档比自然语言更不容易产生歧义。
ARTIFACT_HOCON = "HOCON"
ARTIFACT_ETL_PLAN = "ETL_PLAN"
ARTIFACT_REVIEW_VIEW = "REVIEW_VIEW"
_ARTIFACT_SUFFIX = {
    ARTIFACT_HOCON: ".conf",
    ARTIFACT_ETL_PLAN: ".json",
    ARTIFACT_REVIEW_VIEW: ".json",
}


class PipelineVersionService:
    def __init__(self, root: Path | None = None) -> None:
        self._root = root

    def root(self) -> Path:
        """制品根目录；只来自平台配置，不接受请求指定。"""
        return self._root if self._root is not None else get_settings().artifact_root

    # -------------------------------------------------------------- 版本
    async def create(
        self,
        session: AsyncSession,
        *,
        project_id: uuid.UUID,
        actor_id: uuid.UUID,
        name: str,
        requirement: str,
    ) -> PipelineVersion:
        """创建管道与它的第一个版本；同名管道已存在时续在其版本序列之后。"""
        await require_project_roles(
            session, project_id=project_id, actor_id=actor_id, allowed=WRITABLE, write=True
        )
        pipeline = await session.scalar(
            select(Pipeline).where(Pipeline.project_id == project_id, Pipeline.name == name)
        )
        if pipeline is None:
            pipeline = Pipeline(project_id=project_id, name=name)
            session.add(pipeline)
            await session.flush()

        version = await self._new_version(
            session,
            pipeline=pipeline,
            actor_id=actor_id,
            requirement=requirement,
            parent_version_id=None,
        )
        record_audit_event(
            session,
            event_type=AuditEventType.PIPELINE_VERSION_CREATED,
            target_type="pipeline_version",
            project_id=project_id,
            actor_id=actor_id,
            target_id=str(version.id),
            change_summary={
                "pipeline_id": str(pipeline.id),
                "version_number": version.version_number,
            },
        )
        return version

    async def revise(
        self,
        session: AsyncSession,
        *,
        version_id: uuid.UUID,
        actor_id: uuid.UUID,
        requirement: str,
    ) -> PipelineVersion:
        """需求变更：创建 V+1 并把它挂在父版本之后，**不触碰父版本**（FR-39）。"""
        parent = await self._require_version(session, version_id)
        pipeline = await session.get(Pipeline, parent.pipeline_id)
        if pipeline is None:  # 外键保证不会发生，留一条明确报错胜过 None 解引用
            raise ResourceNotFound("管道不存在")
        await require_project_roles(
            session,
            project_id=pipeline.project_id,
            actor_id=actor_id,
            allowed=WRITABLE,
            write=True,
        )

        version = await self._new_version(
            session,
            pipeline=pipeline,
            actor_id=actor_id,
            requirement=requirement,
            parent_version_id=parent.id,
        )
        record_audit_event(
            session,
            event_type=AuditEventType.PIPELINE_VERSION_REVISED,
            target_type="pipeline_version",
            project_id=pipeline.project_id,
            actor_id=actor_id,
            target_id=str(version.id),
            change_summary={
                "pipeline_id": str(pipeline.id),
                "version_number": version.version_number,
                "parent_version_id": str(parent.id),
                "parent_artifact_sha256": parent.artifact_sha256,
            },
        )
        return version

    # -------------------------------------------------------------- 冻结
    async def freeze(
        self,
        session: AsyncSession,
        *,
        version_id: uuid.UUID,
        actor_id: uuid.UUID,
        candidate: GeneratedCandidate,
    ) -> PipelineVersion:
        """把候选冻结成不可变制品并算出摘要。

        只接受 `GENERATED`：那意味着生成已完成且门禁与真实引擎检查都已通过；
        其余状态由状态机拒绝，不走「先冻结再解释」那条路。
        """
        version = await self._require_version(session, version_id)
        pipeline = await session.get(Pipeline, version.pipeline_id)
        if pipeline is None:
            raise ResourceNotFound("管道不存在")
        await require_project_roles(
            session,
            project_id=pipeline.project_id,
            actor_id=actor_id,
            allowed=WRITABLE,
            write=True,
        )
        if version.artifact_sha256 is not None:
            raise DomainError(
                f"版本 v{version.version_number} 已冻结（摘要 {version.artifact_sha256}）；"
                "需求变更请创建 V+1，不覆盖已有版本"
            )
        # 跳步与终态重启都在这里被拒：DRAFT 直接冻结、已审批版本重新冻结都不成立。
        require_pipeline_transition(version.status, PipelineVersionStatus.VALIDATED)

        artifacts = _artifacts(candidate)
        digest = canonical_sha256(candidate.canonical_payload())
        directory = f"{pipeline.project_id}/{version.id}"
        for artifact_type, payload in artifacts.items():
            await self._store(
                session,
                version=version,
                directory=directory,
                artifact_type=artifact_type,
                payload=payload,
            )

        version.plan = candidate.plan.model_dump(mode="json")
        version.artifact_sha256 = digest
        version.status = PipelineVersionStatus.VALIDATED
        await session.flush()

        record_audit_event(
            session,
            event_type=AuditEventType.PIPELINE_VERSION_FROZEN,
            target_type="pipeline_version",
            project_id=pipeline.project_id,
            actor_id=actor_id,
            target_id=str(version.id),
            change_summary={
                "version_number": version.version_number,
                "artifact_sha256": digest,
                "artifacts": sorted(artifacts),
            },
        )
        return version

    # -------------------------------------------------------------- 提交审批
    async def submit_for_approval(
        self,
        session: AsyncSession,
        *,
        version_id: uuid.UUID,
        actor_id: uuid.UUID,
        trace_id: str | None = None,
    ) -> ApprovalRequest:
        """把已冻结的版本提交审批：状态迁到 `WAITING_APPROVAL` 并绑定制品摘要。

        审批单**绑定 `artifact_sha256`**（SOD-04）：内容一变摘要就变，
        旧审批自然失效 —— 这里不做「审批后替换内容」的补救，因为结构上不允许。
        审批的**决策**（通过/驳回）不在本方法里，属 M5 的审批链路。
        """
        version = await self._require_version(session, version_id)
        pipeline = await session.get(Pipeline, version.pipeline_id)
        if pipeline is None:
            raise ResourceNotFound("管道不存在")
        await require_project_roles(
            session,
            project_id=pipeline.project_id,
            actor_id=actor_id,
            allowed=WRITABLE,
            write=True,
        )
        if version.artifact_sha256 is None:
            raise DomainError("版本尚未冻结，没有可提交审批的制品摘要")
        pending = await session.scalar(
            select(ApprovalRequest.id).where(
                ApprovalRequest.version_id == version.id,
                ApprovalRequest.status == ApprovalStatus.PENDING,
            )
        )
        if pending is not None:
            raise DomainError("该版本已有待审批的申请，不重复提交")
        require_pipeline_transition(version.status, PipelineVersionStatus.WAITING_APPROVAL)

        request = ApprovalRequest(
            version_id=version.id,
            status=ApprovalStatus.PENDING,
            artifact_sha256=version.artifact_sha256,
            requested_by=actor_id,
        )
        session.add(request)
        version.status = PipelineVersionStatus.WAITING_APPROVAL
        await session.flush()
        record_audit_event(
            session,
            event_type=AuditEventType.PIPELINE_VERSION_SUBMITTED,
            target_type="approval_request",
            project_id=pipeline.project_id,
            actor_id=actor_id,
            target_id=str(request.id),
            change_summary={
                "version_id": str(version.id),
                "artifact_sha256": version.artifact_sha256,
            },
            trace_id=trace_id,
        )
        return request

    # -------------------------------------------------------------- 读
    async def design_view(
        self, session: AsyncSession, *, version_id: uuid.UUID, actor_id: uuid.UUID
    ) -> "DesignView":
        """审批人看到的内容：**从冻结制品读回**，不是从内存里的候选读。

        这样「审批人看到的就是将要执行的内容」（US-13）才被真正验证过 ——
        审批界面渲染的和执行阶段拿到的是同一份字节。
        """
        version = await self._require_version(session, version_id)
        pipeline = await session.get(Pipeline, version.pipeline_id)
        if pipeline is None:
            raise ResourceNotFound("管道不存在")
        await require_project_roles(
            session, project_id=pipeline.project_id, actor_id=actor_id, allowed=READABLE
        )
        artifacts = await self.artifacts_of(session, version_id=version.id)
        plan: dict[str, Any] | None = None
        hocon: str | None = None
        if ARTIFACT_ETL_PLAN in artifacts:
            raw = await self.read_artifact(artifacts[ARTIFACT_ETL_PLAN])
            plan = json.loads(raw.decode("utf-8"))
        if ARTIFACT_HOCON in artifacts:
            hocon = (await self.read_artifact(artifacts[ARTIFACT_HOCON])).decode("utf-8")
        return DesignView(
            version_id=version.id,
            status=version.status,
            artifact_sha256=version.artifact_sha256,
            plan=plan,
            hocon=hocon,
        )

    async def artifacts_of(
        self, session: AsyncSession, *, version_id: uuid.UUID
    ) -> dict[str, PipelineArtifact]:
        """列出某版本已冻结的制品；未冻结的版本返回空字典。"""
        rows = await session.scalars(
            select(PipelineArtifact).where(PipelineArtifact.version_id == version_id)
        )
        return {artifact.artifact_type: artifact for artifact in rows.all()}

    async def read_artifact(self, artifact: PipelineArtifact) -> bytes:
        """读回制品原始字节；调用方据其 `sha256` 自行校验完整性。"""
        return await run_blocking((self.root() / artifact.storage_ref).read_bytes)

    # -------------------------------------------------------------- 内部
    async def _new_version(
        self,
        session: AsyncSession,
        *,
        pipeline: Pipeline,
        actor_id: uuid.UUID,
        requirement: str,
        parent_version_id: uuid.UUID | None,
    ) -> PipelineVersion:
        highest = await session.scalar(
            select(func.max(PipelineVersion.version_number)).where(
                PipelineVersion.pipeline_id == pipeline.id
            )
        )
        version = PipelineVersion(
            pipeline_id=pipeline.id,
            version_number=(highest or 0) + 1,
            status=PipelineVersionStatus.DRAFT,
            requirement=requirement,
            parent_version_id=parent_version_id,
            created_by=actor_id,
        )
        session.add(version)
        await session.flush()
        return version

    async def _store(
        self,
        session: AsyncSession,
        *,
        version: PipelineVersion,
        directory: str,
        artifact_type: str,
        payload: bytes,
    ) -> None:
        name = f"{artifact_type}{_ARTIFACT_SUFFIX[artifact_type]}"
        storage_ref = f"{directory}/{name}"
        await run_blocking(_write_bytes, self.root() / storage_ref, payload)
        session.add(
            PipelineArtifact(
                version_id=version.id,
                artifact_type=artifact_type,
                storage_ref=storage_ref,
                sha256=hashlib.sha256(payload).hexdigest(),
                size_bytes=len(payload),
            )
        )

    async def _require_version(
        self, session: AsyncSession, version_id: uuid.UUID
    ) -> PipelineVersion:
        version = await session.get(PipelineVersion, version_id)
        if version is None:
            raise ResourceNotFound("管道版本不存在")
        return version


@dataclass(frozen=True, slots=True)
class DesignView:
    """审批界面看到的内容；未冻结的版本三样都是空的，而不是造一份占位内容。"""

    version_id: uuid.UUID
    status: PipelineVersionStatus
    artifact_sha256: str | None
    plan: dict[str, Any] | None
    hocon: str | None


def _artifacts(candidate: GeneratedCandidate) -> dict[str, bytes]:
    """冻结的三份制品。

    HOCON 原样落盘（不做规范化重排）：审批人看到的字节就是将要交给引擎的字节。
    EtlPlan 与审查视图走规范 JSON，保证同一份候选的字节逐位相同 —— 摘要稳定靠的是这个。
    """
    plan = candidate.plan.model_dump(mode="json")
    return {
        ARTIFACT_HOCON: candidate.hocon.encode("utf-8"),
        ARTIFACT_ETL_PLAN: canonical_json(plan).encode("utf-8"),
        ARTIFACT_REVIEW_VIEW: canonical_json(review_view(candidate)).encode("utf-8"),
    }


def review_view(candidate: GeneratedCandidate) -> Mapping[str, Any]:
    """审查视图：把审批人要看的决策点从 plan 里摘出来。

    刻意只做**投影**，不引入 plan 之外的新事实 —— 多出来的信息一旦与 plan 不一致，
    审批绑定的就不再是同一份内容了（SOD-04）。
    """
    plan = candidate.plan
    return {
        "requirement_fields": {
            "source": plan.source.model_dump(mode="json"),
            "target": plan.target.model_dump(mode="json"),
        },
        "field_mappings": [mapping.model_dump(mode="json") for mapping in plan.mappings],
        "transforms": [step.model_dump(mode="json") for step in plan.transforms],
        "quality_rules": [rule.model_dump(mode="json") for rule in plan.quality_rules],
        "incremental": plan.incremental.model_dump(mode="json"),
        "dedup_keys": list(plan.dedup_keys),
        "budget": plan.budget.model_dump(mode="json"),
        "row_llm": plan.row_llm.model_dump(mode="json"),
    }


def _write_bytes(target: Path, data: bytes) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(data)
