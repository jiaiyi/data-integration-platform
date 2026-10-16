"""系统健康检查。"""

from fastapi import APIRouter, Depends

from data_integration_platform import __version__
from data_integration_platform.api.schemas import HealthResponse
from data_integration_platform.config import Settings, get_settings

router = APIRouter(tags=["system"])


@router.get("/health", response_model=HealthResponse)
async def health(settings: Settings = Depends(get_settings)) -> HealthResponse:
    """返回服务存活状态与当前身份模式，不读取凭据、不探测外部依赖。"""
    return HealthResponse(
        status="ok",
        service=settings.app_name,
        version=__version__,
        environment=settings.environment,
        auth_mode=settings.auth_mode,
        policy_bundle_version=settings.harness_policy_bundle_version,
    )
