"""执行实例、监督与质量接口（SPEC §6.1：13 个端点）。

由 M5 的 T-33 ~ T-35 实现：执行查询、事件、监督快照、质量报告、取消、
重试准备，以及 **Prepare / Commit** 两个协议入口。
"""

from fastapi import APIRouter

router = APIRouter(tags=["executions"])
