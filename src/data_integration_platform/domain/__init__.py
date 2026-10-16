"""领域层：枚举、显式状态机、Harness 值对象与监督契约。

本层不导入 FastAPI、Celery、SQLAlchemy 或任何数据库驱动，也不做 I/O，
以保证业务规则可以脱离框架独立测试（SPEC §3.1 依赖方向铁律）。
"""
