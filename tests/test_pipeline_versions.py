"""不可变版本与制品冻结（T-22 / FR-36、FR-39）。

要守的证据是两条：**摘要稳定**（同一份候选重复冻结得同一个值）与
**父版本不变**（修订创建 V+1，旧版本的内容与摘要原封不动）。
此外还守住「冻结只在门禁之后」「冻结不可重入」这两条不变量 ——
它们一旦松掉，「审批人看到的就是将要执行的内容」就只是句口号。
"""

import hashlib
import uuid
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from data_integration_platform.application.generation import GeneratedCandidate
from data_integration_platform.application.versions import (
    ARTIFACT_ETL_PLAN,
    ARTIFACT_HOCON,
    ARTIFACT_REVIEW_VIEW,
    PipelineVersionService,
    review_view,
)
from data_integration_platform.domain.enums import PipelineVersionStatus, ProjectRole
from data_integration_platform.domain.errors import (
    DomainError,
    InvalidStateTransition,
    PermissionDenied,
)
from data_integration_platform.domain.harness import canonical_json
from data_integration_platform.infrastructure.models import (
    PipelineVersion,
    ProjectMembership,
    User,
)
from tests.conftest import ProjectContext

QUALITY_CODE = "E_POSITIVE"

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


def candidate(*, hocon: str = HOCON, table: str = "orders_wide") -> GeneratedCandidate:
    plan = {**PLAN, "target": {**PLAN["target"], "table_name": table}}
    return GeneratedCandidate.model_validate({"plan": plan, "hocon": hocon})


async def ensure_execution_endpoints(session: AsyncSession, project: ProjectContext) -> None:
    """给 `candidate()` 的计划建好能被服务端解析出的源/目标连接与 Profile。

    Prepare 阶段要按计划里的库表**查实**连接（执行阶段靠它取凭据），
    因此计划里写的那两张表必须在项目内各有一条就绪的 Profile 与连接。
    """
    from data_integration_platform.domain.enums import (
        ConnectionStatus,
        ConnectorType,
        DataEndpointRole,
        DataProfileStatus,
    )
    from data_integration_platform.infrastructure.models import DataConnection, DataProfile

    for connector, role, schema_name, table_name in (
        (ConnectorType.MYSQL, DataEndpointRole.SOURCE, "source_db", "orders"),
        (ConnectorType.DORIS, DataEndpointRole.SINK, "dw", "orders_wide"),
    ):
        connection = DataConnection(
            project_id=project.project_id,
            name=f"{connector.value.lower()}-{uuid.uuid4().hex[:6]}",
            connector_type=connector,
            endpoint_role=role,
            status=ConnectionStatus.READY,
            host="127.0.0.1",
            port=13306,
            database_name=schema_name,
            options={},
            created_by=project.engineer_id,
        )
        session.add(connection)
        await session.flush()
        session.add(
            DataProfile(
                connection_id=connection.id,
                schema_name=schema_name,
                table_name=table_name,
                endpoint_role=role,
                status=DataProfileStatus.READY,
                columns=[{"name": "id", "type": "bigint"}],
                primary_keys=["id"],
            )
        )
    await session.flush()


@pytest.fixture
def service(tmp_path: Path) -> PipelineVersionService:
    return PipelineVersionService(root=tmp_path / "artifacts")


async def make_version(
    service: PipelineVersionService,
    session: AsyncSession,
    project: ProjectContext,
    *,
    requirement: str = "把订单同步到宽表",
) -> PipelineVersion:
    return await service.create(
        session,
        project_id=project.project_id,
        actor_id=project.engineer_id,
        name=f"orders-sync-{uuid.uuid4().hex[:6]}",
        requirement=requirement,
    )


async def generated(
    service: PipelineVersionService, session: AsyncSession, project: ProjectContext
) -> PipelineVersion:
    """造一个停在 GENERATED 的版本 —— 冻结的前置状态。"""
    version = await make_version(service, session, project)
    version.status = PipelineVersionStatus.GENERATED
    await session.flush()
    return version


async def freeze(
    service: PipelineVersionService,
    session: AsyncSession,
    project: ProjectContext,
    version: PipelineVersion,
    *,
    candidate_: GeneratedCandidate | None = None,
) -> PipelineVersion:
    return await service.freeze(
        session,
        version_id=version.id,
        actor_id=project.engineer_id,
        candidate=candidate_ or candidate(),
    )


# ------------------------------------------------------------------ 冻结
async def test_freeze_writes_three_immutable_artifacts(
    service: PipelineVersionService, session: AsyncSession, project: ProjectContext
) -> None:
    version = await generated(service, session, project)
    frozen = await freeze(service, session, project, version)

    assert frozen.status is PipelineVersionStatus.VALIDATED
    artifacts = await service.artifacts_of(session, version_id=version.id)
    assert set(artifacts) == {ARTIFACT_HOCON, ARTIFACT_ETL_PLAN, ARTIFACT_REVIEW_VIEW}
    for artifact in artifacts.values():
        stored = await service.read_artifact(artifact)
        assert len(stored) == artifact.size_bytes
        assert hashlib.sha256(stored).hexdigest() == artifact.sha256
    # HOCON 必须逐字节原样落盘：审批人看到的字节就是交给引擎的字节。
    assert (await service.read_artifact(artifacts[ARTIFACT_HOCON])).decode() == HOCON
    # 版本摘要也落到版本行上，审批绑定的是这个值（SOD-04）。
    assert frozen.artifact_sha256 is not None
    assert frozen.plan == candidate().plan.model_dump(mode="json")


