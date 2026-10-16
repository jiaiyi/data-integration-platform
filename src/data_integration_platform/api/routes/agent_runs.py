"""生成任务接口（SPEC §6.1：4 个端点）。

由 M3 的 T-19 实现：发起生成、回答澄清问题、查询生成记录。
`status=WAITING_INPUT` 表示需要澄清，用回答接口从同一 `thread_id` 恢复（FR-27）。
"""

from fastapi import APIRouter

router = APIRouter(tags=["agent-runs"])
