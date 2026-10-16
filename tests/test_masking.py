"""保守脱敏与凭据擦除（T-13 / FR-12 / G-07）。

核心断言不是「掩码长什么样」，而是：**原始值不出现在输出里**。
"""

import pytest

from data_integration_platform.application.masking import (
    MaskRule,
    mask_cell,
    mask_rows,
    rule_for_column,
    scrub_secrets,
)


# --------------------------------------------------------- 列名判定
@pytest.mark.parametrize(
    ("column_name", "expected"),
    [
        ("email", MaskRule.EMAIL),
        ("user_email", MaskRule.EMAIL),
        ("邮箱", MaskRule.EMAIL),
        ("phone", MaskRule.PHONE),
        ("mobile_no", MaskRule.PHONE),
        ("手机号", MaskRule.PHONE),
        ("name", MaskRule.PERSON_NAME),
        ("customer_name", MaskRule.PERSON_NAME),
        ("姓名", MaskRule.PERSON_NAME),
        ("id_card", MaskRule.ID_CARD),
        ("idcard", MaskRule.ID_CARD),
        ("身份证号", MaskRule.ID_CARD),
        ("passport_no", MaskRule.ID_CARD),
        ("bank_card", MaskRule.BANK_CARD),
        ("card_no", MaskRule.BANK_CARD),
        ("password", MaskRule.SECRET),
        ("api_key", MaskRule.SECRET),
        ("access_token", MaskRule.SECRET),
        ("address", MaskRule.ADDRESS),
        ("client_ip", MaskRule.IP),
    ],
)
def test_sensitive_columns_are_detected(column_name: str, expected: MaskRule) -> None:
    assert rule_for_column(column_name) is expected


@pytest.mark.parametrize(
    "column_name",
    ["id", "amount", "created_at", "region", "status", "file_name", "table_name", "host_name"],
)
def test_non_sensitive_columns_are_not_masked(column_name: str) -> None:
    """技术元数据列名里带 `name` 但语义不是姓名，脱敏会得到无意义样本。"""
    assert rule_for_column(column_name) is MaskRule.NONE


# --------------------------------------------------------- 掩码形态
@pytest.mark.parametrize(
    ("column_name", "raw", "expected"),
    [
        # PRD §7.2 的示例口径
        ("email", "zhangsan@example.com", "z***@ex***.com"),
        ("phone", "13800001234", "138****1234"),
        ("name", "张三", "张*"),
        ("customer_name", "欧阳娜娜", "欧*"),
        ("password", "sup3r-secret", "***"),
        ("client_ip", "192.168.1.77", "192.168.1.*"),
    ],
)
def test_mask_shapes_match_prototype(column_name: str, raw: str, expected: str) -> None:
    assert mask_cell(column_name=column_name, value=raw) == expected


def test_id_card_keeps_only_prefix_and_suffix() -> None:
    """保留前 4 位与后 2 位，中段整体遮蔽且总长度不变。"""
    masked = mask_cell(column_name="id_card", value="110101199003071234")
    assert masked == f"1101{'*' * 12}34"
    assert len(masked) == 18
    assert "1990030712" not in masked


def test_bank_card_keeps_only_last_four() -> None:
    masked = mask_cell(column_name="bank_card", value="4111111111111111")
    assert masked == "************1111"


# --------------------------------------------------------- 值级兜底
def test_value_level_detection_catches_misnamed_column() -> None:
    """列名叫 `remarks` 却装着邮箱：仅靠列名判定会漏，兜底检测必须拦住。"""
    assert mask_cell(column_name="remarks", value="zhangsan@example.com") == "z***@ex***.com"
    assert mask_cell(column_name="memo", value="13800001234") == "138****1234"
    assert mask_cell(column_name="extra", value="110101199003071234") == f"1101{'*' * 12}34"


def test_value_level_detection_uses_luhn_for_bank_cards() -> None:
    """普通长数字（订单号）不应被误判成银行卡，否则样本失去意义。"""
    assert rule_for_column("order_no") is MaskRule.NONE
    assert mask_cell(column_name="order_no", value="1234567890123") == "1234567890123"


def test_plain_values_are_truncated_not_masked() -> None:
    long_text = "A" * 200
    masked = mask_cell(column_name="description", value=long_text)
    assert masked is not None
    assert len(masked) == 65
    assert masked.endswith("…")


def test_binary_values_never_reach_the_sample() -> None:
    assert mask_cell(column_name="payload", value=b"\x00\x01binary") == "***"


def test_none_stays_none() -> None:
    assert mask_cell(column_name="email", value=None) is None
    assert mask_cell(column_name="description", value=None) is None


# --------------------------------------------------------- 样本整体
def test_no_raw_value_survives_in_the_masked_sample() -> None:
    """G-07：原始值不得出现在脱敏后的样本里（这里是唯一写入入口）。"""
    raw_rows = [
        {
            "id": 1,
            "email": "zhangsan@example.com",
            "phone": "13800001234",
            "customer_name": "张三",
            "id_card": "110101199003071234",
            "password": "sup3r-secret",
        }
    ]
    masked = mask_rows(columns=list(raw_rows[0]), rows=raw_rows)
    rendered = repr(masked)
    for secret in (
        "zhangsan@example.com",
        "13800001234",
        "张三",
        "110101199003071234",
        "sup3r-secret",
    ):
        assert secret not in rendered
    # 非敏感列正常保留，样本才有诊断价值；样本值统一为字符串，便于 JSON 落库
    assert masked[0]["id"] == "1"


def test_mask_rows_keeps_declared_column_order() -> None:
    rows = [{"b": "x", "a": "y"}]
    masked = mask_rows(columns=["a", "b"], rows=rows)
    assert list(masked[0]) == ["a", "b"]


def test_mask_rows_fills_missing_columns() -> None:
    assert mask_rows(columns=["a", "b"], rows=[{"a": "1"}]) == [{"a": "1", "b": None}]


# --------------------------------------------------------- 凭据擦除
def test_scrub_secrets_removes_dsn_credentials() -> None:
    text = "connection failed: postgresql://dip_user:sup3r-secret@db.internal:5432/dip"
    scrubbed = scrub_secrets(text)
    assert "sup3r-secret" not in scrubbed
    # 保留可诊断性：主机与用户名仍在
    assert "db.internal" in scrubbed
    assert "dip_user" in scrubbed


@pytest.mark.parametrize(
    "text",
    [
        "auth failed password=hunter2 for user reader",
        "pwd: hunter2",
        "mysql error token='abc123' invalid",
        "Auth error API_KEY=sk-live-9f8e7d6c",
        'secret = "topsecret"',
    ],
)
def test_scrub_secrets_removes_keyword_credentials(text: str) -> None:
    scrubbed = scrub_secrets(text)
    for secret in ("hunter2", "abc123", "sk-live-9f8e7d6c", "topsecret"):
        assert secret not in scrubbed


def test_scrub_secrets_leaves_clean_text_untouched() -> None:
    text = "源端字段 amount 由 DECIMAL 变更为 VARCHAR，类型解析失败"
    assert scrub_secrets(text) == text
