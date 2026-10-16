"""在同步 Celery 任务里安全执行异步逻辑。

Celery 的任务函数是同步的，而控制面的数据库与探查器都是异步的，因此需要桥接。
桥接集中在这一处。

**必须复用同一个事件循环**：`infrastructure/database.py` 的 `AsyncEngine` 在模块导入时
创建，其连接池里的连接绑定在「第一次使用它的事件循环」上。若每个任务都用 `asyncio.run`
新建循环，第二次任务就会拿着属于上一个循环的连接去读写，在 Windows 的 Proactor 事件循环上
直接崩成 `'NoneType' object has no attribute 'send'`（实测踩过，且报错位置离根因很远）。
"""

import asyncio
import sys
from collections.abc import Coroutine
from typing import Any, TypeVar

T = TypeVar("T")

_loop: asyncio.AbstractEventLoop | None = None


def _worker_loop() -> asyncio.AbstractEventLoop:
    """取（必要时创建）worker 进程级事件循环。"""
    global _loop
    if _loop is None or _loop.is_closed():
        if sys.platform == "win32":
            # Windows 上必须用 Selector 而不是默认的 Proactor：两者只能取交集。
            # - asyncpg（业务库）**两种都能跑**（实测 Selector 上连查正常）；
            # - psycopg（LangGraph 的 Checkpoint 库）**明确拒绝 Proactor**，
            #   抛 `Psycopg cannot use the 'ProactorEventLoop' to run in async mode`。
            # 所以 Selector 是唯一能让业务库与 Checkpoint 库共存的实现。
            # 代价：Windows 的 Selector 循环不支持子进程与管道 reader，本项目不用这两样。
            asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
        _loop = asyncio.new_event_loop()
        asyncio.set_event_loop(_loop)
    return _loop


def run_async(coro: Coroutine[Any, Any, T]) -> T:
    """在进程级事件循环上跑完一个协程并返回结果。"""
    return _worker_loop().run_until_complete(coro)
