"""Pipeline 与版本接口（SPEC §6.1：8 个端点）。

由 M3 的 T-19 ~ T-22 实现：创建版本、设计审查视图、profiling / generation /
validation / revisions。
"""

from fastapi import APIRouter

router = APIRouter(tags=["pipelines"])
