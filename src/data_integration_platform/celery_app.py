"""Celery 应用定义（**无副作用**）。

刻意与 `worker.py` 分开：任务模块需要引用 `celery_app`，而 `worker.py` 在导入时
会做日志与可观测初始化。若任务模块直接 import `worker`，API 进程只要导入一个任务
就会顺带跑一遍 Worker 的初始化 —— 把进程角色搞错。这里只放配置。
"""

import asyncio
from collections.abc import Callable
from typing import Any

from celery import Celery

from data_integration_platform.config import get_settings

settings = get_settings()

_ALERT_SCAN_TASK = "data_integration_platform.credential_alert_tasks.scan_credential_expiry_alerts"

celery_app = Celery(
    "data_integration_platform",
    broker=settings.redis_url,
    backend=settings.redis_url,
    include=[
        "data_integration_platform.beat_tasks",
        "data_integration_platform.connection_tasks",
        "data_integration_platform.execution_tasks",
        "data_integration_platform.generation_tasks",
        "data_integration_platform.secret_migration_tasks",
        "data_integration_platform.secret_rotation_tasks",
        "data_integration_platform.secret_rotation_rollback_tasks",
        "data_integration_platform.secret_destruction_tasks",
        "data_integration_platform.credential_alert_tasks",
    ],
)

celery_app.conf.update(
    task_serializer="json",
    result_serializer="json",
    accept_content=["json"],
    # Worker 崩溃时任务被重新投递而不是静默丢失；幂等由载荷摘要、租约与
    # Capability 单次消费共同保证，不依赖投递次数。
    task_acks_late=True,
    worker_prefetch_multiplier=1,
    task_reject_on_worker_lost=True,
    # 明确给 Broker 设超时：默认不设超时，Broker 不可达时投递会一直挂住，
    # 把「外部依赖故障」伪装成「请求还没处理完」。
    broker_transport_options={
        "socket_connect_timeout": 5,
        "socket_timeout": 5,
        "retry_on_timeout": False,
    },
    broker_connection_retry_on_startup=True,
    # 凭据到期扫描（FR-22）。T-39 会把 Beat 的完整集合（心跳、Outbox 投递、调度扫描）
    # 一并补齐；这里先登记本里程碑真正需要的那一条，不预置空跑的定时任务。
    beat_schedule={
        "credential-expiry-alert-scan": {
            "task": _ALERT_SCAN_TASK,
            "schedule": float(settings.credential_alert_scan_interval_seconds),
        },
        # T-39 的四条定时扫描；间隔都是可调实现参数（PRD 决策 Q5）。
        "worker-heartbeat": {
            "task": "data_integration_platform.beat_tasks.worker_heartbeat",
            "schedule": float(settings.worker_heartbeat_interval_seconds),
        },
        "schedule-scan": {
            "task": "data_integration_platform.beat_tasks.scan_pipeline_schedules",
            "schedule": float(settings.schedule_scan_interval_seconds),
        },
        "outbox-dispatch-scan": {
            "task": "data_integration_platform.beat_tasks.dispatch_pending_commands",
            "schedule": float(settings.harness_outbox_scan_interval_seconds),
        },
    },
)


async def dispatch(delay: Callable[..., Any], *args: Any) -> None:
    """把任务投递卸载到线程执行。

    Celery 的投递是**同步阻塞**调用；直接在 async 路由里调用会卡住整个事件循环 ——
    Broker 稍慢一点，所有并发请求一起卡住（实测踩过：请求挂到客户端读超时）。
    """
    await asyncio.to_thread(delay, *args)
