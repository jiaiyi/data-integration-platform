"""FastAPI 控制面装配（SPEC §3.2）。

本模块不含业务逻辑：只挂载路由、注册领域异常到 HTTP 的映射、装配可观测埋点。
"""

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from data_integration_platform import __version__
from data_integration_platform.api.routes import (
    agent_runs,
    approvals,
    cleanups,
    connections,
    executions,
    file_assets,
    health,
    operations,
    pipelines,
    projects,
    rollbacks,
    safety_evolution,
    schedules,
    secret_destructions,
    secret_migrations,
    secret_rotation_rollbacks,
    secret_rotations,
    side_effects,
)
from data_integration_platform.config import get_settings
from data_integration_platform.domain.errors import (
    DomainError,
    ExternalServiceUnavailable,
    PermissionDenied,
    ResourceNotFound,
    VersionConflict,
)
from data_integration_platform.infrastructure.database import engine
from data_integration_platform.observability import (
    configure_logging,
    configure_observability,
    current_trace_id,
)

settings = get_settings()
configure_logging(json_output=settings.is_production)

app = FastAPI(title=settings.app_name, version=__version__)
app.include_router(health.router)
app.include_router(projects.router, prefix=settings.api_prefix)
app.include_router(connections.router, prefix=settings.api_prefix)
app.include_router(file_assets.router, prefix=settings.api_prefix)
app.include_router(pipelines.router, prefix=settings.api_prefix)
app.include_router(agent_runs.router, prefix=settings.api_prefix)
app.include_router(approvals.router, prefix=settings.api_prefix)
app.include_router(executions.router, prefix=settings.api_prefix)
app.include_router(operations.router, prefix=settings.api_prefix)
app.include_router(schedules.router, prefix=settings.api_prefix)
app.include_router(rollbacks.router, prefix=settings.api_prefix)
app.include_router(cleanups.router, prefix=settings.api_prefix)
app.include_router(side_effects.router, prefix=settings.api_prefix)
app.include_router(secret_migrations.router, prefix=settings.api_prefix)
app.include_router(secret_rotations.router, prefix=settings.api_prefix)
app.include_router(secret_rotation_rollbacks.router, prefix=settings.api_prefix)
app.include_router(secret_destructions.router, prefix=settings.api_prefix)
app.include_router(safety_evolution.router, prefix=settings.api_prefix)


@app.exception_handler(DomainError)
async def domain_error_handler(_request: Request, exc: DomainError) -> JSONResponse:
    """把领域异常映射为稳定 HTTP 错误结构，并附带可关联日志的 trace id。

    未引入字符串业务错误码段位（决策 D-Q4）：异常类型区分语义、状态码供前端分流、
    `trace_id` 直达日志，三者已足够；六段错误码只作为预留契约登记在 SPEC §6.4。
    """
    status_code = 400
    if isinstance(exc, PermissionDenied):
        status_code = 403
    elif isinstance(exc, ExternalServiceUnavailable):
        status_code = 503
    elif isinstance(exc, ResourceNotFound):
        status_code = 404
    elif isinstance(exc, VersionConflict):
        status_code = 409
    content: dict[str, str] = {"detail": str(exc)}
    if trace := current_trace_id():
        content["trace_id"] = trace
    return JSONResponse(status_code=status_code, content=content)


# 路由全部注册后再挂载自动埋点，确保 FastAPI Instrumentor 覆盖完整 ASGI 应用。
configure_observability("api", fastapi_app=app, sqlalchemy_engine=engine.sync_engine)


def run() -> None:
    """启动控制面服务。"""
    import uvicorn

    uvicorn.run("data_integration_platform.main:app", host="0.0.0.0", port=8000)
