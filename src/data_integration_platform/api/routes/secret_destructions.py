"""Secret 销毁接口（SPEC §6.1：4 个端点）。

由 M2 的 T-15 实现；只作用于轮换源凭据或已回退的暂存凭据，且需审批（FR-21）。
"""

from fastapi import APIRouter

router = APIRouter(tags=["secret-destructions"])
