"""版本审批与执行事实查询接口（SPEC §6.1；对应 PRD FR-56）。

由 M5 的 T-34 实现。
"""

from fastapi import APIRouter

router = APIRouter(tags=["approvals"])
