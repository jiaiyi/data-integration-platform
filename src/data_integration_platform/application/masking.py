"""保守脱敏与受限样本（T-13 / FR-12）。

两条判定路径，**取更保守的一方**：
1. 按列名匹配敏感规则（邮箱 / 手机 / 姓名 / 证件号 / 银行卡 / 地址 / 密钥 / IP）；
2. 列名没命中时，再按**取值形态**兜底检测——避免列名叫 `remarks`、`memo` 这种
   却装着邮箱或身份证号的字段直接落盘。

目标不是「尽量多留信息」，而是 G-07：**敏感明文泄露 = 0**。因此这里宁可过度脱敏。
样本脱敏后才入库，原文不落盘。
"""

import re
from collections.abc import Callable, Mapping, Sequence
from enum import StrEnum
from typing import Any

# 非敏感列的取值上限：样本只用于让模型理解形态，不需要长文本。
_MAX_PLAIN_LENGTH = 64

_EMAIL = re.compile(r"^[^@\s]{1,64}@[^@\s]{1,255}$")
_MOBILE_CN = re.compile(r"^1[3-9]\d{9}$")
_DIGITS = re.compile(r"\D")
_IPV4 = re.compile(r"^(\d{1,3})\.(\d{1,3})\.(\d{1,3})\.(\d{1,3})$")
# 18 位身份证：6 位地址码 + 8 位出生日期 + 3 位顺序码 + 1 位校验位（可为 X）。
_ID_CARD_18 = re.compile(r"^\d{17}[\dXx]$")
_CJK_NAME = re.compile(r"^[一-鿿]{2,6}$")


class MaskRule(StrEnum):
    """脱敏规则；`NONE` 表示该列不属敏感字段。"""

    NONE = "NONE"
    EMAIL = "EMAIL"
    PHONE = "PHONE"
    PERSON_NAME = "PERSON_NAME"
    ID_CARD = "ID_CARD"
    BANK_CARD = "BANK_CARD"
    ADDRESS = "ADDRESS"
    SECRET = "SECRET"
    IP = "IP"
    BINARY = "BINARY"


# 顺序即优先级：越靠前越先命中，`SECRET` 必须最先（密码列不该被当成别的规则）。
# `pass` 必须加词边界：否则 `passport_no` 会被当成密码列，护照号反而漏掉证件号规则。
_COLUMN_RULES: tuple[tuple[MaskRule, re.Pattern[str]], ...] = (
    (
        MaskRule.SECRET,
        re.compile(
            r"(?<![a-z])pass(word|wd)?(?![a-z])"
            r"|(?<![a-z])pwd(?![a-z])"
            r"|secret|token|api[_-]?key|private[_-]?key|证书|密钥|口令"
        ),
    ),
    (MaskRule.EMAIL, re.compile(r"e[_-]?mail|邮箱|邮件")),
    (MaskRule.PHONE, re.compile(r"(?<![a-z])phone|mobile|tel(ephone)?|手机|电话|联系方式")),
    (
        MaskRule.ID_CARD,
        re.compile(
            r"id[_-]?card|idcard|id[_-]?no|id[_-]?number|identity|身份证|证件号|护照|passport"
        ),
    ),
    (
        MaskRule.BANK_CARD,
        re.compile(r"bank[_-]?card|card[_-]?no|card[_-]?number|acct[_-]?no|账号|银行卡|卡号"),
    ),
    (MaskRule.ADDRESS, re.compile(r"address|addr|住址|地址|收货")),
    (MaskRule.IP, re.compile(r"(?<![a-z])ip([_-]?addr(ess)?)?|client[_-]?ip|客户端ip")),
    (
        MaskRule.PERSON_NAME,
        re.compile(
            r"^(name|full[_-]?name|real[_-]?name|user[_-]?name|customer[_-]?name|contact[_-]?name|姓名|联系人|客户名)$"
        ),
    ),
)

# 名字里含 `name` 但语义是技术元数据，按姓名脱敏会得到无意义的样本。
_TECHNICAL_NAME_COLUMNS = frozenset(
    {
        "file_name",
        "filename",
        "table_name",
        "tablename",
        "column_name",
        "col_name",
        "host_name",
        "hostname",
        "db_name",
        "database_name",
        "schema_name",
        "index_name",
        "type_name",
        "metric_name",
        "job_name",
        "task_name",
        "event_name",
        "log_name",
        "partition_name",
    }
)


def rule_for_column(column_name: str) -> MaskRule:
    """按列名判定脱敏规则；判定不出敏感语义时返回 `NONE`，交由取值兜底检测。"""
    normalized = column_name.strip().casefold()
    if not normalized:
        return MaskRule.NONE
    if normalized in _TECHNICAL_NAME_COLUMNS:
        return MaskRule.NONE
    for rule, pattern in _COLUMN_RULES:
        if pattern.search(normalized):
            return rule
    return MaskRule.NONE


def _mask_email(value: str) -> str:
    match = _EMAIL.match(value.strip())
    if match is None:
        return "***"
    local, domain = match.group(0).split("@", 1)
    labels = domain.split(".")
    # 保留域名主体前 2 位与完整顶级域，与原型示例 `a***@ex***.com` 一致。
    head = labels[0][:2]
    tail = f".{'.'.join(labels[1:])}" if len(labels) > 1 else ""
    return f"{local[:1]}***@{head}***{tail}"


