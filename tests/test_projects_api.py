"""身份、项目与成员治理的端到端行为（SPEC T-08 / T-09）。

覆盖 PRD §9.1 的验收条件：一次性初始化、成员增删、职责槽互斥、
跨项目拒绝与审计只读。
"""

from typing import Any

from httpx import AsyncClient

from data_integration_platform.domain.enums import ProjectRole
from data_integration_platform.domain.harness import HarnessRole
from tests.conftest import TEST_BOOTSTRAP_TOKEN

BOOTSTRAP_PAYLOAD = {
    "admin_subject": "zhangsan",
    "admin_display_name": "张三",
    "project_name": "电商数据平台",
}


def headers(actor_id: str | None = None, **extra: str) -> dict[str, str]:
    values = dict(extra)
    if actor_id is not None:
        values["X-Actor-Id"] = actor_id
    return values


async def bootstrap(client: AsyncClient) -> dict[str, Any]:
    response = await client.post(
        "/api/v1/bootstrap",
        json=BOOTSTRAP_PAYLOAD,
        headers=headers(**{"X-Bootstrap-Token": TEST_BOOTSTRAP_TOKEN}),
    )
    assert response.status_code == 201, response.text
    return dict(response.json())


# ------------------------------------------------------------------ 系统
async def test_health_reports_identity_boundary(client: AsyncClient) -> None:
    response = await client.get("/health")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["auth_mode"] == "dev-header"
    assert body["environment"] == "local"
    # 策略决策要能对回策略包版本（FR-49），控制台侧栏也展示它。
    assert body["policy_bundle_version"] == "harness-v1-20261016"


# ------------------------------------------------------------------ 一次性初始化
async def test_bootstrap_requires_token(client: AsyncClient) -> None:
    response = await client.post("/api/v1/bootstrap", json=BOOTSTRAP_PAYLOAD)
    assert response.status_code == 401


async def test_bootstrap_creates_first_admin_and_project(client: AsyncClient) -> None:
    body = await bootstrap(client)
    assert body["user"]["subject"] == "zhangsan"
    assert body["project"]["name"] == "电商数据平台"
    # 首位管理员只获得项目成员角色，不会被隐式授予任何 Harness 职责槽。
    grants = await client.get(
        f"/api/v1/projects/{body['project']['id']}/role-grants",
        headers=headers(body["user"]["id"]),
    )
    assert grants.status_code == 200
    assert grants.json() == []


async def test_bootstrap_is_one_time_only(client: AsyncClient) -> None:
    await bootstrap(client)
    response = await client.post(
        "/api/v1/bootstrap",
        json=BOOTSTRAP_PAYLOAD,
        headers=headers(**{"X-Bootstrap-Token": TEST_BOOTSTRAP_TOKEN}),
    )
    assert response.status_code == 400
    assert "不可重复调用" in response.json()["detail"]


# ------------------------------------------------------------------ 项目与成员
async def test_project_member_lifecycle(client: AsyncClient) -> None:
    body = await bootstrap(client)
    admin = body["user"]["id"]
    project_id = body["project"]["id"]

    created = await client.post(
        "/api/v1/projects", json={"name": "日志平台"}, headers=headers(admin)
    )
    assert created.status_code == 201
    assert created.json()["name"] == "日志平台"

    added = await client.put(
        f"/api/v1/projects/{project_id}/members",
        json={"subject": "lisi", "display_name": "李四", "role": ProjectRole.DATA_ENGINEER},
        headers=headers(admin),
    )
    assert added.status_code == 200
    member_id = added.json()["user_id"]

    members = await client.get(f"/api/v1/projects/{project_id}/members", headers=headers(admin))
    assert members.status_code == 200
    assert {item["user_id"] for item in members.json()} == {admin, member_id}

    removed = await client.delete(
        f"/api/v1/projects/{project_id}/members/{member_id}", headers=headers(admin)
    )
    assert removed.status_code == 204

    after = await client.get(f"/api/v1/projects/{project_id}/members", headers=headers(admin))
    assert {item["user_id"] for item in after.json()} == {admin}


async def test_non_admin_member_cannot_manage_members(client: AsyncClient) -> None:
    body = await bootstrap(client)
    admin, project_id = body["user"]["id"], body["project"]["id"]

    member = await client.put(
        f"/api/v1/projects/{project_id}/members",
        json={"subject": "lisi", "display_name": "李四", "role": ProjectRole.DATA_ENGINEER},
        headers=headers(admin),
    )
    member_id = member.json()["user_id"]

    denied = await client.put(
        f"/api/v1/projects/{project_id}/members",
        json={"subject": "wangwu", "display_name": "王五", "role": ProjectRole.VIEWER},
        headers=headers(member_id),
    )
    assert denied.status_code == 403


async def test_cross_project_access_is_denied(client: AsyncClient) -> None:
    """FR-02：非本项目成员访问该项目资源一律拒绝。"""
    body = await bootstrap(client)
    project_id = body["project"]["id"]
    outsider = "99999999-9999-9999-9999-999999999999"

    denied = await client.get(f"/api/v1/projects/{project_id}/members", headers=headers(outsider))
    assert denied.status_code == 403

    visible = await client.get("/api/v1/projects", headers=headers(outsider))
    assert visible.status_code == 200
    assert visible.json() == []


