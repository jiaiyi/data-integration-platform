"""身份与项目上下文注入（SPEC §8.1 身份边界）。

两种模式：
- `dev-header`：仅本地开发使用，直接信任 `X-Actor-Id`；
- `trusted-gateway`：只信任经共享密钥签名、且 Nonce 未被消费过的网关注入请求头。

生产环境（`environment ∈ {prod, production}`）强制 `trusted-gateway`，由
`Settings.require_production_boundaries` 在启动期拦截，禁止客户端自行伪造身份。
"""

import hashlib
import hmac
import re
import time
import uuid
from functools import lru_cache
from typing import Annotated, Protocol

from fastapi import Depends, Header, HTTPException, Request, status
from redis.asyncio import Redis
from redis.exceptions import RedisError

from data_integration_platform.config import Settings, get_settings
from data_integration_platform.observability import current_trace_id

_SAFE_NONCE = re.compile(r"^[A-Za-z0-9_-]{16,128}$")


class GatewayNonceStore(Protocol):
    """可信网关 Nonce 的跨进程一次性消费边界。"""

    async def consume(self, nonce: str, *, ttl_seconds: int) -> bool:
        """原子消费 Nonce；已存在返回 False，存储不可用时向上抛出异常。"""
        ...


class RedisGatewayNonceStore:
    """用 Redis SET NX 让所有 API 实例共享同一防重放窗口。"""

    def __init__(self, client: Redis) -> None:
        """绑定长连接客户端，避免每个请求重建连接池。"""
        self._client = client

    async def consume(self, nonce: str, *, ttl_seconds: int) -> bool:
        """只保存 Nonce 摘要，避免 Redis 成为身份轨迹副本。"""
        nonce_sha256 = hashlib.sha256(nonce.encode("ascii")).hexdigest()
        result = await self._client.set(
            f"dip:gateway-nonce:{nonce_sha256}",
            "1",
            ex=ttl_seconds,
            nx=True,
        )
        return bool(result)


@lru_cache
def get_gateway_nonce_store() -> GatewayNonceStore:
    """构造进程级 Redis Nonce Store；生产认证依赖不可用时必须失败关闭。"""
    return RedisGatewayNonceStore(Redis.from_url(get_settings().redis_url, decode_responses=True))


def gateway_signature_payload(
    *,
    actor: str,
    timestamp: str,
    nonce: str,
    method: str,
    path_and_query: str,
) -> bytes:
    """构造网关与控制面共享的规范签名文本；字段换行分隔以避免拼接歧义。"""
    return "\n".join((actor, timestamp, nonce, method.upper(), path_and_query)).encode("utf-8")


def sign_gateway_identity(
    *,
    secret: str,
    actor: str,
    timestamp: str,
    nonce: str,
    method: str,
    path_and_query: str,
) -> str:
    """用 HMAC-SHA256 签署可信身份；供网关实现与契约测试复用。"""
    payload = gateway_signature_payload(
        actor=actor,
        timestamp=timestamp,
        nonce=nonce,
        method=method,
        path_and_query=path_and_query,
    )
    return hmac.new(secret.encode("utf-8"), payload, hashlib.sha256).hexdigest()


def request_target(request: Request) -> str:
    """返回不含主机的原始请求目标，防止代理改写 Host 导致合法签名失效。"""
    raw_path = request.scope.get("raw_path", request.url.path.encode("utf-8"))
    query = request.scope.get("query_string", b"")
    path = bytes(raw_path).decode("latin-1")
    return f"{path}?{bytes(query).decode('latin-1')}" if query else path


def _authentication_failed(detail: str) -> HTTPException:
    """统一 401 响应，避免不同失败分支泄漏签名校验内部细节。"""
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail=detail,
        headers={"WWW-Authenticate": "Gateway-HMAC"},
    )