async def test_freeze_digest_is_stable_for_the_same_candidate(
    service: PipelineVersionService, session: AsyncSession, project: ProjectContext
) -> None:
    """摘要稳定：换一个版本、换一次冻结，同一份候选仍然是同一个摘要。"""
    first = await generated(service, session, project)
    second = await generated(service, session, project)

    frozen_first = await freeze(service, session, project, first)
    frozen_second = await freeze(service, session, project, second)

    assert frozen_first.artifact_sha256 == frozen_second.artifact_sha256


async def test_freeze_digest_changes_when_content_changes(
    service: PipelineVersionService, session: AsyncSession, project: ProjectContext
) -> None:
    """摘要绑内容：改了 HOCON 就得换摘要，否则审批会绑到旧内容上。"""
    first = await generated(service, session, project)
    second = await generated(service, session, project)

    frozen_first = await freeze(service, session, project, first)
    frozen_second = await freeze(
        service,
        session,
        project,
        second,
        candidate_=candidate(hocon=HOCON.replace("BATCH", "STREAMING")),
    )

    assert frozen_first.artifact_sha256 != frozen_second.artifact_sha256


async def test_review_view_is_a_pure_projection_of_the_plan() -> None:
    """审查视图只做投影：投影两次逐位相同，摘要才覆盖得到它。"""
    assert canonical_json(review_view(candidate())) == canonical_json(review_view(candidate()))

    view: Mapping[str, Any] = review_view(candidate())
    assert view["dedup_keys"] == ["order_id"]
    assert view["incremental"]["mode"] == "BATCH"
    assert [mapping["target_column"] for mapping in view["field_mappings"]] == [
        "order_id",
        "amount",
    ]


async def test_freeze_is_not_reentrant(
    service: PipelineVersionService, session: AsyncSession, project: ProjectContext
) -> None:
    """已冻结的版本再冻结必须被拒 —— 覆盖摘要就等于换了审批对象（US-09）。"""
    version = await generated(service, session, project)
    await freeze(service, session, project, version)

    with pytest.raises(DomainError, match="已冻结"):
        await freeze(service, session, project, version, candidate_=candidate(table="other_table"))


async def test_freeze_requires_the_generated_state(
    service: PipelineVersionService, session: AsyncSession, project: ProjectContext
) -> None:
    """DRAFT 直接冻结是跳步：生成没跑完就冻结等于把空壳当成方案。"""
    version = await make_version(service, session, project)

    with pytest.raises(InvalidStateTransition):
        await freeze(service, session, project, version)


# ------------------------------------------------------------------ 修订
async def test_revision_creates_next_version_and_leaves_the_parent_untouched(
    service: PipelineVersionService, session: AsyncSession, project: ProjectContext
) -> None:
    """FR-39：需求变更创建 V+1，旧版本的内容、摘要与状态都不动。"""
    parent = await generated(service, session, project)
    frozen = await freeze(service, session, project, parent)
    parent_digest = frozen.artifact_sha256
    parent_artifacts = await service.artifacts_of(session, version_id=parent.id)

    revision = await service.revise(
        session,
        version_id=parent.id,
        actor_id=project.engineer_id,
        requirement="金额上限改成 100 万，另外加一列渠道编码",
    )
    await session.refresh(frozen)

    assert revision.version_number == frozen.version_number + 1
    assert revision.parent_version_id == frozen.id
    assert revision.status is PipelineVersionStatus.DRAFT
    assert revision.artifact_sha256 is None
    assert revision.requirement.startswith("金额上限改成 100 万")
    # 父版本：内容与摘要不变，制品还在，状态也没被改动。
    assert frozen.artifact_sha256 == parent_digest
    assert frozen.status is PipelineVersionStatus.VALIDATED
    assert await service.artifacts_of(session, version_id=parent.id) == parent_artifacts


async def test_revision_chains_without_rewriting_earlier_versions(
    service: PipelineVersionService, session: AsyncSession, project: ProjectContext
) -> None:
    first = await make_version(service, session, project, requirement="第一版需求")
    second = await service.revise(
        session, version_id=first.id, actor_id=project.engineer_id, requirement="第二版需求"
    )
    third = await service.revise(
        session, version_id=second.id, actor_id=project.engineer_id, requirement="第三版需求"
    )

    assert [first.version_number, second.version_number, third.version_number] == [1, 2, 3]
    assert (second.parent_version_id, third.parent_version_id) == (first.id, second.id)
    # 版本只增不改：每一版的需求原文都各自留着。
    assert [first.requirement, second.requirement, third.requirement] == [
        "第一版需求",
        "第二版需求",
        "第三版需求",
    ]


# ------------------------------------------------------------------ 授权
async def test_viewer_cannot_freeze(
    service: PipelineVersionService, session: AsyncSession, project: ProjectContext
) -> None:
    version = await generated(service, session, project)
    viewer = User(subject=f"viewer-{uuid.uuid4().hex[:8]}", display_name="王五")
    session.add(viewer)
    await session.flush()
    session.add(
        ProjectMembership(project_id=project.project_id, user_id=viewer.id, role=ProjectRole.VIEWER)
    )
    await session.flush()

    with pytest.raises(PermissionDenied):
        await service.freeze(
            session, version_id=version.id, actor_id=viewer.id, candidate=candidate()
        )
