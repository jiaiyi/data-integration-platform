"""数据源连接与元数据探查接口（SPEC §6.1：8 个端点）。

由 M2 的 T-10 ~ T-17 实现（含 GAP-01 ClickHouse / GAP-02 S3-MinIO / GAP-03 REST API）。
"""

from fastapi import APIRouter

router = APIRouter(tags=["connections"])
