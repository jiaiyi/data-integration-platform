"""远端文件的受限读取策略（T-16 GAP-02 对象存储 / HDFS）。

策略集中在**一处**：对象存储与 WebHDFS 是两条独立实现，但必须遵守同一上限与同一
「这个格式的结构在哪」判断，否则两条路径会长出不一致的边界，而边界不一致本身就是漏洞。

硬约束（决策 D-Q3）：**严禁全量下载**。对象可达 GB 级，全量下载会打爆控制面内存与
临时盘 —— 这是正确性问题，不是性能优化。
"""

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from data_integration_platform.application.profiling import ProbeResult
from data_integration_platform.domain.enums import FileAssetFormat
from data_integration_platform.domain.errors import DomainError
from data_integration_platform.infrastructure.file_formats import (
    detect_format,
    inspect_file,
    requires_complete_file,
)
from data_integration_platform.infrastructure.probe_support import cell_to_plain


@dataclass(frozen=True, slots=True)
class RemoteReadPlan:
    """一次远端探查的读取计划：读多少字节、能否据此得知真实行数。"""

    file_format: FileAssetFormat
    read_length: int
    complete: bool


def plan_remote_read(*, object_name: str, size_bytes: int, cap_bytes: int) -> RemoteReadPlan:
    """决定读多少字节。

    结构位于文件尾部的格式（Parquet / ORC / Excel / PDF）在超过上限时**明确报错**：
    前缀字节里根本没有 schema，读一半再「推断」出来的是假结构，
    比直接失败危险得多。
    """
    if size_bytes <= 0:
        raise DomainError(f"对象内容为空：{object_name}")
    file_format = detect_format(object_name)
    read_length = min(size_bytes, cap_bytes)
    if requires_complete_file(file_format) and read_length < size_bytes:
        raise DomainError(
            f"{file_format.value} 的结构信息位于文件尾部，读取上限为 {cap_bytes} 字节，"
            f"而该对象为 {size_bytes} 字节；请改用不超过上限的对象或文本类格式"
        )
    return RemoteReadPlan(
        file_format=file_format,
        read_length=read_length,
        complete=read_length >= size_bytes,
    )


def build_remote_probe_result(
    *,
    plan: RemoteReadPlan,
    data: bytes,
    schema_name: str,
    object_name: str,
    size_bytes: int,
    sample_limit: int,
    statistics: Mapping[str, Any] | None = None,
) -> ProbeResult:
    """把读到的字节按计划转成探查结果；完整读取时才给出真实行数。"""
    inspection = inspect_file(
        data,
        file_format=plan.file_format,
        sample_limit=sample_limit,
        complete=plan.complete,
    )
    return ProbeResult(
        schema_name=schema_name,
        table_name=object_name,
        columns=tuple(inspection.columns),
        # 文件类数据源没有主键约束；显式留空，并标注原因而不是让调用方猜。
        primary_keys=(),
        statistics={
            **(statistics or {}),
            **inspection.statistics,
            "object_bytes": size_bytes,
            "primary_key_supported": False,
        },
        sample_rows=tuple(
            {key: cell_to_plain(value) for key, value in row.items()}
            for row in inspection.sample_rows
        ),
        # 只有整份读取时 `inspection.row_count` 才是文件真实行数。
        row_count=inspection.row_count,
    )
