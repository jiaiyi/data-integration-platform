"""凭据访问端口与值对象（T-14 / FR-16）。

边界：
- 凭据**永不**进入模型上下文、API 响应、审计与普通日志；控制面只持有 `secret_ref` 引用。
- 所有秘密字段用 `SecretStr` 包裹，使 repr / 日志 / 异常里天然只出现掩码。
- 具名实现见 `infrastructure/secret_factory.py`（Fernet-DB 与 Vault KV v2 两种）。
"""

import uuid
from dataclasses import dataclass
from typing import Any, Protocol

from pydantic import BaseModel, ConfigDict, SecretStr

# 载荷字段名与取值类型的单一口径，供两个 Provider 共用。
_PAYLOAD_FIELDS = (
    "username",
    "password",
    "auth_header_name",
    "auth_header_value",
    "access_key",
    "secret_key",
    "session_token",
)


class ConnectionCredentials(BaseModel):
    """连接凭据。

    REST API 的鉴权头名（如 `Authorization`）不是秘密，头值才是（T-17 / 决策 D-Q2）；
    S3/MinIO 用 access_key + secret_key（T-16 / GAP-02）。
    """

    model_config = ConfigDict(extra="forbid")

    username: str | None = None
    password: SecretStr | None = None
    auth_header_name: str | None = None
    auth_header_value: SecretStr | None = None
    access_key: str | None = None
    secret_key: SecretStr | None = None
    session_token: SecretStr | None = None

    def redacted(self) -> dict[str, Any]:
        """只保留非秘密字段与「是否已配置」的事实，可安全进日志与审计。"""
        return {
            "username": self.username,
            "auth_header_name": self.auth_header_name,
            "access_key": self.access_key,
            "has_password": self.password is not None,
            "has_auth_header_value": self.auth_header_value is not None,
            "has_secret_key": self.secret_key is not None,
            "has_session_token": self.session_token is not None,
        }

    def reveal_all(self) -> dict[str, str | None]:
        """展开全部秘密字段。

        **仅允许**在加密写入存储、或刚从存储读出并立即使用时调用；
        返回值不得进入日志、响应、审计或异常信息。
        """
        revealed: dict[str, str | None] = {}
        for field in _PAYLOAD_FIELDS:
            value = getattr(self, field)
            revealed[field] = value.get_secret_value() if isinstance(value, SecretStr) else value
        return revealed

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> "ConnectionCredentials":
        """从存储载荷还原凭据；未知字段一律忽略，避免旧数据把新代码打挂。"""
        known = {field: payload.get(field) for field in _PAYLOAD_FIELDS}
        return cls.model_validate(known)


@dataclass(frozen=True, slots=True)
class StoredSecret:
    """一次凭据写入的结果。`key_id` 标识加密密钥版本，供后续轮换识别（T-15）。"""

    secret_ref: str
    key_id: str | None = None


class SecretProviderPort(Protocol):
    """凭据存储端口。"""

    provider_name: str

    async def store(
        self,
        *,
        project_id: uuid.UUID,
        name: str,
        credentials: ConnectionCredentials,
    ) -> StoredSecret:
        """加密保存凭据并返回引用；明文不落盘。"""
        ...

    async def load(self, secret_ref: str) -> ConnectionCredentials:
        """按引用读出凭据；引用不存在或与 Provider 不匹配时失败，不降级放行。"""
        ...

    async def destroy(self, secret_ref: str) -> None:
        """销毁凭据；引用不存在按幂等处理。"""
        ...
