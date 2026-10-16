"""审计事件写入（PRD FR-100）。

审计记录**变化而非秘密**：只保存主体、事件类型、目标对象与变化摘要，
不保存凭据明文、原始业务行或完整 Prompt。本模块只往会话中追加对象，事务边界由调用方持有。
"""

import uuid
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from data_integration_platform.domain.enums import AuditEventType
from data_integration_platform.infrastructure.models import AuditEvent


def record_audit_event(
    session: AsyncSession,
    *,
    event_type: AuditEventType,
    target_type: str,
    project_id: uuid.UUID | None = None,
    actor_id: uuid.UUID | None = None,
    target_id: str | None = None,
    change_summary: dict[str, Any] | None = None,
    trace_id: str | None = None,
) -> AuditEvent:
    """构造并挂载一条审计事件，使其与调用方的事务一并提交或回滚。"""
    event = AuditEvent(
        event_type=event_type,
        target_type=target_type,
        project_id=project_id,
        actor_id=actor_id,
        target_id=target_id,
        change_summary=change_summary or {},
        trace_id=trace_id,
    )
    session.add(event)
    return event
