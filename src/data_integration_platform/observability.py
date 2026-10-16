"""可观测基座：结构化日志 + OpenTelemetry 埋点（SPEC T-04 / §8.4）。

两条硬约束：
1. **路由全部注册后**再挂载 FastAPIInstrumentor，否则埋点覆盖不到完整 ASGI 应用。
2. Span 与日志只带低敏字段 —— 不记录 Prompt、Artifact 内容、凭据与原始业务行。
"""

import logging
from typing import Any, Literal

import structlog
from fastapi import FastAPI
from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.instrumentation.celery import CeleryInstrumentor
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
from opentelemetry.instrumentation.sqlalchemy import SQLAlchemyInstrumentor
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.sdk.trace.sampling import ParentBased, TraceIdRatioBased

from data_integration_platform.config import Settings, get_settings

_configured = False


def configure_logging(*, json_output: bool = False, level: int = logging.INFO) -> None:
    """装配 structlog；生产用 JSON 便于采集，本地用可读输出。"""
    renderer: Any = (
        structlog.processors.JSONRenderer(ensure_ascii=False)
        if json_output
        else structlog.dev.ConsoleRenderer()
    )
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            renderer,
        ],
        wrapper_class=structlog.make_filtering_bound_logger(level),
        logger_factory=structlog.PrintLoggerFactory(),
        cache_logger_on_first_use=True,
    )


def configure_observability(
    component: Literal["api", "worker"],
    *,
    fastapi_app: FastAPI | None = None,
    sqlalchemy_engine: Any | None = None,
    celery_app: Any | None = None,
    settings: Settings | None = None,
) -> bool:
    """按进程启用 OTLP Trace 并串联 API / 数据库 / HTTP / Celery 边界。

    默认关闭，使本地开发不依赖外部 Collector。Exporter 只发送框架语义字段，
    主动屏蔽鉴权类请求头，且不采集请求体、SQL 参数或业务样本。
    """
    global _configured
    resolved = settings or get_settings()
    if not resolved.otel_enabled or _configured:
        return False

    resource = Resource.create(
        {
            "service.name": f"{resolved.otel_service_name}-{component}",
            "deployment.environment.name": resolved.environment,
        }
    )
    provider = TracerProvider(
        resource=resource,
        sampler=ParentBased(TraceIdRatioBased(resolved.otel_trace_sample_ratio)),
    )
    endpoint = str(resolved.otel_traces_endpoint) if resolved.otel_traces_endpoint else None
    provider.add_span_processor(
        BatchSpanProcessor(
            OTLPSpanExporter(endpoint=endpoint, timeout=resolved.otel_export_timeout_seconds)
        )
    )
    trace.set_tracer_provider(provider)

    if fastapi_app is not None:
        FastAPIInstrumentor.instrument_app(
            fastapi_app,
            tracer_provider=provider,
            excluded_urls="/health",
            http_capture_headers_sanitize_fields=[
                "authorization",
                "cookie",
                "x-api-key",
                "x-auth-signature",
                "x-bootstrap-token",
            ],
            exclude_spans=["receive", "send"],
        )
    HTTPXClientInstrumentor().instrument(tracer_provider=provider)
    if sqlalchemy_engine is not None:
        # 默认不记录绑定参数，避免样本或密钥进入 Span。
        SQLAlchemyInstrumentor().instrument(
            engine=sqlalchemy_engine,
            tracer_provider=provider,
            enable_commenter=False,
        )
    if celery_app is not None:
        # CeleryInstrumentor 尚未发布类型声明，运行时 API 与官方用法一致。
        CeleryInstrumentor().instrument(tracer_provider=provider)  # type: ignore[no-untyped-call]
    _configured = True
    return True


def current_trace_id() -> str | None:
    """返回当前有效 W3C Trace ID，供错误响应与审计记录关联。"""
    span_context = trace.get_current_span().get_span_context()
    if not span_context.is_valid:
        return None
    return f"{span_context.trace_id:032x}"


def tracer(name: str = "data_integration_platform") -> trace.Tracer:
    """获取平台统一 Tracer；业务 Span 只允许写低基数、无敏感值的属性。"""
    return trace.get_tracer(name)
