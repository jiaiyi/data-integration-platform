"""分离签名的端口（T-28 / FR-45）。

**私钥与公钥分开持有**是这一层的全部意义（SPEC §8.1）：签发方（API / 授权服务）
拿私钥，执行方（Worker）只挂一张 `key_id → 公钥` 的映射，因此 Worker **在结构上
无法自己签发**能力令牌 —— 不是靠约定或代码评审，而是它手里根本没有那把钥匙。

签名面向**规范 JSON 的字节**（`canonical_payload()` 的序列化结果），不是面向对象：
对象的属性顺序、字典遍历顺序都不能影响签名与验签结果。

这里只放端口与算法名，具体实现（`cryptography`）在
`infrastructure/ed25519_signing.py`。
"""

from collections.abc import Mapping
from typing import Protocol

from data_integration_platform.domain.harness import canonical_json

# 算法名会随令牌一起留存（`claims_signature_algorithm` / `signature_algorithm` 列）：
# 将来换算法时，历史令牌仍能按它签发时的算法解释。
SIGNATURE_ALGORITHM = "Ed25519"


class SignatureError(Exception):
    """验签失败的统一出口。

    刻意**不**继承 `DomainError`：验签失败是安全边界上的拒绝，调用方要能把它
    与普通业务规则违反分开处理（前者一律失败关闭，且不该被当成参数错误回话）。
    """


class SignerPort(Protocol):
    """签发方持有的私钥能力；Worker 侧不应实现它。"""

    @property
    def key_id(self) -> str: ...

    def sign(self, payload: bytes) -> str:
        """对规范载荷字节签名，返回 base64url 编码的签名。"""
        ...


class VerifierPort(Protocol):
    """执行方持有的公钥映射；**只验不签**。"""

    def verify(self, payload: bytes, *, signature: str, key_id: str) -> None:
        """验签失败抛 `SignatureError`；成功返回 `None`。"""
        ...

    def knows(self, key_id: str) -> bool:
        """这把钥匙在不在映射里。缺钥匙必须失败关闭，不能当作「无需验签」。"""
        ...


class NullSigner:
    """没有配置私钥时的签发方：**任何签发都失败**。

    不做「无签名令牌」的降级 —— 那是把「没有钥匙」变成「不需要钥匙」。
    """

    key_id = ""

    def sign(self, payload: bytes) -> str:
        raise SignatureError("本进程未配置签名私钥，无法签发能力令牌")


class EmptyVerifier:
    """没有配置任何公钥时的验签方：任何验签都失败（失败关闭）。"""

    def verify(self, payload: bytes, *, signature: str, key_id: str) -> None:
        raise SignatureError(f"未配置任何公钥，无法验证 key_id={key_id!r}")

    def knows(self, key_id: str) -> bool:
        return False


def signing_payload(canonical: Mapping[str, object]) -> bytes:
    """把规范载荷转成待签名字节。

    签名对象是**规范 JSON 的 UTF-8 字节**：`canonical_json` 已经保证键有序、
    无多余空白，因此同一份声明体在任何进程里都得到同一串字节。
    """
    return canonical_json(canonical).encode("utf-8")
