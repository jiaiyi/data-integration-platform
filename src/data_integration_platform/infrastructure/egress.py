"""平台主动外发的出口主机白名单（T-17 REST 数据源 / T-15 告警 Webhook）。

两类外发（探查 REST 数据源、投递告警 Webhook）共用同一段判定：
**白名单为空表示全部拒绝**，而不是全部放行 —— 失败关闭是这套治理的默认姿态。

校验对象是**实际要请求的 URL 主机**，而不是配置里的字符串，
避免「配置写 A、重定向到 B」这类绕过。
"""

from urllib.parse import urlsplit

from data_integration_platform.domain.errors import DomainError, PermissionDenied


def require_allowed_egress(url: str, allowed_hosts: frozenset[str]) -> str:
    """校验外发目标主机；通过时返回规范化主机名，供日志与审计使用。"""
    parts = urlsplit(url)
    if parts.scheme not in {"http", "https"}:
        raise DomainError("外发地址必须是 http 或 https")
    host = (parts.hostname or "").casefold()
    if not host:
        raise DomainError("外发地址缺少主机名")
    if not allowed_hosts:
        raise PermissionDenied("出口主机白名单为空，按失败关闭策略拒绝全部外发调用")
    port = parts.port or (443 if parts.scheme == "https" else 80)
    if host in allowed_hosts or f"{host}:{port}" in allowed_hosts:
        return host
    raise PermissionDenied(f"目标主机不在出口白名单内：{host}")
