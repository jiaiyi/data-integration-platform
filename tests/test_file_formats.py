"""受管文件格式的模式推断（T-16 / FR-09）。

九种格式共用同一契约：字段清单 + 行数 + 受限样本。重点验证两件事：
1. **基线四项与超集五项都通过**（PRD §8.4 决策 Q6：两者均纳入验收）；
2. **采样有界** —— 样本行数受 `sample_limit` 约束，行数走元数据而非全量读取。
"""

import io
from typing import Any

import openpyxl
import pyarrow as pa
import pyarrow.parquet as pa_parquet
import pyorc
import pypdf
import pytest

from data_integration_platform.domain.enums import FileAssetFormat
from data_integration_platform.domain.errors import DomainError
from data_integration_platform.infrastructure.file_formats import (
    detect_format,
    inspect_file,
    supported_formats,
)

TOTAL_ROWS = 1_000
SAMPLE_LIMIT = 3


def build_csv() -> bytes:
    lines = ["id,name,email,amount"]
    for index in range(1, TOTAL_ROWS + 1):
        lines.append(f"{index},张{index},user{index}@example.com,{index}.50")
    return "\n".join(lines).encode("utf-8")


def build_json() -> bytes:
    records = [{"id": index, "email": f"user{index}@example.com"} for index in range(TOTAL_ROWS)]
    import json

    return json.dumps(records, ensure_ascii=False).encode("utf-8")


def build_jsonl() -> bytes:
    import json

    lines = [
        json.dumps({"id": index, "email": f"user{index}@example.com"}, ensure_ascii=False)
        for index in range(TOTAL_ROWS)
    ]
    return ("\n".join(lines) + "\n").encode("utf-8")


def build_xlsx() -> bytes:
    workbook = openpyxl.Workbook()
    sheet = workbook.active
    sheet.append(["id", "email", "amount"])
    for index in range(1, TOTAL_ROWS + 1):
        sheet.append([index, f"user{index}@example.com", index * 1.5])
    buffer = io.BytesIO()
    workbook.save(buffer)
    workbook.close()
    return buffer.getvalue()


def build_parquet() -> bytes:
    table = pa.table(
        {
            "id": pa.array(range(TOTAL_ROWS), type=pa.int64()),
            "email": pa.array([f"user{i}@example.com" for i in range(TOTAL_ROWS)]),
        }
    )
    buffer = io.BytesIO()
    pa_parquet.write_table(table, buffer)
    return buffer.getvalue()


def build_orc() -> bytes:
    buffer = io.BytesIO()
    with pyorc.Writer(buffer, "struct<id:int,email:string>") as writer:
        for index in range(TOTAL_ROWS):
            writer.write((index, f"user{index}@example.com"))
    return buffer.getvalue()


def build_xml() -> bytes:
    records = "".join(
        f'<order id="{index}"><email>user{index}@example.com</email>'
        f"<amount>{index}.50</amount></order>"
        for index in range(1, TOTAL_ROWS + 1)
    )
    return f"<orders>{records}</orders>".encode()


def build_markdown() -> bytes:
    lines = ["# 订单", "", "| id | email | amount |", "| --- | --- | --- |"]
    for index in range(1, TOTAL_ROWS + 1):
        lines.append(f"| {index} | user{index}@example.com | {index}.50 |")
    return "\n".join(lines).encode("utf-8")


def build_pdf() -> bytes:
    writer = pypdf.PdfWriter()
    writer.add_blank_page(width=200, height=200)
    writer.add_blank_page(width=200, height=200)
    buffer = io.BytesIO()
    writer.write(buffer)
    return buffer.getvalue()


BUILDERS: dict[FileAssetFormat, Any] = {
    FileAssetFormat.CSV: build_csv,
    FileAssetFormat.JSON: build_json,
    FileAssetFormat.JSONL: build_jsonl,
    FileAssetFormat.XLSX: build_xlsx,
    FileAssetFormat.PARQUET: build_parquet,
    FileAssetFormat.ORC: build_orc,
    FileAssetFormat.XML: build_xml,
    FileAssetFormat.MARKDOWN: build_markdown,
    FileAssetFormat.PDF: build_pdf,
}


