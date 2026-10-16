"""运维盘点与扫描接口（SPEC §6.1 / FR-22）。

本里程碑先落「凭据治理盘点」这一类：到期告警、告警投递、扫描触发与单条重发。
Worker 健康、回滚制品盘点等在各自里程碑补齐（T-39 / T-45）。
"""

import uuid

from fastapi import APIRouter, Depends, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from data_integration_platform.api.dependencies import ActorId
from data_integration_platform.api.schemas import (
    CredentialAlertDeliveryResponse,
    CredentialAlertResponse,
)
from data_integration_platform.application.credential_alerts import CredentialAlertService
from data_integration_platform.celery_app import dispatch
from data_integration_platform.credential_alert_tasks import (
    deliver_credential_alert,
    scan_credential_expiry_alerts,
)
from data_integration_platform.domain.enums import CredentialAlertStatus
from data_integration_platform.infrastructure.database import get_session

router = APIRouter(tags=["operations"])


@router.get(
    "/projects/{project_id}/credential-alerts", response_model=list[CredentialAlertResponse]
)
async def list_credential_alerts(
    project_id: uuid.UUID,
    actor: ActorId,
    alert_status: CredentialAlertStatus | None = Query(default=None, alias="status"),
    session: AsyncSession = Depends(get_session),
) -> list[CredentialAlertResponse]:
    """列出项目内的凭据到期告警；默认含已解决的历史记录。"""
    alerts = await CredentialAlertService().list_alerts(
        session, project_id=project_id, actor_id=actor, status=alert_status
    )
    return [CredentialAlertResponse.model_validate(item) for item in alerts]


@router.get(
    "/projects/{project_id}/credential-alert-deliveries",
    response_model=list[CredentialAlertDeliveryResponse],
)
async def list_credential_alert_deliveries(
    project_id: uuid.UUID,
    actor: ActorId,
    session: AsyncSession = Depends(get_session),
) -> list[CredentialAlertDeliveryResponse]:
    """列出告警投递记录；`delivery_id` 即接收端用于幂等的键。"""
    deliveries = await CredentialAlertService().list_deliveries(
        session, project_id=project_id, actor_id=actor
    )
    return [CredentialAlertDeliveryResponse.model_validate(item) for item in deliveries]


@router.post(
    "/credential-alerts/scan",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=None,
)
async def trigger_credential_alert_scan() -> None:
    """手动触发一次到期扫描；与 Beat 周期任务走同一入口。"""
    await dispatch(scan_credential_expiry_alerts.delay)


@router.post(
    "/credential-alert-deliveries/{delivery_id}/retry",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=None,
)
async def retry_credential_alert_delivery(delivery_id: str) -> None:
    """重发单条告警；已成功投递的记录不会产生第二次外发。"""
    await dispatch(deliver_credential_alert.delay, delivery_id)
