"""受管回滚接口（SPEC §6.1：4 个端点）。

由 M6 的 T-45 实现；回滚与清理互斥（FR-74），且不改写原执行记录（FR-73）。
"""

from fastapi import APIRouter

router = APIRouter(tags=["rollbacks"])