# ------------------------------------------------------------------ 职责槽
async def test_harness_role_grant_and_revoke(client: AsyncClient) -> None:
    body = await bootstrap(client)
    admin, project_id = body["user"]["id"], body["project"]["id"]
    member = await client.put(
        f"/api/v1/projects/{project_id}/members",
        json={"subject": "lisi", "display_name": "李四", "role": ProjectRole.APPROVER},
        headers=headers(admin),
    )
    member_id = member.json()["user_id"]

    granted = await client.post(
        f"/api/v1/projects/{project_id}/role-grants",
        json={"user_id": member_id, "role": HarnessRole.DATA_APPROVER},
        headers=headers(admin),
    )
    assert granted.status_code == 201

    revoked = await client.delete(
        f"/api/v1/projects/{project_id}/role-grants/{member_id}",
        params={"role": HarnessRole.DATA_APPROVER.value},
        headers=headers(admin),
    )
    assert revoked.status_code == 204

    remaining = await client.get(
        f"/api/v1/projects/{project_id}/role-grants", headers=headers(admin)
    )
    assert remaining.json() == []


async def test_one_person_cannot_hold_both_approval_slots(client: AsyncClient) -> None:
    """FR-04 / SOD-02：数据审批槽与安全审批槽不得由同一人担任。"""
    body = await bootstrap(client)
    admin, project_id = body["user"]["id"], body["project"]["id"]
    member = await client.put(
        f"/api/v1/projects/{project_id}/members",
        json={"subject": "wanger", "display_name": "王二", "role": ProjectRole.APPROVER},
        headers=headers(admin),
    )
    member_id = member.json()["user_id"]

    first = await client.post(
        f"/api/v1/projects/{project_id}/role-grants",
        json={"user_id": member_id, "role": HarnessRole.DATA_APPROVER},
        headers=headers(admin),
    )
    assert first.status_code == 201

    second = await client.post(
        f"/api/v1/projects/{project_id}/role-grants",
        json={"user_id": member_id, "role": HarnessRole.SECURITY_APPROVER},
        headers=headers(admin),
    )
    assert second.status_code == 403
    assert "SOD-02" in second.json()["detail"]

    other = await client.put(
        f"/api/v1/projects/{project_id}/members",
        json={"subject": "zhangsi", "display_name": "张四", "role": ProjectRole.APPROVER},
        headers=headers(admin),
    )
    allowed = await client.post(
        f"/api/v1/projects/{project_id}/role-grants",
        json={"user_id": other.json()["user_id"], "role": HarnessRole.SECURITY_APPROVER},
        headers=headers(admin),
    )
    assert allowed.status_code == 201


async def test_removing_member_revokes_harness_roles(client: AsyncClient) -> None:
    """离开项目即失去审批资格 —— 否则职责槽会变成可留存的隐性授权。"""
    body = await bootstrap(client)
    admin, project_id = body["user"]["id"], body["project"]["id"]
    member = await client.put(
        f"/api/v1/projects/{project_id}/members",
        json={"subject": "lisi", "display_name": "李四", "role": ProjectRole.APPROVER},
        headers=headers(admin),
    )
    member_id = member.json()["user_id"]
    await client.post(
        f"/api/v1/projects/{project_id}/role-grants",
        json={"user_id": member_id, "role": HarnessRole.DATA_APPROVER},
        headers=headers(admin),
    )

    await client.delete(
        f"/api/v1/projects/{project_id}/members/{member_id}", headers=headers(admin)
    )
    remaining = await client.get(
        f"/api/v1/projects/{project_id}/role-grants", headers=headers(admin)
    )
    assert remaining.json() == []


async def test_auditor_cannot_perform_writes(client: AsyncClient) -> None:
    """SOD-05：审计只读，持有审计职责的主体不得执行任何写操作。"""
    body = await bootstrap(client)
    admin, project_id = body["user"]["id"], body["project"]["id"]
    auditor = await client.put(
        f"/api/v1/projects/{project_id}/members",
        json={"subject": "auditor", "display_name": "审计员", "role": ProjectRole.ADMIN},
        headers=headers(admin),
    )
    auditor_id = auditor.json()["user_id"]
    granted = await client.post(
        f"/api/v1/projects/{project_id}/role-grants",
        json={"user_id": auditor_id, "role": HarnessRole.AUDITOR},
        headers=headers(admin),
    )
    assert granted.status_code == 201

    denied = await client.put(
        f"/api/v1/projects/{project_id}/members",
        json={"subject": "wangwu", "display_name": "王五", "role": ProjectRole.VIEWER},
        headers=headers(auditor_id),
    )
    assert denied.status_code == 403
    assert "SOD-05" in denied.json()["detail"]

    # 只读仍然放行：审计需要能查阅事实，只是不能改。
    readable = await client.get(
        f"/api/v1/projects/{project_id}/members", headers=headers(auditor_id)
    )
    assert readable.status_code == 200


async def test_granting_role_to_non_member_is_rejected(client: AsyncClient) -> None:
    body = await bootstrap(client)
    admin, project_id = body["user"]["id"], body["project"]["id"]

    denied = await client.post(
        f"/api/v1/projects/{project_id}/role-grants",
        json={"user_id": "99999999-9999-9999-9999-999999999999", "role": HarnessRole.OPERATOR},
        headers=headers(admin),
    )
    assert denied.status_code == 404


async def test_actor_header_is_required(client: AsyncClient) -> None:
    """缺少调用者身份头时请求在边界即被拒，不进入任何业务逻辑。"""
    response = await client.get("/api/v1/projects")
    assert response.status_code == 422
