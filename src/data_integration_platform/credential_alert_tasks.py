"""凭据到期告警任务：扫描与投递（T-15 / FR-22）。

与凭据操作刻意相反，**投递可以安全重试**：接收端按 `delivery_id` 幂等，
重发同一条告警不会造成第二次业务效果。重试上限见 `MAX_DELIVERY_ATTEMPTS`。
"""

from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from data_integration_platform.application.credential_alerts import (
    MAX_DELIVERY_ATTEMPTS,
    CredentialAlertService,
)
from data_integration_platform.application.secret_governance import OperationOutcome
from data_integration_platform.celery_app import celery_app
from data_integration_platform.domain.enums import CredentialAlertDeliveryStatus
from data_integration_platform.infrastructure.database import run_in_transaction
from data_integration_platform.infrastructure.models import CredentialAlertDelivery


@celery_app.task(
    name="data_integration_platform.credential_alert_tasks.scan_credential_expiry_alerts"
)
def scan_credential_expiry_alerts() -> dict[str, Any]:
    """扫描到期与预警窗口，开/关告警，并为待外发告警创建投递记录。

    Beat 周期由 `credential_alert_scan_interval_seconds` 控制；未配置 Webhook 时
    告警照常在平台内留存，只是不产生外发投递。
    """
    return run_in_transaction(_scan_and_enqueue)


async def _scan_and_enqueue(session: AsyncSession) -> dict[str, Any]:
    service = CredentialAlertService()
    changed = await service.scan(session)
    created = await service.enqueue_deliveries(session)
    return {"alerts_changed": len(changed), "deliveries_created": len(created)}


@celery_app.task(name="data_integration_platform.credential_alert_tasks.deliver_credential_alerts")
def deliver_credential_alerts() -> dict[str, Any]:
    """投递所有待发与可重试的告警。"""
    return run_in_transaction(_deliver_pending)


async def _deliver_pending(session: AsyncSession) -> dict[str, Any]:
    pending = await session.scalars(
        select(CredentialAlertDelivery.delivery_id).where(
            CredentialAlertDelivery.status.in_(
                [
                    CredentialAlertDeliveryStatus.PENDING,
                    CredentialAlertDeliveryStatus.FAILED,
                ]
            ),
            CredentialAlertDelivery.attempts < MAX_DELIVERY_ATTEMPTS,
        )
    )
    service = CredentialAlertService()
    succeeded = 0
    attempted = 0
    for delivery_id in list(pending):
        attempted += 1
        if (await service.deliver(session, delivery_id=delivery_id)).succeeded:
            succeeded += 1
    return {"attempted": attempted, "succeeded": succeeded}


@celery_app.task(name="data_integration_platform.credential_alert_tasks.deliver_credential_alert")
def deliver_credential_alert(delivery_id: str) -> dict[str, Any]:
    """按投递 ID 重发单条告警；已成功的投递不会产生第二次外发。"""
    outcome: OperationOutcome = run_in_transaction(
        lambda session: CredentialAlertService().deliver(session, delivery_id=delivery_id)
    )
    return {"delivery_id": delivery_id, "succeeded": outcome.succeeded, "reason": outcome.reason}
