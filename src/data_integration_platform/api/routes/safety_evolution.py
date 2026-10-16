"""Benchmark 与受控安全进化接口（SPEC §6.1：14 个端点）。

由 M8 的 T-58 实现：Benchmark 运行、改进提案、影子/Canary/推广/回归撤销。
`PROMOTION_APPROVED` 只是授权，不等于热更新（FR-91）。
"""

from fastapi import APIRouter

router = APIRouter(tags=["safety-evolution"])
