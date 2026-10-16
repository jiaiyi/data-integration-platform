"""Harness 安全内核（M4 / SPEC 阶段 4）。

分层与别处一致：纯规则在 `domain/harness.py`，这里放**用例与策略**，
真正的执行器（SeaTunnel 客户端、Doris 连接）留在 `infrastructure/`。

内核的职责边界：它决定「这件事能不能做、由谁批准、拿什么凭据去做」，
不亲自做那件事。所以本包内没有任何外部 I/O。
"""
