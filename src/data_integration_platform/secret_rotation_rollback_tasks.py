"""Secret 轮换回退任务（T-15 / FR-20）。"""

import uuid
from typing import Any

from data_integration_platform.application.secret_governance import OperationOutcome
from data_integration_platform.application.secret_rotation_rollbacks import (
    SecretRotationRollbackService,
)
from data_integration_platform.celery_app import celery_app
from data_integration_platform.infrastructure.database import run_in_transaction


@celery_app.task(
    name="data_integration_platform.secret_rotation_rollback_tasks.execute_secret_rotation_rollback"
)
def execute_secret_rotation_rollback(request_id: str) -> dict[str, Any]:
    """执行已批准的回退：把连接切回旧凭据并还原其签发时间。"""
    outcome: OperationOutcome = run_in_transaction(
        lambda session: SecretRotationRollbackService().execute(
            session, request_id=uuid.UUID(request_id)
        )
    )
    return {"request_id": request_id, "succeeded": outcome.succeeded, "reason": outcome.reason}
