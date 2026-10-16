"""凭据到期告警与 Webhook 投递（T-15 / FR-22）。

两件事共用本模块：
1. **到期扫描**：按「签发时间 + 最大年龄」判定期限，进入预警窗口开 `WARNING`、
   已过期开 `OVERDUE`；同等级在冷却窗口内不重复打扰，等级升级则关闭旧告警开新告警。
2. **Webhook 投递**：接收端按 `delivery_id` 幂等，因此这里**可以安全重试** ——
   与凭据操作刻意相反：凭据操作可能已经部分生效，自动重试只会放大问题。

外发内容只含**告警事实**（连接名、等级、期限、检测时间），不含任何凭据或引用。
"""

import hashlib
import hmac
import json
import uuid
from datetime import UTC, datetime

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from data_integration_platform.application.access import require_project_roles
from data_integration_platform.application.audit import record_audit_event
from data_integration_platform.application.connections import READABLE
from data_integration_platform.application.masking import scrub_secrets
from data_integration_platform.application.secret_governance import (
    OperationOutcome,
    classify_credential_expiry,
    cooldown_for,
    credential_due_at,
    is_in_cooldown,
)
from data_integration_platform.config import get_settings
from data_integration_platform.domain.enums import (
    AuditEventType,
    CredentialAlertDeliveryStatus,
    CredentialAlertLevel,
    CredentialAlertStatus,
)
from data_integration_platform.domain.errors import DomainError, ResourceNotFound
from data_integration_platform.infrastructure.egress import require_allowed_egress
from data_integration_platform.infrastructure.models import (
    CredentialAlertDelivery,
    CredentialExpiryAlert,
    DataConnection,
)

# 投递重试上限：接收端幂等，所以重试是安全的，但仍要有界，避免堆积无限重试。
MAX_DELIVERY_ATTEMPTS = 3
_LEVEL_ORDER = {CredentialAlertLevel.WARNING: 1, CredentialAlertLevel.OVERDUE: 2}


async def resolve_open_alerts(session: AsyncSession, *, connection_id: uuid.UUID, note: str) -> int:
    """关闭该连接上所有未解决的告警；返回关闭条数。

    凭据换新或期限不再落入窗口时调用 —— 否则告警会一直重复提醒已解决的事。
    """
    alerts = await session.scalars(
        select(CredentialExpiryAlert).where(
            CredentialExpiryAlert.connection_id == connection_id,
            CredentialExpiryAlert.status == CredentialAlertStatus.OPEN,
        )
    )
    now = datetime.now(UTC)
    resolved = 0
    for alert in alerts:
        alert.status = CredentialAlertStatus.RESOLVED
        alert.resolved_at = now
        alert.resolution_note = note[:2000]
        resolved += 1
    return resolved


