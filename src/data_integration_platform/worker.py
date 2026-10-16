"""Celery Worker 入口（SPEC §3.2 / §4.1）。

本模块不含业务逻辑，只负责进程级装配。任务定义在各 `*_tasks.py`，
Celery 应用定义在 `celery_app.py`。

Beat 定时任务随里程碑加入：心跳与告警扫描在 M5/M6、Outbox 投递在 M5。
"""

import sys

from data_integration_platform.celery_app import celery_app
from data_integration_platform.config import get_settings
from data_integration_platform.infrastructure.database import engine
from data_integration_platform.observability import configure_logging, configure_observability

settings = get_settings()
configure_logging(json_output=settings.is_production)

# Worker 是独立进程；挂载 Celery、数据库与外部 HTTP 埋点以传播同一 Trace。
configure_observability(
    "worker",
    celery_app=celery_app,
    sqlalchemy_engine=engine.sync_engine,
)


def run() -> None:
    """启动 Worker；Windows 下必须使用 solo 池（prefork 在 Windows 不可用）。"""
    argv = ["worker", "--loglevel=INFO"]
    if sys.platform == "win32":
        argv.append("--pool=solo")
    celery_app.worker_main(argv)
