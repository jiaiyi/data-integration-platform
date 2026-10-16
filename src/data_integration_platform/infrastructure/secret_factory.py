"""凭据存储的两个实现（T-14）。

`fernet-db`：本地加密库，密文落在 `encrypted_secrets.ciphertext`，明文不落盘。
`vault-kv-v2`：写入企业 Secret Provider，控制面只保存 `vault://` 引用。

两者都**失败关闭**：Provider 未就绪或引用不可解析时抛出明确错误，不放行、不降级。
"""

import hashlib
import json
import uuid
from collections.abc import Mapping
from typing import Any

import httpx
from cryptography.fernet import Fernet, InvalidToken
from pydantic import SecretStr
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from data_integration_platform.application.secrets import (
    ConnectionCredentials,
    SecretProviderPort,
    StoredSecret,
)
from data_integration_platform.config import Settings, get_settings
from data_integration_platform.domain.errors import (
    DomainError,
    ExternalServiceUnavailable,
    ResourceNotFound,
    VersionConflict,
)
from data_integration_platform.infrastructure.models import EncryptedSecret

FERNET_REF_SCHEME = "fernet://"
VAULT_REF_SCHEME = "vault://"
# 凭据载荷在存储中的单一键名：字段名本身也不外泄。
_PAYLOAD_KEY = "credentials"


def _as_mapping(value: Any) -> Mapping[str, Any]:
    """把响应里的任意嵌套节点收成映射；非映射一律当空，避免链式取值抛 TypeError。"""
    return value if isinstance(value, Mapping) else {}


def _not_ready(provider: str) -> ExternalServiceUnavailable:
    """Provider 未就绪属依赖不可用，映射 503，与业务规则违反区分开。"""
    return ExternalServiceUnavailable(f"凭据存储未就绪（{provider}），请检查部署配置")


class FernetDbSecretProvider:
    """本地加密库实现；密文入库，主密钥来自 `DIP_SECRET_MASTER_KEY`。"""

    provider_name = "fernet-db"

    def __init__(self, session: AsyncSession, key: SecretStr | None) -> None:
        """绑定会话与主密钥；密钥缺失时保留未就绪状态，在调用时才失败关闭。"""
        self._session = session
        self._key = key
        self._fernet = Fernet(key.get_secret_value().encode("utf-8")) if key is not None else None
        # key_id 只标识「用的是哪把密钥」，本身不是密钥，也不可反推密钥。
        self._key_id = (
            hashlib.sha256(key.get_secret_value().encode("utf-8")).hexdigest()[:16]
            if key is not None
            else None
        )

    async def store(
        self,
        *,
        project_id: uuid.UUID,
        name: str,
        credentials: ConnectionCredentials,
    ) -> StoredSecret:
        if self._fernet is None or self._key_id is None:
            raise _not_ready(self.provider_name)
        secret_ref = f"{FERNET_REF_SCHEME}{uuid.uuid4()}"
        payload = json.dumps(
            {_PAYLOAD_KEY: credentials.reveal_all()}, ensure_ascii=False, sort_keys=True
        )
        row = EncryptedSecret(
            project_id=project_id,
            provider=self.provider_name,
            secret_ref=secret_ref,
            ciphertext=self._fernet.encrypt(payload.encode("utf-8")),
            key_id=self._key_id,
        )
        self._session.add(row)
        await self._session.flush()
        return StoredSecret(secret_ref=secret_ref, key_id=self._key_id)

    async def load(self, secret_ref: str) -> ConnectionCredentials:
        if self._fernet is None:
            raise _not_ready(self.provider_name)
        row = await self._require_row(secret_ref)
        if row.ciphertext is None:
            raise DomainError("凭据记录缺少密文，拒绝降级读取")
        if row.key_id is not None and row.key_id != self._key_id:
            # 主密钥与密文不匹配时无法解密；明确报错，绝不返回空凭据冒充成功。
            raise VersionConflict("凭据由其他主密钥加密，请先完成密钥轮换")
        try:
            payload = json.loads(self._fernet.decrypt(row.ciphertext))
        except InvalidToken as exc:
            raise DomainError("凭据解密失败") from exc
        return ConnectionCredentials.from_payload(payload.get(_PAYLOAD_KEY, {}))

    async def destroy(self, secret_ref: str) -> None:
        row = await self._session.scalar(
            select(EncryptedSecret).where(EncryptedSecret.secret_ref == secret_ref)
        )
        if row is not None:
            await self._session.delete(row)

    async def _require_row(self, secret_ref: str) -> EncryptedSecret:
        if not secret_ref.startswith(FERNET_REF_SCHEME):
            raise DomainError("凭据引用与当前 Provider 不匹配")
        row = await self._session.scalar(
            select(EncryptedSecret).where(EncryptedSecret.secret_ref == secret_ref)
        )
        if row is None:
            raise ResourceNotFound("凭据引用不存在")
        if row.provider != self.provider_name:
            raise DomainError("凭据引用的 Provider 与当前配置不一致")
        return row