def test_format_scope_covers_baseline_and_superset() -> None:
    """基线四项（CSV/Excel/JSON/Parquet）与超集五项（JSONL/ORC/XML/Markdown/PDF）都在范围内。"""
    assert supported_formats() == frozenset(FileAssetFormat)


@pytest.mark.parametrize("file_format", list(FileAssetFormat))
def test_every_format_returns_a_schema(file_format: FileAssetFormat) -> None:
    """PRD §8.4：九种格式都通过同一探查契约，任一不可用即视为验收未通过。"""
    inspection = inspect_file(
        BUILDERS[file_format](), file_format=file_format, sample_limit=SAMPLE_LIMIT
    )
    assert inspection.columns, f"{file_format.value} 未产出字段清单"
    assert [column.ordinal for column in inspection.columns] == list(
        range(1, len(inspection.columns) + 1)
    )


@pytest.mark.parametrize(
    ("file_format", "expected_columns"),
    [
        (FileAssetFormat.CSV, ["id", "name", "email", "amount"]),
        (FileAssetFormat.JSON, ["id", "email"]),
        (FileAssetFormat.JSONL, ["id", "email"]),
        (FileAssetFormat.XLSX, ["id", "email", "amount"]),
        (FileAssetFormat.PARQUET, ["id", "email"]),
        (FileAssetFormat.ORC, ["id", "email"]),
        (FileAssetFormat.XML, ["@id", "email", "amount"]),
        (FileAssetFormat.MARKDOWN, ["id", "email", "amount"]),
        (FileAssetFormat.PDF, ["content"]),
    ],
)
def test_column_names(file_format: FileAssetFormat, expected_columns: list[str]) -> None:
    inspection = inspect_file(
        BUILDERS[file_format](), file_format=file_format, sample_limit=SAMPLE_LIMIT
    )
    assert [column.name for column in inspection.columns] == expected_columns


@pytest.mark.parametrize(
    ("file_format", "expected_rows"),
    [
        (FileAssetFormat.CSV, TOTAL_ROWS),
        (FileAssetFormat.JSON, TOTAL_ROWS),
        (FileAssetFormat.JSONL, TOTAL_ROWS),
        (FileAssetFormat.XLSX, TOTAL_ROWS),
        # 行数取自 Parquet 元数据，无需解码数据 —— 这正是「不用无界 COUNT」的做法。
        (FileAssetFormat.PARQUET, TOTAL_ROWS),
        (FileAssetFormat.XML, TOTAL_ROWS),
        (FileAssetFormat.MARKDOWN, TOTAL_ROWS),
    ],
)
def test_row_count_is_reported_without_full_scan(
    file_format: FileAssetFormat, expected_rows: int
) -> None:
    inspection = inspect_file(
        BUILDERS[file_format](), file_format=file_format, sample_limit=SAMPLE_LIMIT
    )
    assert inspection.row_count == expected_rows


@pytest.mark.parametrize(
    "file_format",
    [
        FileAssetFormat.CSV,
        FileAssetFormat.JSON,
        FileAssetFormat.JSONL,
        FileAssetFormat.XLSX,
        FileAssetFormat.PARQUET,
        FileAssetFormat.ORC,
        FileAssetFormat.XML,
        FileAssetFormat.MARKDOWN,
    ],
)
def test_sample_is_bounded_by_sample_limit(file_format: FileAssetFormat) -> None:
    """受限采样：1000 行的文件只取 3 行样本，绝不把整表放进控制面。"""
    inspection = inspect_file(
        BUILDERS[file_format](), file_format=file_format, sample_limit=SAMPLE_LIMIT
    )
    assert len(inspection.sample_rows) == SAMPLE_LIMIT
    assert set(inspection.sample_rows[0]) <= {column.name for column in inspection.columns}


