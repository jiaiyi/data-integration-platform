"""Agent 编排层：LangGraph 状态机与 Checkpoint（SPEC §3.2）。

本层**不持有**数据库 Session、**不持有**引擎凭据、也不直接提交 SeaTunnel ——
模型只产出候选，确定性代码与数据库约束才是边界（ADR-06）。
"""