def _mask_phone(value: str) -> str:
    digits = _DIGITS.sub("", value)
    if not digits:
        return "***"
    if _MOBILE_CN.match(digits):
        # 原型示例：138****1234
        return f"{digits[:3]}****{digits[-4:]}"
    if len(digits) <= 5:
        return "***"
    return f"{digits[:3]}{'*' * (len(digits) - 5)}{digits[-2:]}"


def _mask_person_name(value: str) -> str:
    text = value.strip()
    if not text:
        return "***"
    if _CJK_NAME.match(text):
        # 原型示例：张*
        return f"{text[0]}*"
    return f"{text[0]}***"


def _mask_id_card(value: str) -> str:
    text = value.strip()
    if len(text) < 8:
        return "***"
    # 保留前 4 位（地址码前缀）与后 2 位，中段整体遮蔽。
    return f"{text[:4]}{'*' * (len(text) - 6)}{text[-2:]}"


def _mask_bank_card(value: str) -> str:
    digits = _DIGITS.sub("", value)
    if len(digits) < 8:
        return "***"
    return f"{'*' * (len(digits) - 4)}{digits[-4:]}"


def _mask_address(value: str) -> str:
    text = value.strip()
    if not text:
        return "***"
    return f"{text[:3]}***"


def _mask_ip(value: str) -> str:
    match = _IPV4.match(value.strip())
    if match is None:
        return "***"
    return f"{match.group(1)}.{match.group(2)}.{match.group(3)}.*"


_MASKERS: dict[MaskRule, Callable[[str], str]] = {
    MaskRule.EMAIL: _mask_email,
    MaskRule.PHONE: _mask_phone,
    MaskRule.PERSON_NAME: _mask_person_name,
    MaskRule.ID_CARD: _mask_id_card,
    MaskRule.BANK_CARD: _mask_bank_card,
    MaskRule.ADDRESS: _mask_address,
    MaskRule.IP: _mask_ip,
    MaskRule.SECRET: lambda _value: "***",
    MaskRule.BINARY: lambda _value: "***",
}


def detect_value_rule(value: str) -> MaskRule:
    """按取值形态兜底判定；无法判定时返回 `NONE`。"""
    text = value.strip()
    if not text:
        return MaskRule.NONE
    if _EMAIL.match(text):
        return MaskRule.EMAIL
    digits = _DIGITS.sub("", text)
    if _MOBILE_CN.match(digits) and len(digits) == len(text):
        return MaskRule.PHONE
    if _ID_CARD_18.match(text):
        return MaskRule.ID_CARD
    if 12 <= len(digits) <= 19 and len(digits) == len(text) and _luhn_valid(digits):
        return MaskRule.BANK_CARD
    return MaskRule.NONE


def _luhn_valid(digits: str) -> bool:
    """Luhn 校验；用于区分银行卡号与普通长数字（订单号、流水号）。"""
    total = 0
    for index, char in enumerate(reversed(digits)):
        digit = int(char)
        if index % 2 == 1:
            digit *= 2
            if digit > 9:
                digit -= 9
        total += digit
    return total % 10 == 0


def _mask_plain(value: Any) -> str | None:
    """非敏感列的取值：只做长度截断与类型归一，便于模型理解字段形态。"""
    if value is None:
        return None
    if isinstance(value, bytes | bytearray | memoryview):
        return "***"
    text = value if isinstance(value, str) else str(value)
    text = text.replace("\r", " ").replace("\n", " ").strip()
    if len(text) > _MAX_PLAIN_LENGTH:
        return f"{text[:_MAX_PLAIN_LENGTH]}…"
    return text


def mask_cell(*, column_name: str, value: Any) -> str | None:
    """脱敏单个单元格：列名规则优先，未命中时按取值形态兜底。"""
    if isinstance(value, bytes | bytearray | memoryview):
        return "***"
    rule = rule_for_column(column_name)
    if rule is MaskRule.NONE and isinstance(value, str):
        rule = detect_value_rule(value)
    if rule is MaskRule.NONE:
        return _mask_plain(value)
    if value is None:
        return None
    masker = _MASKERS[rule]
    return masker(value if isinstance(value, str) else str(value))


def mask_rows(
    *,
    columns: Sequence[str],
    rows: Sequence[Mapping[str, Any]],
) -> list[dict[str, str | None]]:
    """把受限样本按列逐格脱敏；列顺序固定，缺失值记 `None`。"""
    return [
        {column: mask_cell(column_name=column, value=row.get(column)) for column in columns}
        for row in rows
    ]


# 连接串里的 `user:password@host` 与 `password=...` 是驱动异常最常夹带的明文来源。
_DSN_CREDENTIALS = re.compile(
    r"(?P<scheme>[a-z0-9+.\-]+://)(?P<user>[^:/@\s]+):(?P<secret>[^@/\s]+)@",
    re.IGNORECASE,
)
_KEYWORD_SECRETS = re.compile(
    r"(?i)\b(pass(word|wd)?|pwd|secret|token|api[_-]?key)\b\s*[=:]\s*(\"[^\"]*\"|'[^']*'|\S+)"
)


def scrub_secrets(text: str) -> str:
    """从任意文本中擦除疑似凭据。

    用于把驱动异常安全地转成用户可见的失败原因：**保留可诊断性，去掉秘密**
    （FR-08：失败信息只说明失败原因，不含密码明文）。
    """
    scrubbed = _DSN_CREDENTIALS.sub(r"\g<scheme>\g<user>:***@", text)
    return _KEYWORD_SECRETS.sub(lambda m: f"{m.group(1)}=***", scrubbed)
