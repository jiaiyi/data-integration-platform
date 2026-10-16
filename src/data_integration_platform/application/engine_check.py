"""真实引擎检查的端口与报告组装（T-21 / FR-35）。

FR-35 要求「通过静态门禁后经真实 SeaTunnel 检查，通过后才允许冻结 Artifact」，
并且**引擎不可用时必须阻止冻结**——不允许跳过引擎检查直接冻结。

与确定性门禁（`application/policy.py`）的分工：那一层是纯计算，不碰任何外部系统；
这一层是**唯一**把候选交给外部引擎裁决的地方。结论以 `GateKind.ENGINE_CHECK` 的
`GateFinding` 返回，由 `with_engine_check()` 追加到同一份报告上，因此引擎失败与
确定性失败天然**共享同一份候选预算**（状态机侧已经是这个结构，见 `agent/graph.py`）。

**能力边界（必须如实交代，不能声称等价于 SPEC 写的「引擎 --check」）**：
SeaTunnel 2.3.13 实测没有作业配置级的 dry-run —— CLI 的 `--check` 对作业配置是空操作
（它校验的是集群配置），REST 的 `/submit-job` 会真的起作业，两者都不能用在冻结之前。
因此本层做两件确定的事：①引擎可用性与版本；②把候选配置交给引擎**自己的配置解析器**
判定文档结构（缺 source/sink、source 为空这类问题由引擎报出，而不是由平台自说自话）。
**连接器选项级校验发生在作业解析阶段（提交时），不在本层能力范围内**。
"""

from collections.abc import Awaitable, Callable
from typing import Protocol

from data_integration_platform.application.generation import GeneratedCandidate
from data_integration_platform.application.policy import (
    GateFinding,
    GateReport,
    SourceFacts,
    TargetFacts,
    evaluate,
    with_engine_check,
)


class EngineCheckPort(Protocol):
    """把一份候选配置交给真实引擎检查的能力。

    **不抛异常**：引擎不可用本身就是一种必须落地的结论（FR-35 要求据此阻止冻结），
    把它当异常抛出去会让「引擎不可用」与「状态机自身出错」混在一起。
    """

    async def check(self, *, hocon: str) -> GateFinding:
        """返回 `GateKind.ENGINE_CHECK` 的结论；永不抛异常。"""
        ...


async def gate_runner_with_engine(
    candidate: GeneratedCandidate,
    *,
    engine: EngineCheckPort,
    source: SourceFacts,
    target: TargetFacts,
) -> GateReport:
    """先跑确定性七项，通过后再交给真实引擎 —— 组装成状态机要的 `gate_runner`。

    确定性门禁不过时**不再外呼引擎**：此时候选已经被拒绝，多打一次引擎既浪费一次
    外部调用，又会让修复提示被引擎结论挤掉。FR-35 的验收场景本身也是
    「一份**已通过**静态门禁的候选」遇上引擎不可用。
    """
    report = evaluate(candidate, source=source, target=target)
    if not report.passed:
        return report
    return with_engine_check(report, await engine.check(hocon=candidate.hocon))


def build_gate_runner(
    *,
    engine: EngineCheckPort,
    source: SourceFacts,
    target: TargetFacts,
) -> Callable[[GeneratedCandidate], Awaitable[GateReport]]:
    """把事实与引擎绑成 `agent.graph.GateRunner` 形状的可调用对象。

    不直接引用 `agent.graph` 的类型：依赖方向是 `agent` → `application`，
    反向导入会成环；这里只按结构对齐同一个签名。
    """

    async def run(candidate: GeneratedCandidate) -> GateReport:
        return await gate_runner_with_engine(candidate, engine=engine, source=source, target=target)

    return run
