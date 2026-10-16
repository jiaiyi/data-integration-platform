"""调度配置接口（SPEC §6.1：3 个端点）。

由 M3 的 T-23 实现：配置批量/水位增量模式、触发时刻与失火策略，以及启停。
"""

from fastapi import APIRouter

router = APIRouter(tags=["schedules"])
