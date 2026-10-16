"""Celery 装配（SPEC §3.2 / §4.1）。

本模块不含业务逻辑。任务模块（`*_tasks.py`）随各自里程碑加入：
连接探查与凭据治理在 M2、生成在 M3、执行与 Outbox 投递在 M5。

约定（SPEC T-37）：`acks_late` + `prefetch_multiplier=1` + `reject_on_worker_lost`，
使 Worker 崩溃时任务被重新投递，而不是静默丢失；幂等由载荷摘要、租约与
Capability 单次消费共同保证，不依赖投递次数。
"""

import sys

from celery import Celery

from data_integration_platform.config import get_settings
from data_integration_platform.infrastructure.database import engine
from data_integration_platform.observability import configure_logging, configure_observability

settings = get_settings()
configure_logging(json_output=settings.is_production)

celery_app = Celery(
    "data_integration_platform",
    broker=settings.redis_url,
    backend=settings.redis_url,
)
celery_app.conf.update(
    task_serializer="json",
    result_serializer="json",
    accept_content=["json"],
    task_acks_late=True,
    worker_prefetch_multiplier=1,
    task_reject_on_worker_lost=True,
)

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
