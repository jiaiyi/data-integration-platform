"""通用副作用准备单与审批查询接口（SPEC §6.1：3 个端点）。

由 M5 的 T-36 实现：九种副作用操作共用同一「申请—审批—提交」入口（FR-57）。
"""

from fastapi import APIRouter

router = APIRouter(tags=["side-effects"])