def _authentication_unavailable() -> HTTPException:
    """防重放存储不可用时返回 503，禁止降级为仅验 HMAC 的可重放认证。"""
    return HTTPException(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        detail="可信网关认证暂时不可用",
        headers={"Retry-After": "5"},
    )


async def actor_id(
    request: Request,
    x_actor_id: Annotated[str, Header()],
    x_auth_timestamp: Annotated[str | None, Header()] = None,
    x_auth_nonce: Annotated[str | None, Header()] = None,
    x_auth_signature: Annotated[str | None, Header()] = None,
    settings: Settings = Depends(get_settings),
    nonce_store: GatewayNonceStore = Depends(get_gateway_nonce_store),
) -> uuid.UUID:
    """解析调用者身份；生产模式只信任经过签名且未重放的网关注入请求头。"""
    try:
        parsed_actor = uuid.UUID(x_actor_id)
    except ValueError as exc:
        raise _authentication_failed("调用者身份无效") from exc

    if settings.auth_mode == "dev-header":
        return parsed_actor

    if not x_auth_timestamp or not x_auth_nonce or not x_auth_signature:
        raise _authentication_failed("请求缺少可信网关签名")
    if not _SAFE_NONCE.fullmatch(x_auth_nonce):
        raise _authentication_failed("可信网关签名无效")
    try:
        signed_at = int(x_auth_timestamp)
    except ValueError as exc:
        raise _authentication_failed("可信网关签名无效") from exc
    if abs(int(time.time()) - signed_at) > settings.gateway_max_clock_skew_seconds:
        raise _authentication_failed("可信网关签名已过期")

    secret = settings.gateway_hmac_secret
    if secret is None:
        # Settings 已在启动期校验；保留运行时保护，避免错误依赖覆盖绕过边界。
        raise _authentication_failed("可信网关认证未配置")
    expected = sign_gateway_identity(
        secret=secret.get_secret_value(),
        actor=x_actor_id,
        timestamp=x_auth_timestamp,
        nonce=x_auth_nonce,
        method=request.method,
        path_and_query=request_target(request),
    )
    if not hmac.compare_digest(expected, x_auth_signature.casefold()):
        raise _authentication_failed("可信网关签名无效")
    try:
        consumed = await nonce_store.consume(
            x_auth_nonce,
            # 接受窗口覆盖时间戳前后两个方向，额外 5 秒避免边界时钟抖动
            # 让已接受请求在另一实例上重新获得一次机会。
            ttl_seconds=settings.gateway_max_clock_skew_seconds * 2 + 5,
        )
    except RedisError as exc:
        raise _authentication_unavailable() from exc
    if not consumed:
        raise _authentication_failed("可信网关签名已被使用")
    return parsed_actor


# 用 Depends(...) 显式声明依赖，而不是 `Annotated[T, 裸函数]`：
# 后者在 Optional 注解（如 `str | None`）下不会被识别为依赖，会退化成一个必填的
# query 参数并直接返回 422。
ActorId = Annotated[uuid.UUID, Depends(actor_id)]


async def require_bootstrap_token(
    x_bootstrap_token: Annotated[str | None, Header()] = None,
    settings: Settings = Depends(get_settings),
) -> None:
    """校验首次初始化 Token。

    接口未启用时返回 404 —— 不暴露「存在一个初始化入口但 Token 不对」这一信息（FR-05）。
    """
    if not settings.bootstrap_enabled or settings.bootstrap_token is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="接口不存在")
    if x_bootstrap_token is None or not hmac.compare_digest(
        x_bootstrap_token, settings.bootstrap_token.get_secret_value()
    ):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="首次初始化 Token 无效"
        )


async def resolve_trace_id(x_trace_id: Annotated[str | None, Header()] = None) -> str | None:
    """返回本次请求的 Trace ID：优先取客户端透传值，否则取当前 Span。"""
    return x_trace_id or current_trace_id()


TraceId = Annotated[str | None, Depends(resolve_trace_id)]
