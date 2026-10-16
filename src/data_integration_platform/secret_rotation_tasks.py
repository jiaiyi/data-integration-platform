"""Secret 轮换任务：验证与激活（T-15 / FR-19）。

分两个任务而不是一个，因为它们分处审批两侧：
`validate_secret_rotation` 在**审批之前**（验证不通过就进不了审批），
`activate_secret_rotation` 在**审批之后**。合成一个任务会让「未验证也能审批」有机可乘。
"""

import uuid
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from data_integration_platform.application.probing import ProbingService
from data_integration_platform.application.secret_governance import OperationOutcome
from data_integration_platform.application.secret_rotations import SecretRotationService
from data_integration_platform.celery_app import celery_app
from data_integration_platform.infrastructure.database import run_in_transaction
from data_integration_platform.infrastructure.probe_registry import ProbeRegistry
from data_integration_platform.infrastructure.secret_factory import create_secret_provider


@celery_app.task(name="data_integration_platform.secret_rotation_tasks.validate_secret_rotation")
def validate_secret_rotation(request_id: str) -> dict[str, Any]:
    """用暂存凭据做真实连通性验证；通过后才允许进入审批（FR-19）。"""

    async def work(session: AsyncSession) -> OperationOutcome:
        connectivity = ProbingService(
            registry=ProbeRegistry(), secret_provider=create_secret_provider(session)
        )
        return await SecretRotationService().validate(
            session, request_id=uuid.UUID(request_id), connectivity=connectivity
        )

    outcome: OperationOutcome = run_in_transaction(work)
    return {"request_id": request_id, "succeeded": outcome.succeeded, "reason": outcome.reason}


@celery_app.task(name="data_integration_platform.secret_rotation_tasks.activate_secret_rotation")
def activate_secret_rotation(request_id: str) -> dict[str, Any]:
    """激活已批准的轮换：切换连接引用、重算有效期、开启回退窗口。"""
    outcome: OperationOutcome = run_in_transaction(
        lambda session: SecretRotationService().activate(session, request_id=uuid.UUID(request_id))
    )
    return {"request_id": request_id, "succeeded": outcome.succeeded, "reason": outcome.reason}
