"""Secret 轮换接口（SPEC §6.1：4 个端点）。

由 M2 的 T-15 实现：暂存 → 验证 → 激活三阶段，激活前必须验证新凭据可用（FR-19）。
"""

from fastapi import APIRouter

router = APIRouter(tags=["secret-rotations"])
