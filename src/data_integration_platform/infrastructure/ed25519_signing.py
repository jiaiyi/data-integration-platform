"""Ed25519 分离签名的实现（T-28）。

密钥用 **base64url 编码的裸 32 字节**（私钥是种子，公钥是点）：
比 PEM 短、便于进环境变量，且不会带上一堆与算法无关的包装字段。

**公钥映射可以有多把**：轮换期间旧令牌仍要能验签，所以映射按 `key_id` 索引，
而不是只留一把当前钥匙。映射为空时验签一律失败（见 `EmptyVerifier`）——
「找不到钥匙」绝不能被当成「不需要验签」。
"""

import base64
import binascii
from collections.abc import Mapping

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)

from data_integration_platform.application.harness.signing import (
    SignatureError,
    SignerPort,
    VerifierPort,
)
from data_integration_platform.config import Settings, get_settings

_PUBLIC_KEY_BYTES = 32
_PRIVATE_KEY_BYTES = 32


def decode_key(value: str, *, expect: int = _PUBLIC_KEY_BYTES) -> bytes:
    """解出 base64url 裸密钥；长度不对就报错，不截断也不补齐。"""
    try:
        raw = base64.urlsafe_b64decode(_pad(value))
    except (binascii.Error, ValueError) as exc:
        raise SignatureError("密钥不是合法的 base64url 编码") from exc
    if len(raw) != expect:
        raise SignatureError(f"密钥长度应为 {expect} 字节，实际 {len(raw)} 字节")
    return raw


def encode_key(raw: bytes) -> str:
    """把裸密钥编码成 base64url（去掉填充，方便写进环境变量）。"""
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _pad(value: str) -> str:
    return value + "=" * (-len(value) % 4)


class Ed25519Signer(SignerPort):
    """持私钥的签发方。"""

    def __init__(self, *, key_id: str, private_key: str) -> None:
        self._key_id = key_id
        self._key = Ed25519PrivateKey.from_private_bytes(
            decode_key(private_key, expect=_PRIVATE_KEY_BYTES)
        )

    @property
    def key_id(self) -> str:
        return self._key_id

    def sign(self, payload: bytes) -> str:
        return encode_key(self._key.sign(payload))


class Ed25519KeyRingVerifier(VerifierPort):
    """只挂公钥映射的验签方；**没有私钥，因此签不出来**。"""

    def __init__(self, public_keys: Mapping[str, Ed25519PublicKey]) -> None:
        self._keys = dict(public_keys)

    @property
    def key_ids(self) -> frozenset[str]:
        return frozenset(self._keys)

    def knows(self, key_id: str) -> bool:
        return key_id in self._keys

    def verify(self, payload: bytes, *, signature: str, key_id: str) -> None:
        key = self._keys.get(key_id)
        if key is None:
            # 失败关闭：钥匙不在映射里就是验不过，绝不退化成「不验签」。
            raise SignatureError(f"未登记的公钥：{key_id!r}")
        try:
            key.verify(decode_key(signature, expect=64), payload)
        except InvalidSignature as exc:
            raise SignatureError("签名与载荷不匹配") from exc


def parse_public_keys(raw: str) -> dict[str, Ed25519PublicKey]:
    """解析 `key_id:base64url,key_id:base64url` 形式的公钥映射。

    解析失败一律抛出：公钥配置写错必须让进程**起不来**，
    而不是悄悄少一把钥匙、让某些令牌突然验不过。
    """
    keys: dict[str, Ed25519PublicKey] = {}
    for item in (part.strip() for part in raw.split(",")):
        if not item:
            continue
        key_id, separator, value = item.partition(":")
        if not separator or not key_id or not value:
            raise SignatureError(f"公钥映射条目格式应为 `key_id:base64url`：{item!r}")
        if key_id in keys:
            raise SignatureError(f"公钥映射里 key_id 重复：{key_id!r}")
        keys[key_id] = Ed25519PublicKey.from_public_bytes(decode_key(value))
    return keys


def create_capability_signer(*, settings: Settings | None = None) -> SignerPort:
    """本进程能否签发，取决于有没有私钥；没有就给一个「签发必失败」的实现。"""
    resolved = settings if settings is not None else get_settings()
    private_key = resolved.harness_signing_private_key
    if private_key is None:
        from data_integration_platform.application.harness.signing import NullSigner

        return NullSigner()
    return Ed25519Signer(
        key_id=resolved.harness_signing_key_id,
        private_key=private_key.get_secret_value(),
    )


def create_capability_verifier(*, settings: Settings | None = None) -> VerifierPort:
    """本进程能验哪些钥匙，取决于公钥映射；映射为空则验签一律失败。

    空映射与「钥匙没登记」给的是**不同的**错误：前者是部署漏配（忘配公钥），
    后者是拿了不属于本环境签发的令牌。混成一句话会让排查方向全错。
    """
    resolved = settings if settings is not None else get_settings()
    keys = parse_public_keys(resolved.harness_signing_public_keys)
    if not keys:
        from data_integration_platform.application.harness.signing import EmptyVerifier

        return EmptyVerifier()
    return Ed25519KeyRingVerifier(keys)


def generate_keypair() -> tuple[str, str]:
    """生成一对密钥，返回 `(私钥, 公钥)` 的 base64url 表示；供部署与测试使用。

    刻意不提供「自动生成并落盘」的路径：签名密钥凭空出现在运行目录里，
    等于把信任根交给文件系统权限。密钥由部署方显式提供。
    """
    private = Ed25519PrivateKey.generate()
    from cryptography.hazmat.primitives.serialization import (
        Encoding,
        NoEncryption,
        PrivateFormat,
        PublicFormat,
    )

    seed = private.private_bytes(Encoding.Raw, PrivateFormat.Raw, NoEncryption())
    public = private.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
    return encode_key(seed), encode_key(public)
