"""探查器共用支持：同步驱动卸载、标识符校验、失败信息脱敏（T-10 ~ T-12）。

这里刻意只放**所有探查器都必须遵守的约束**，不放方言细节 —— 方言留在各自 Adapter 里。
"""

import asyncio
import re
from collections.abc import Callable, Mapping
from typing import Any, TypeVar

from pydantic import SecretStr

from data_integration_platform.application.masking import scrub_secrets
from data_integration_platform.application.secrets import ConnectionCredentials
from data_integration_platform.domain.errors import DomainError

T = TypeVar("T")

# 合法的库/模式/表/列标识符：字母数字下划线美元符号与东亚文字，长度受限。
_SAFE_IDENTIFIER = re.compile(r"^[\w$一-鿿]{1,128}$")
# HTTP 头名按 RFC 7230 的 token 字符集。
_SAFE_HEADER_NAME = re.compile(r"^[!#$%&'*+\-.^_`|~0-9A-Za-z]{1,128}$")
# 失败原因只保留有限长度，避免把远端长报文原样透出。
_MAX_REASON_LENGTH = 240


async def run_blocking(func: Callable[..., T], /, *args: Any, **kwargs: Any) -> T:
    """把同步驱动调用卸载到工作线程，避免阻塞控制面事件循环（FR-15 的前提）。"""
    return await asyncio.to_thread(func, *args, **kwargs)


def require_safe_identifier(value: str, *, kind: str) -> str:
    """校验标识符。

    表名/列名来自调用方请求，会被拼进元数据查询：**必须**在这里挡住注入，
    不能依赖上游「应该校验过」。值本身仍要按方言加引号。
    """
    if not _SAFE_IDENTIFIER.match(value):
        raise DomainError(f"{kind}名称不合法：只允许字母、数字、下划线、$ 与东亚文字")
    return value


def require_safe_header(name: str, value: str) -> tuple[str, str]:
    """校验自定义请求头。

    头名与头值都会进入 HTTP 请求：必须挡住 CR/LF，否则一次探查就能注入额外请求头
    （凭据以 Secret 引用管理，但头名与头值仍来自配置，不能默认可信）。
    """
    if not _SAFE_HEADER_NAME.match(name):
        raise DomainError(f"鉴权头名不合法：{name}")
    if "\r" in value or "\n" in value or "\x00" in value:
        raise DomainError("鉴权头值不允许包含控制字符")
    return name, value


def require_relative_path(value: str, *, kind: str) -> str:
    """校验相对路径。

    对象键 / HDFS 相对路径会被拼到 base 路径之后：必须挡住 `..` 与绝对路径，
    否则一次探查就能读到受管范围之外的对象。
    """
    candidate = value.strip()
    if not candidate or candidate.startswith("/") or "\\" in candidate:
        raise DomainError(f"{kind}必须是相对路径")
    segments = candidate.split("/")
    if any(segment in {"", ".", ".."} for segment in segments):
        raise DomainError(f"{kind}不允许包含空段、`.` 或 `..`")
    return candidate


def describe_failure(exc: BaseException, *, context: str) -> str:
    """把驱动异常转成可诊断且不含凭据的失败原因。"""
    reason = scrub_secrets(str(exc)).strip().replace("\n", " ")
    if len(reason) > _MAX_REASON_LENGTH:
        reason = f"{reason[:_MAX_REASON_LENGTH]}…"
    detail = (
        f"{context}失败：{type(exc).__name__}：{reason}"
        if reason
        else f"{context}失败：{type(exc).__name__}"
    )
    if isinstance(exc, TimeoutError):
        # 实测踩过：Windows 上 `localhost` 可能优先解析到 `::1`，而目标只监听 IPv4，
        # 结果一直挂到超时。把这条线索写进失败原因，省掉一次盲查。
        return (
            f"{detail}（连接或查询超时：确认主机与端口可达；若填的是 localhost，"
            "注意系统可能优先解析到 IPv6 而目标只监听 IPv4，可改用 127.0.0.1）"
        )
    return detail


def quote_identifier(value: str, *, quote: str, quote_close: str | None = None) -> str:
    """按方言加引号；闭合引号字符自身加倍转义，避免闭合逃逸。

    SQL Server 的 `[name]` 是不对称引号，因此闭合字符可单独指定，
    而不是依赖 `QUOTED_IDENTIFIER` 之类的会话设置。
    """
    closing = quote_close or quote
    escaped = value.replace(closing, closing * 2)
    return f"{quote}{escaped}{closing}"


def qualify(*parts: str, quote: str, quote_close: str | None = None) -> str:
    """拼接限定名，例如 `` `db`.`table` `` 或 `` [dbo].[orders] ``。"""
    return ".".join(quote_identifier(part, quote=quote, quote_close=quote_close) for part in parts)


def require_username(credentials: ConnectionCredentials, *, connector: str) -> str:
    """数据库类连接必须有用户名；缺失时明确报错而不是拿空串去连。"""
    if not credentials.username:
        raise DomainError(f"{connector} 连接缺少用户名")
    return credentials.username


def require_password(credentials: ConnectionCredentials, *, connector: str) -> str:
    if credentials.password is None:
        raise DomainError(f"{connector} 连接缺少密码")
    return credentials.password.get_secret_value()


def optional_secret(value: SecretStr | None) -> str | None:
    """读取可选的秘密字段；只在真正要交给驱动时调用。"""
    return value.get_secret_value() if value is not None else None


def row_get(row: Mapping[str, Any], key: str, default: Any = None) -> Any:
    """按列名取值，**大小写不敏感**。

    实测踩过：MySQL 8.4 的 `information_schema` 返回大写列名（`TABLE_NAME`），
    而 MariaDB / Doris 返回小写；Oracle 全大写、PostgreSQL 全小写。
    把大小写写死会在某些版本上直接 `KeyError`，因此统一走这里。
    """
    if key in row:
        return row[key]
    folded = key.casefold()
    for candidate, value in row.items():
        if str(candidate).casefold() == folded:
            return value
    return default


def cell_to_plain(value: Any) -> Any:
    """把驱动返回的特殊类型收敛成可序列化的普通值。

    只做类型归一，不做脱敏 —— 脱敏统一在 `ProfileService` 完成，保证只有一个写入入口。
    """
    if value is None or isinstance(value, str | int | float | bool):
        return value
    if isinstance(value, bytes | bytearray | memoryview):
        return bytes(value)
    # Decimal / datetime / UUID / 驱动自定义类型统一转字符串，避免 JSON 序列化失败。
    return str(value)
