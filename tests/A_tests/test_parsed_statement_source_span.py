"""模块 A 的 ParsedStatement 原文与全局 SourceSpan 专项测试。

测试覆盖原始大小写、内部空白、CRLF、多条语句、Unicode 字符和末条无分号，
确保 sql 字段与一基闭区间 span 始终描述完整脚本中的同一条语句。
"""

from __future__ import annotations

from compiler import parse_script
from contracts.ast import SourceSpan


# 此测试验证语句原文保留大小写和内部空白，并让 span 包含源码中的分号。
def test_parsed_statement_preserves_original_text_exactly() -> None:
    """确认只移除语句前分隔空白，不格式化语句内部原文。"""
    source = " \tSeLeCt  U.ID\nFROM Users U \nWHERE TRUE   ;  \n"

    script = parse_script(source)

    assert len(script) == 1
    assert script[0].sql == "SeLeCt  U.ID\nFROM Users U \nWHERE TRUE   ;"
    assert script[0].span == SourceSpan(
        start_line=1,
        start_col=3,
        end_line=3,
        end_col=14,
    )


# 此测试验证 CRLF 后的语句继续使用完整脚本中的全局行列号。
def test_parsed_statement_spans_remain_global_across_crlf() -> None:
    """确认第二条语句不会把行号重置为一，并正确跳过前导空格。"""
    source = "INSERT INTO logs VALUES ('中;文');\r\n  USE Shop;"

    script = parse_script(source)

    assert [item.sql for item in script] == [
        "INSERT INTO logs VALUES ('中;文');",
        "USE Shop;",
    ]
    assert [item.span for item in script] == [
        SourceSpan(start_line=1, start_col=1, end_line=1, end_col=32),
        SourceSpan(start_line=2, start_col=3, end_line=2, end_col=11),
    ]


# 此测试验证末条省略分号时，尾随空白不进入原文或 SourceSpan。
def test_final_statement_span_excludes_trailing_separator_whitespace() -> None:
    """确认无分号末条结束于最后一个有效 Token，而不是脚本 EOF。"""
    script = parse_script("USE Shop   \r\n\t")

    assert len(script) == 1
    assert script[0].sql == "USE Shop"
    assert script[0].span == SourceSpan(
        start_line=1,
        start_col=1,
        end_line=1,
        end_col=8,
    )