def test_pdf_has_no_sample() -> None:
    """PDF 正文可能整段是个人信息，值级兜底检测对长文本不可靠，因此不产出样本。"""
    inspection = inspect_file(build_pdf(), file_format=FileAssetFormat.PDF, sample_limit=5)
    assert inspection.sample_rows == ()
    assert inspection.statistics["pages"] == 2
    assert inspection.statistics["representation"] == "pdf-text"


def test_orc_reports_no_row_count_instead_of_guessing() -> None:
    """ORC 的稳定 API 不提供行数；留空而不是拿样本行数冒充满行数。"""
    inspection = inspect_file(build_orc(), file_format=FileAssetFormat.ORC, sample_limit=2)
    assert inspection.row_count is None
    assert len(inspection.sample_rows) == 2


def test_parquet_type_information_comes_from_schema_not_values() -> None:
    inspection = inspect_file(build_parquet(), file_format=FileAssetFormat.PARQUET, sample_limit=1)
    types = {column.name: column.data_type for column in inspection.columns}
    assert types["id"] == "int64"


def test_semicolon_delimited_csv_is_sniffed() -> None:
    inspection = inspect_file(
        b"id;email\n1;a@b.com\n2;c@d.com\n", file_format=FileAssetFormat.CSV, sample_limit=10
    )
    assert [column.name for column in inspection.columns] == ["id", "email"]
    assert inspection.row_count == 2


def test_gb18030_csv_is_decoded() -> None:
    """中文数据的常见编码；解码失败会让样本变成乱码而不是明文。"""
    data = "编号,姓名\n1,张三\n".encode("gb18030")
    inspection = inspect_file(data, file_format=FileAssetFormat.CSV, sample_limit=10)
    assert [column.name for column in inspection.columns] == ["编号", "姓名"]
    assert inspection.sample_rows[0]["姓名"] == "张三"


def test_markdown_without_table_falls_back_to_content_only() -> None:
    inspection = inspect_file(
        "# 标题\n\n没有表格。\n".encode(), file_format=FileAssetFormat.MARKDOWN, sample_limit=5
    )
    assert [column.name for column in inspection.columns] == ["content"]
    assert inspection.sample_rows == ()
    assert inspection.statistics["tables"] == 0


def test_json_object_root_is_treated_as_single_row() -> None:
    inspection = inspect_file(
        b'{"id": 1, "email": "a@b.com"}', file_format=FileAssetFormat.JSON, sample_limit=5
    )
    assert [column.name for column in inspection.columns] == ["id", "email"]
    assert inspection.row_count == 1


def test_json_array_of_scalars_is_rejected() -> None:
    with pytest.raises(DomainError, match="不是对象"):
        inspect_file(b"[1, 2, 3]", file_format=FileAssetFormat.JSON, sample_limit=5)


def test_malformed_json_reports_line_number() -> None:
    with pytest.raises(DomainError, match="第 2 行"):
        inspect_file(b'{"a": 1}\n{"b":', file_format=FileAssetFormat.JSONL, sample_limit=5)


def test_empty_file_is_rejected() -> None:
    with pytest.raises(DomainError, match="内容为空"):
        inspect_file(b"", file_format=FileAssetFormat.CSV, sample_limit=5)


def test_unsupported_suffix_is_rejected() -> None:
    """不支持的后缀明确报错，不做内容嗅探式猜测、不静默降级。"""
    with pytest.raises(DomainError, match="不支持的文件后缀"):
        detect_format("orders.docx")


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("orders.csv", FileAssetFormat.CSV),
        ("orders.CSV", FileAssetFormat.CSV),
        ("orders.tsv", FileAssetFormat.CSV),
        ("orders.jsonl", FileAssetFormat.JSONL),
        ("orders.ndjson", FileAssetFormat.JSONL),
        ("orders.parquet", FileAssetFormat.PARQUET),
        ("orders.xlsm", FileAssetFormat.XLSX),
        ("README.md", FileAssetFormat.MARKDOWN),
    ],
)
def test_format_detection(name: str, expected: FileAssetFormat) -> None:
    assert detect_format(name) is expected
