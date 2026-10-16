"""Tool Broker：副作用的唯一出口（T-27 / FR-44）。

所有会产生副作用的调用都从这里出去，出去之前**逐项重校**：
工具清单 → 输入 → 资源范围 → 策略 → 能力令牌 → 重放状态。
任何一项不成立就抛出去，**执行器一次都不会被调到** —— 这就是「无绕过路径」的可检验形态：
测试断言的不是「返回了错误」，而是「执行器的调用次数是 0」。

为什么要「重校」而不是信任上游：上游判定发生在**准备阶段**，与执行阶段之间隔着
人工审批、队列等待和可能的进程重启。中间任何一刻状态都可能变（策略包换了、
令牌过期了、资源改了），所以这里按**当下**的事实再判一次，而不是沿用旧结论。
"""

import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol

from sqlalchemy.ext.asyncio import AsyncSession

from data_integration_platform.application.harness import grants
from data_integration_platform.application.harness.capability import (
    CapabilityExpectation,
    CapabilityMismatch,
    SignedCapability,
    verify_capability,
)
from data_integration_platform.application.harness.pdp import PolicyBundle
from data_integration_platform.application.harness.signing import SignatureError, VerifierPort
from data_integration_platform.application.harness.tools import ToolRegistry, ToolSpec
from data_integration_platform.domain.errors import DomainError, PermissionDenied
from data_integration_platform.domain.harness import (
    RiskLevel,
    ToolIntent,
    ToolIntentOutcome,
)

_REJECTION = "工具调用被拒绝"


class ToolExecutorPort(Protocol):
    """真正干活的执行器；由基础设施层实现（SeaTunnel 客户端、Doris 连接等）。

    只在 Broker 全部校验通过之后被调用一次。
    """

    async def __call__(
        self, *, spec: ToolSpec, payload: Mapping[str, Any]
    ) -> Mapping[str, Any]: ...


@dataclass(frozen=True, slots=True)
class ToolCall:
    """一次副作用调用的完整描述；`payload` 是工具输入，必须符合清单 Schema。"""

    intent: ToolIntent
    payload: Mapping[str, Any]
    expectation: CapabilityExpectation
    records: int | None = None
    bytes_: int | None = None


@dataclass(frozen=True, slots=True)
class ToolReceipt:
    """一次成功调用的回执；不含凭据，可进证据账本。"""

    tool_name: str
    tool_version: str
    risk_level: RiskLevel
    matched_rules: tuple[str, ...]
    token_id: str
    result: Mapping[str, Any]


class ToolBroker:
    def __init__(
        self,
        *,
        registry: ToolRegistry,
        bundle: PolicyBundle,
        verifier: VerifierPort,
        executor: ToolExecutorPort,
    ) -> None:
        self._registry = registry
        self._bundle = bundle
        self._verifier = verifier
        self._executor = executor

    async def invoke(
        self,
        session: AsyncSession,
        *,
        call: ToolCall,
        token: SignedCapability,
        execution_run_id: uuid.UUID,
        now: datetime | None = None,
    ) -> ToolReceipt:
        """校验 → 原子消费令牌 → 调用执行器。

        **所有**拒绝都收敛成 `PermissionDenied`：验签不过、令牌不匹配、令牌不存在、
        令牌已被用过 —— 对调用方都是同一件事「这次调用被拒」。分开成 404/409 会诱导
        调用方把「令牌已被使用」当成可重试的冲突，而它恰恰是最不该重试的那一种。
        原始原因保留在消息里，供排查与记账。
        """
        spec = self._resolve(call)
        self._require_input(spec, call)
        self._require_resource_scope(call)
        decision = self._bundle.evaluate(call.intent, spec=spec)
        # 拒绝与延迟都不放行。`REQUIRE_APPROVAL` 可以继续，是因为**令牌本身就是审批完成的
        # 证据**：签发方只在所需职责槽都具名批准之后才发得出这张令牌（M5 的准备单流程）。
        # 审批状态不需要在这里再查一遍，令牌的六个绑定项已经把「给谁、对什么、在哪个环境」
        # 钉死了，重放检查保证它只能用一次。
        if decision.outcome not in (ToolIntentOutcome.ALLOW, ToolIntentOutcome.REQUIRE_APPROVAL):
            raise PermissionDenied(
                f"{_REJECTION}：策略判定为 {decision.outcome.value}（{decision.reason}）"
            )

        try:
            claims = verify_capability(
                token, expectation=call.expectation, verifier=self._verifier, now=now
            )
        except (SignatureError, CapabilityMismatch) as exc:
            raise PermissionDenied(f"{_REJECTION}：{exc}") from exc

        # 重放检查放在**执行器之前、且在同一事务里**：令牌被消费掉与副作用发生之间
        # 不留窗口，否则两个 Worker 可以在同一个窗口里各调一次执行器。
        try:
            await grants.consume(
                session,
                token_sha256=token.token_sha256(),
                subject_id=claims.subject_id,
                execution_run_id=execution_run_id,
                now=now,
            )
        except DomainError as exc:
            raise PermissionDenied(f"{_REJECTION}：{exc}") from exc

        result = await self._executor(spec=spec, payload=call.payload)
        return ToolReceipt(
            tool_name=spec.manifest.name,
            tool_version=spec.manifest.version,
            risk_level=decision.risk_level,
            matched_rules=decision.matched_rules,
            token_id=claims.token_id,
            result=dict(result),
        )

    # -------------------------------------------------------------- 校验
    def _resolve(self, call: ToolCall) -> ToolSpec:
        try:
            return self._registry.resolve(
                name=call.intent.tool_name, version=call.intent.tool_version
            )
        except DomainError as exc:
            raise PermissionDenied(f"{_REJECTION}：{exc}") from exc

    def _require_input(self, spec: ToolSpec, call: ToolCall) -> None:
        try:
            spec.parse(call.payload)
            self._registry.require_within_limits(spec, records=call.records, bytes_=call.bytes_)
        except DomainError as exc:
            raise PermissionDenied(f"{_REJECTION}：{exc}") from exc

    def _require_resource_scope(self, call: ToolCall) -> None:
        """资源指纹必须与意图里的资源范围一致。

        调用方不能一边声明「只碰这个项目、这些表」，一边拿一份对不上的资源指纹去换令牌 ——
        令牌绑的是指纹，指纹必须真的来自这份范围声明。
        """
        if call.expectation.resource_fingerprint != call.intent.scope.fingerprint():
            raise PermissionDenied(f"{_REJECTION}：资源指纹与意图中的资源范围不一致")
