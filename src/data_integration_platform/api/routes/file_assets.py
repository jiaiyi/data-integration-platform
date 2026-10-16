"""文件与对象存储资产接口（SPEC §6.1：3 个端点）。

由 M2 的 T-16 实现。
"""

from fastapi import APIRouter

router = APIRouter(tags=["file-assets"])