class VaultKvV2SecretProvider:
    """HashiCorp Vault KV v2 实现；控制面只持有 `vault://<mount>/<path>` 引用。"""

    provider_name = "vault-kv-v2"

    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    def _base_url(self) -> str:
        settings = self._settings
        if settings.vault_address is None or settings.vault_token is None:
            raise _not_ready(self.provider_name)
        return str(settings.vault_address).rstrip("/")

    def _headers(self) -> dict[str, str]:
        token = self._settings.vault_token
        if token is None:
            raise _not_ready(self.provider_name)
        headers = {"X-Vault-Token": token.get_secret_value()}
        if self._settings.vault_namespace:
            headers["X-Vault-Namespace"] = self._settings.vault_namespace
        return headers

    async def store(
        self,
        *,
        project_id: uuid.UUID,
        name: str,
        credentials: ConnectionCredentials,
    ) -> StoredSecret:
        path = self._path_for(project_id=project_id, name=name)
        url = f"{self._base_url()}/v1/{self._settings.vault_kv_mount}/data/{path}"
        payload = await self._request(
            "POST", url, json_body={"data": {_PAYLOAD_KEY: credentials.reveal_all()}}
        )
        version = _as_mapping(payload.get("data")).get("version")
        return StoredSecret(
            secret_ref=f"{VAULT_REF_SCHEME}{self._settings.vault_kv_mount}/{path}",
            key_id=str(version) if version is not None else None,
        )

    async def load(self, secret_ref: str) -> ConnectionCredentials:
        mount, path = self._parse_ref(secret_ref)
        url = f"{self._base_url()}/v1/{mount}/data/{path}"
        payload = await self._request("GET", url)
        inner = _as_mapping(_as_mapping(payload.get("data")).get("data"))
        if not inner:
            raise DomainError("凭据记录结构不符合 KV v2 契约")
        stored = inner.get(_PAYLOAD_KEY)
        if not isinstance(stored, dict):
            raise DomainError("凭据记录缺少有效载荷")
        return ConnectionCredentials.from_payload(dict(stored))

    async def destroy(self, secret_ref: str) -> None:
        mount, path = self._parse_ref(secret_ref)
        url = f"{self._base_url()}/v1/{mount}/data/{path}"
        try:
            await self._request("DELETE", url)
        except ResourceNotFound:
            # 销毁按幂等处理；永久销毁走 metadata 端点，属凭据生命周期，见 T-15。
            return

    def _path_for(self, *, project_id: uuid.UUID, name: str) -> str:
        safe_name = "".join(ch if ch.isalnum() or ch in "-_." else "-" for ch in name).strip("-")
        if not safe_name:
            raise DomainError("连接名称无法用于凭据路径")
        return f"{self._settings.vault_path_prefix}/{project_id}/{safe_name}"

    def _parse_ref(self, secret_ref: str) -> tuple[str, str]:
        if not secret_ref.startswith(VAULT_REF_SCHEME):
            raise DomainError("凭据引用与当前 Provider 不匹配")
        remainder = secret_ref[len(VAULT_REF_SCHEME) :]
        mount, _, path = remainder.partition("/")
        if not mount or not path:
            raise DomainError("Vault 凭据引用格式非法")
        return mount, path

    async def _request(
        self,
        method: str,
        url: str,
        *,
        json_body: dict[str, object] | None = None,
    ) -> dict[str, Any]:
        """发出一次 Vault 请求；失败信息只带状态码或异常类型，绝不含令牌与请求体。"""
        try:
            async with httpx.AsyncClient(
                timeout=self._settings.vault_timeout_seconds,
                verify=self._settings.vault_verify_tls,
                # 绝不走系统代理：Vault token 会被发给一个未知的中间人，
                # 而且内网地址经代理转发通常直接失败（实测报 502）。
                trust_env=False,
            ) as client:
                response = await client.request(
                    method, url, headers=self._headers(), json=json_body
                )
        except httpx.HTTPError as exc:
            raise ExternalServiceUnavailable(f"Vault 请求失败：{type(exc).__name__}") from exc
        if response.status_code == 404:
            raise ResourceNotFound("Vault 中不存在该凭据")
        if response.status_code >= 400:
            raise ExternalServiceUnavailable(f"Vault 返回 {response.status_code}")
        if response.status_code == 204 or not response.content:
            return {}
        parsed = response.json()
        return parsed if isinstance(parsed, dict) else {}


