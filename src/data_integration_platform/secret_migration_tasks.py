"""Secret Provider 迁移任务（T-15 / FR-18）。

凭据操作**不做 Celery 自动重试**：迁移中途失败可能已经写入了新的存储位置，
自动重试会把「部分完成」放大成更难收拾的状态。失败一律落成 `FAILED` 事实，
由人决定重新申请还是回退。
"""

import uuid
from typing import Any

from data_integration_platform.application.secret_governance import OperationOutcome
from data_integration_platform.application.secret_migrations import SecretMigrationService
from data_integration_platform.celery_app import celery_app
from data_integration_platform.infrastructure.database import run_in_transaction


@celery_app.task(name="data_integration_platform.secret_migration_tasks.execute_secret_migration")
def execute_secret_migration(request_id: str) -> dict[str, Any]:
    """执行已批准的迁移：源 → 目标，保留源凭据。"""
    outcome: OperationOutcome = run_in_transaction(
        lambda session: SecretMigrationService().execute(session, request_id=uuid.UUID(request_id))
    )
    return {"request_id": request_id, "succeeded": outcome.succeeded, "reason": outcome.reason}
