"""受管清理接口（SPEC §6.1：4 个端点）。

由 M6 的 T-45 实现：清理影子表未发布脏数据并释放资源。
"""

from fastapi import APIRouter

router = APIRouter(tags=["cleanups"])