def create_secret_provider(session: AsyncSession) -> SecretProviderPort:
    """按配置构造凭据 Provider；Fernet 实现需要会话以读写密文表。"""
    settings = get_settings()
    if settings.secret_provider == "vault-kv-v2":
        return VaultKvV2SecretProvider(settings)
    return FernetDbSecretProvider(session, settings.secret_master_key)


def build_secret_provider(session: AsyncSession, *, provider_name: str) -> SecretProviderPort:
    """按**显式名字**构造 Provider。

    迁移场景要同时操作源与目标两个 Provider，其中目标未必是当前配置的那一个；
    目标 Provider 未就绪时在这里失败，而不是等到执行阶段才发现。
    """
    settings = get_settings()
    if provider_name == VaultKvV2SecretProvider.provider_name:
        return VaultKvV2SecretProvider(settings)
    if provider_name == FernetDbSecretProvider.provider_name:
        return FernetDbSecretProvider(session, settings.secret_master_key)
    raise DomainError(f"未支持的凭据 Provider：{provider_name}")


def provider_name_of(secret_ref: str) -> str:
    """由引用前缀判定它属于哪个 Provider。"""
    for scheme, provider_name in (
        (FERNET_REF_SCHEME, FernetDbSecretProvider.provider_name),
        (VAULT_REF_SCHEME, VaultKvV2SecretProvider.provider_name),
    ):
        if secret_ref.startswith(scheme):
            return provider_name
    raise DomainError("无法识别的凭据引用格式")


def secret_provider_for_ref(session: AsyncSession, secret_ref: str) -> SecretProviderPort:
    """按引用前缀取对应 Provider；与配置不一致时也照引用走，避免读错存储。"""
    return build_secret_provider(session, provider_name=provider_name_of(secret_ref))


def secret_provider_readiness() -> tuple[str, bool]:
    """返回（Provider 名称, 是否就绪），供 `/health` 明确报告未就绪而非静默失败。"""
    settings = get_settings()
    if settings.secret_provider == "vault-kv-v2":
        return settings.secret_provider, (
            settings.vault_address is not None and settings.vault_token is not None
        )
    return settings.secret_provider, settings.secret_master_key is not None
