"""Secret Provider 迁移接口（SPEC §6.1：4 个端点）。

由 M2 的 T-15 实现；迁移过程**不销毁**源凭据，可回退（FR-18）。
"""

from fastapi import APIRouter

router = APIRouter(tags=["secret-migrations"])
