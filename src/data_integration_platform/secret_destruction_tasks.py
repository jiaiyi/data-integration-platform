"""Secret 销毁任务（T-15 / FR-21）。

执行前由服务层重新求证「待销毁的凭据当前没在用」——申请与执行之间状态可能已变。
"""

import uuid
from typing import Any

from data_integration_platform.application.secret_destructions import SecretDestructionService
from data_integration_platform.application.secret_governance import OperationOutcome
from data_integration_platform.celery_app import celery_app
from data_integration_platform.infrastructure.database import run_in_transaction


@celery_app.task(
    name="data_integration_platform.secret_destruction_tasks.execute_secret_destruction"
)
def execute_secret_destruction(request_id: str) -> dict[str, Any]:
    """执行已批准的销毁。"""
    outcome: OperationOutcome = run_in_transaction(
        lambda session: SecretDestructionService().execute(
            session, request_id=uuid.UUID(request_id)
        )
    )
    return {"request_id": request_id, "succeeded": outcome.succeeded, "reason": outcome.reason}