class CredentialAlertService:
    async def scan(self, session: AsyncSession) -> list[CredentialExpiryAlert]:
        """扫描所有启用轮换策略的连接，按需开告警或关闭已不成立的告警。"""
        settings = get_settings()
        now = datetime.now(UTC)
        webhook_configured = settings.credential_alert_webhook_url is not None
        connections = await session.scalars(
            select(DataConnection).where(DataConnection.credential_rotation_enabled.is_(True))
        )
        changed: list[CredentialExpiryAlert] = []
        for connection in connections:
            alert = await self._scan_connection(
                session,
                connection=connection,
                now=now,
                webhook_configured=webhook_configured,
                warning_cooldown_hours=settings.credential_alert_warning_cooldown_hours,
                overdue_cooldown_hours=settings.credential_alert_overdue_cooldown_hours,
            )
            if alert is not None:
                changed.append(alert)
        return changed

    async def _scan_connection(
        self,
        session: AsyncSession,
        *,
        connection: DataConnection,
        now: datetime,
        webhook_configured: bool,
        warning_cooldown_hours: int,
        overdue_cooldown_hours: int,
    ) -> CredentialExpiryAlert | None:
        due_at = credential_due_at(
            issued_at=connection.credential_issued_at,
            max_age_days=connection.credential_max_age_days,
        )
        level = classify_credential_expiry(
            due_at=due_at, warning_days=connection.credential_warning_days, now=now
        )

        open_alerts = list(
            await session.scalars(
                select(CredentialExpiryAlert).where(
                    CredentialExpiryAlert.connection_id == connection.id,
                    CredentialExpiryAlert.status == CredentialAlertStatus.OPEN,
                )
            )
        )
        for alert in open_alerts:
            alert.last_scanned_at = now

        if level is None:
            # 期限不成立（未启用、缺字段）或已不在窗口内：关掉仍开着的告警。
            for alert in open_alerts:
                alert.status = CredentialAlertStatus.RESOLVED
                alert.resolved_at = now
                alert.resolution_note = "期限条件已不满足或凭据已更新"
            return None

        current = max(open_alerts, key=lambda item: _LEVEL_ORDER.get(item.level, 0), default=None)
        cooldown = cooldown_for(
            level, warning_hours=warning_cooldown_hours, overdue_hours=overdue_cooldown_hours
        )
        if current is not None and current.level is level:
            # 同等级告警已存在：冷却期内不再打扰，冷却结束后允许再通知一轮。
            if is_in_cooldown(last_opened_at=current.opened_at, cooldown=cooldown, now=now):
                return None
            current.cooldown_until = now + cooldown
            current.scan_count += 1
            return current

        # 等级升级（WARNING → OVERDUE）或首次开告警：关闭旧等级，开新告警。
        for alert in open_alerts:
            alert.status = CredentialAlertStatus.RESOLVED
            alert.resolved_at = now
            alert.resolution_note = f"等级升级为 {level.value}，旧告警关闭"

        alert = CredentialExpiryAlert(
            connection_id=connection.id,
            secret_ref=connection.secret_ref,
            level=level,
            status=CredentialAlertStatus.OPEN,
            credential_issued_at=connection.credential_issued_at,
            credential_max_age_days=connection.credential_max_age_days,
            warning_days=connection.credential_warning_days,
            due_at=due_at,
            opened_at=now,
            last_scanned_at=now,
            scan_count=1,
            cooldown_until=now + cooldown,
            delivery_required=webhook_configured,
            alert_context={"connection_name": connection.name, "level": level.value},
        )
        session.add(alert)
        await session.flush()
        record_audit_event(
            session,
            event_type=AuditEventType.CREDENTIAL_ALERT_OPENED,
            target_type="credential_expiry_alert",
            project_id=connection.project_id,
            target_id=str(alert.id),
            change_summary={
                "level": level.value,
                "connection_name": connection.name,
                "due_at": due_at.isoformat() if due_at else None,
            },
        )
        return alert

    async def list_alerts(
        self,
        session: AsyncSession,
        *,
        project_id: uuid.UUID,
        actor_id: uuid.UUID,
        status: CredentialAlertStatus | None = None,
    ) -> list[CredentialExpiryAlert]:
        await require_project_roles(
            session, project_id=project_id, actor_id=actor_id, allowed=READABLE
        )
        statement = (
            select(CredentialExpiryAlert)
            .join(DataConnection, DataConnection.id == CredentialExpiryAlert.connection_id)
            .where(DataConnection.project_id == project_id)
            .order_by(CredentialExpiryAlert.opened_at.desc())
        )
        if status is not None:
            statement = statement.where(CredentialExpiryAlert.status == status)
        return list(await session.scalars(statement))

    async def enqueue_deliveries(self, session: AsyncSession) -> list[CredentialAlertDelivery]:
        """为待外发的告警创建投递记录。

        `delivery_id` 由「告警 + 通知轮次」决定且唯一，因此重复扫描不会产生第二次外发 ——
        接收端也按同一 ID 幂等。
        """
        settings = get_settings()
        if settings.credential_alert_webhook_url is None:
            return []
        target_url = str(settings.credential_alert_webhook_url)
        require_allowed_egress(target_url, settings.allowed_webhook_hosts())

        alerts = await session.scalars(
            select(CredentialExpiryAlert).where(
                CredentialExpiryAlert.status == CredentialAlertStatus.OPEN,
                CredentialExpiryAlert.delivery_required.is_(True),
            )
        )
        created: list[CredentialAlertDelivery] = []
        for alert in alerts:
            delivery_id = f"{alert.id}:{alert.scan_count}"
            existing = await session.scalar(
                select(CredentialAlertDelivery.id).where(
                    CredentialAlertDelivery.delivery_id == delivery_id
                )
            )
            if existing is not None:
                continue
            delivery = CredentialAlertDelivery(
                alert_id=alert.id,
                delivery_id=delivery_id,
                status=CredentialAlertDeliveryStatus.PENDING,
                target_url=target_url,
            )
            session.add(delivery)
            created.append(delivery)
        await session.flush()
        return created

    async def deliver(self, session: AsyncSession, *, delivery_id: str) -> OperationOutcome:
        """投递一条告警；已成功的投递直接返回，不再产生第二次外发。"""
        delivery = await session.scalar(
            select(CredentialAlertDelivery).where(
                CredentialAlertDelivery.delivery_id == delivery_id
            )
        )
        if delivery is None:
            raise ResourceNotFound("告警投递记录不存在")
        if delivery.status is CredentialAlertDeliveryStatus.SUCCEEDED:
            return OperationOutcome.success()
        if delivery.attempts >= MAX_DELIVERY_ATTEMPTS:
            return OperationOutcome.failure("投递重试已达上限")

        alert = await session.get(CredentialExpiryAlert, delivery.alert_id)
        connection = await session.get(DataConnection, alert.connection_id) if alert else None
        if alert is None or connection is None:
            delivery.status = CredentialAlertDeliveryStatus.SKIPPED
            return OperationOutcome.failure("告警或连接已不存在，跳过投递")

        settings = get_settings()
        if settings.credential_alert_webhook_url is None:
            delivery.status = CredentialAlertDeliveryStatus.SKIPPED
            return OperationOutcome.failure("未配置 Webhook 地址，跳过投递")

        body = _alert_payload(delivery_id=delivery_id, alert=alert, connection=connection)
        raw = json.dumps(body, ensure_ascii=False, sort_keys=True).encode("utf-8")
        headers = {"Content-Type": "application/json"}
        secret = settings.credential_alert_webhook_signing_secret
        if secret is not None:
            headers["X-DIP-Signature"] = (
                "sha256="
                + hmac.new(
                    secret.get_secret_value().encode("utf-8"), raw, hashlib.sha256
                ).hexdigest()
            )

        delivery.status = CredentialAlertDeliveryStatus.SENDING
        delivery.attempts += 1
        await session.flush()
        try:
            async with httpx.AsyncClient(
                timeout=settings.credential_alert_webhook_timeout_seconds,
                follow_redirects=False,
                # 与其它平台外发一致：不走系统代理，避免把告警内容交给未知中间人。
                trust_env=False,
            ) as client:
                response = await client.post(
                    str(settings.credential_alert_webhook_url), content=raw, headers=headers
                )
        except httpx.HTTPError as exc:
            delivery.status = CredentialAlertDeliveryStatus.FAILED
            delivery.last_error = scrub_secrets(f"{type(exc).__name__}: {exc}")[:2000]
            await session.flush()
            return OperationOutcome.failure(delivery.last_error)

        if response.status_code >= 400:
            delivery.status = CredentialAlertDeliveryStatus.FAILED
            delivery.last_error = f"接收端返回 {response.status_code}"
            await session.flush()
            return OperationOutcome.failure(delivery.last_error)

        delivery.status = CredentialAlertDeliveryStatus.SUCCEEDED
        delivery.delivered_at = datetime.now(UTC)
        delivery.last_error = None
        await session.flush()
        record_audit_event(
            session,
            event_type=AuditEventType.CREDENTIAL_ALERT_DELIVERED,
            target_type="credential_alert_delivery",
            project_id=connection.project_id,
            target_id=delivery_id,
            change_summary={"level": alert.level.value, "attempts": delivery.attempts},
        )
        return OperationOutcome.success()

    async def list_deliveries(
        self,
        session: AsyncSession,
        *,
        project_id: uuid.UUID,
        actor_id: uuid.UUID,
    ) -> list[CredentialAlertDelivery]:
        await require_project_roles(
            session, project_id=project_id, actor_id=actor_id, allowed=READABLE
        )
        statement = (
            select(CredentialAlertDelivery)
            .join(
                CredentialExpiryAlert, CredentialExpiryAlert.id == CredentialAlertDelivery.alert_id
            )
            .join(DataConnection, DataConnection.id == CredentialExpiryAlert.connection_id)
            .where(DataConnection.project_id == project_id)
            .order_by(CredentialAlertDelivery.created_at.desc())
        )
        return list(await session.scalars(statement))


def _alert_payload(
    *, delivery_id: str, alert: CredentialExpiryAlert, connection: DataConnection
) -> dict[str, object]:
    """外发载荷：只含告警事实，**不含凭据、不含 secret_ref**。"""
    return {
        "delivery_id": delivery_id,
        "alert_id": str(alert.id),
        "level": alert.level.value,
        "connection_id": str(connection.id),
        "connection_name": connection.name,
        "credential_issued_at": (
            alert.credential_issued_at.isoformat() if alert.credential_issued_at else None
        ),
        "credential_max_age_days": alert.credential_max_age_days,
        "due_at": alert.due_at.isoformat() if alert.due_at else None,
        "detected_at": alert.opened_at.isoformat(),
    }


def require_webhook_configured() -> None:
    """扫描触发前的快速失败：没配 Webhook 就没必要跑投递。"""
    if get_settings().credential_alert_webhook_url is None:
        raise DomainError("未配置凭据告警 Webhook 地址")


def signature_header(raw_body: bytes, *, secret: str) -> str:
    """供接收端实现与契约测试复用：对原始请求体做 HMAC-SHA256。"""
    return "sha256=" + hmac.new(secret.encode("utf-8"), raw_body, hashlib.sha256).hexdigest()
