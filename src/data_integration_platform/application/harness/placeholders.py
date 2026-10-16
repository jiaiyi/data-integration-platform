"""配置里的凭据占位符（FR-17 的落地侧）。

模型写 HOCON 时会用 `${MYSQL_PASSWORD}` 这类占位符，但**名字是它自己编的**，
平台不知道它指哪把凭据。所以这里把名字固定成一条机械规则，并把可用名字
**在生成阶段就告诉模型**，两边用的是同一份清单：

```
<角色>_<连接器>_<字段>      例：SOURCE_MYSQL_PASSWORD、SINK_DORIS_HOST
```

带上角色前缀是为了消歧：源端和目标端都可能是 MySQL，`${MYSQL_PASSWORD}` 就说不清
用的是哪一个。名字长得啰嗦，但它是给机器比对用的，不是给人念的。

**认不出的占位符一律拒绝，而不是原样交给引擎**：实测引擎只会从 JVM 系统属性解析
`${X}`，交给它等于把字面量 `${MYSQL_PASSWORD}` 当口令去连库 —— 要么连不上，
要么在日志里留下一串看起来像密钥的东西。
"""

import re
from collections.abc import Mapping
from dataclasses import dataclass

from data_integration_platform.application.secrets import ConnectionCredentials
from data_integration_platform.domain.enums import ConnectorType, DataEndpointRole
from data_integration_platform.domain.errors import DomainError
from data_integration_platform.infrastructure.models import DataConnection

# 只认裸名字：`${...}` 里出现点号、冒号、嵌套都会在下面的白名单比对里被拒。
PLACEHOLDER_PATTERN = re.compile(r"\$\{([^{}]*)\}")

# 字段名 → `ConnectionCredentials` 上的属性。值是秘密的那几个要显式取明值。
_CREDENTIAL_FIELDS: Mapping[str, str] = {
    "USER": "username",
    "PASSWORD": "password",
    "ACCESS_KEY": "access_key",
    "SECRET_KEY": "secret_key",
    "SESSION_TOKEN": "session_token",
    "AUTH_HEADER_NAME": "auth_header_name",
    "AUTH_HEADER_VALUE": "auth_header_value",
}

# 每类连接器**允许出现**的字段名。生成阶段与执行阶段读的是同一份 ——
# 两边各写一遍的话，模型照着清单写出来的名字到了执行阶段就成了「未登记」。
_SQL_FIELDS = ("HOST", "PORT", "DATABASE", "USER", "PASSWORD")
CONNECTOR_FIELDS: Mapping[ConnectorType, tuple[str, ...]] = {
    ConnectorType.MYSQL: _SQL_FIELDS,
    ConnectorType.POSTGRESQL: _SQL_FIELDS,
    ConnectorType.ORACLE: _SQL_FIELDS,
    ConnectorType.SQL_SERVER: _SQL_FIELDS,
    ConnectorType.DORIS: _SQL_FIELDS,
    ConnectorType.CLICKHOUSE: _SQL_FIELDS,
    ConnectorType.HIVE: _SQL_FIELDS,
    ConnectorType.HDFS: ("HOST", "PORT"),
    ConnectorType.S3_MINIO: ("HOST", "PORT", "ACCESS_KEY", "SECRET_KEY", "SESSION_TOKEN"),
    ConnectorType.REST_API: ("HOST", "PORT", "AUTH_HEADER_NAME", "AUTH_HEADER_VALUE"),
    # 本地文件没有凭据可谈：给了名字只会诱使模型去写占位符。
    ConnectorType.LOCAL_FILE: (),
}


@dataclass(frozen=True, slots=True)
class PlaceholderTable:
    """一份「可用占位符名 → 取值」的表。

    `values` 只在 Worker 的单次运行作用域里存在；它**不进日志、不进审计、不进证据**。
    """

    values: Mapping[str, str]

    def names(self) -> frozenset[str]:
        return frozenset(self.values)

    def substitute(self, hocon: str) -> str:
        """把占位符换成真实值；出现表外的名字就拒绝。"""
        unknown = self.unknown_in(hocon)
        if unknown:
            raise DomainError(
                "配置里出现了平台未登记的凭据占位符："
                + "、".join(f"${{{name}}}" for name in sorted(unknown))
                + "。可用名字见生成阶段给出的清单"
            )
        return PLACEHOLDER_PATTERN.sub(lambda m: self.values[m.group(1)], hocon)

    def unknown_in(self, hocon: str) -> frozenset[str]:
        return frozenset(
            match.group(1)
            for match in PLACEHOLDER_PATTERN.finditer(hocon)
            if match.group(1) not in self.values
        )

    def redacted(self) -> dict[str, bool]:
        """只报「有哪些名字」，不含值 —— 这个名字清单可以安全进日志。"""
        return {name: True for name in sorted(self.values)}


def placeholder_name(*, role: DataEndpointRole, connector_type: ConnectorType, field: str) -> str:
    return f"{role.value}_{connector_type.value}_{field}".upper()


def declared_names(
    *,
    role: DataEndpointRole,
    connector_type: ConnectorType,
    has_credentials: bool,
) -> frozenset[str]:
    """这条连接**声明**会用到的占位符名字。

    没登记凭据时不声明 USER/PASSWORD 这类名字：声明了却取不到值，模型照着写出来的配置
    要到执行阶段才被拒，那时已经花掉一次生成与一轮审批。

    生成阶段与执行阶段读的是**同一份** `CONNECTOR_FIELDS` —— 两边各写一遍的话，
    模型照着清单写出来的名字到了执行阶段就成了「未登记」。
    """
    fields = CONNECTOR_FIELDS.get(connector_type, ())
    return frozenset(
        placeholder_name(role=role, connector_type=connector_type, field=field)
        for field in fields
        if field not in _CREDENTIAL_FIELDS or has_credentials
    )


def build_placeholder_table(
    *,
    role: DataEndpointRole,
    connection: DataConnection,
    credentials: ConnectionCredentials | None,
) -> PlaceholderTable:
    """按一条连接导出它的可用占位符。

    连接上登记的非秘密字段总是导出；凭据只在**确实配了**的时候导出 ——
    导出空值等于把一个空口令递给模型，它写出来的配置会连不上，而错误要到执行时才暴露。
    """
    values: dict[str, str] = {}
    for field in CONNECTOR_FIELDS.get(connection.connector_type, ()):
        raw: object = None
        if field == "HOST":
            raw = connection.host
        elif field == "PORT":
            raw = connection.port
        elif field == "DATABASE":
            raw = connection.database_name
        elif credentials is not None:
            raw = getattr(credentials, _CREDENTIAL_FIELDS[field], None)
        if raw is None or raw == "":
            continue
        # `SecretStr` 要显式取明值，否则替换进去的是 `**********`。
        values[
            placeholder_name(role=role, connector_type=connection.connector_type, field=field)
        ] = raw.get_secret_value() if hasattr(raw, "get_secret_value") else str(raw)
    return PlaceholderTable(values=values)
