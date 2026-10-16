"""Secret 轮换回退接口（SPEC §6.1：4 个端点）。

由 M2 的 T-15 实现；激活后发现问题可回退到旧凭据（FR-20）。
"""

from fastapi import APIRouter

router = APIRouter(tags=["secret-rotation-rollbacks"])
