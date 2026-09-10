"""模块 A 的 V2 多语句 parse_script 测试。

本文件验证脚本解析直接消费完整 Token 流，保留每条语句的 AST、原文和全局
SourceSpan。测试特别覆盖字符串内分号，防止实现退化为简单字符串 split。
"""

from __future__ import annotations

import pytest

from compiler import parse, parse_script
from contracts.ast import (
    CreateDatabaseStmt,
    InsertStmt,
    SelectStmt,
    SourceSpan,
    UseDatabaseStmt,
)
from contracts.errors import E_SYNTAX, ParseError


# 此测试验证题目给出的两条 SQL 能按顺序解析并保留各自原文与范围。
def test_parse_script_returns_statements_in_source_order() -> None:
    """确认多语句脚本生成两个有序 ParsedStatement。"""
    script = parse_script("CREATE DATABASE shop; USE shop;")

    assert [item.statement for item in script] == [
        CreateDatabaseStmt(name="shop"),
        UseDatabaseStmt(name="shop"),
    ]
    assert [item.sql for item in script] == [
        "CREATE DATABASE shop;",
        "USE shop;",
    ]
    assert [item.span for item in script] == [
        SourceSpan(start_line=1, start_col=1, end_line=1, end_col=21),
        SourceSpan(start_line=1, start_col=23, end_line=1, end_col=31),
    ]


# 此测试验证字符串 Token 内的分号不会被当成语句分隔符。
def test_parse_script_does_not_split_semicolon_inside_string() -> None:
    """确认包含 ``'a;b'`` 的 INSERT 仍是一条完整语句。"""
    script = parse_script("INSERT INTO logs VALUES ('a;b'); USE shop;")

    assert len(script) == 2
    assert script[0].statement == InsertStmt(table="logs", values=("a;b",))
    assert script[0].sql == "INSERT INTO logs VALUES ('a;b');"
    assert script[1].statement == UseDatabaseStmt(name="shop")


# 此测试验证跨多行语句与下一条语句共享同一套全局行列坐标。
def test_parse_script_preserves_multiline_global_spans() -> None:
    """确认 span 忽略分隔空白，但覆盖语句内部换行和结束分号。"""
    source = (
        "\n  SELECT u.id\n"
        "  FROM users u\n"
        "  WHERE u.active = TRUE;\n"
        "USE shop"
    )

    script = parse_script(source)

    assert len(script) == 2
    assert isinstance(script[0].statement, SelectStmt)
    assert script[0].sql == (
        "SELECT u.id\n"
        "  FROM users u\n"
        "  WHERE u.active = TRUE;"
    )
    assert script[0].span == SourceSpan(
        start_line=2,
        start_col=3,
        end_line=4,
        end_col=24,
    )
    assert script[1].sql == "USE shop"
    assert script[1].span == SourceSpan(
        start_line=5,
        start_col=1,
        end_line=5,
        end_col=8,
    )


# 此参数化测试验证空输入合法，并且最后一条语句的分号可以省略。
@pytest.mark.parametrize("source", ["", "   ", "\n\t\r\n"])
def test_parse_script_returns_empty_tuple_for_blank_input(source: str) -> None:
    """确认没有 Token 的空白脚本不会被误报为语法错误。

    Args:
        source: 空字符串或只含不同空白字符的源码。
    """
    assert parse_script(source) == ()


# 此测试验证末条省略分号时，原文和闭区间结束于最后一个 Token。
def test_parse_script_allows_final_statement_without_semicolon() -> None:
    """确认可选末尾分号不会影响 AST，并产生准确结束列。"""
    script = parse_script("USE shop")

    assert len(script) == 1
    assert script[0].statement == UseDatabaseStmt(name="shop")
    assert script[0].sql == "USE shop"
    assert script[0].span == SourceSpan(1, 1, 1, 8)


# 此测试验证普通 parse 仍只接受一条 SQL，不会悄悄改变原公开接口语义。
def test_single_statement_parse_still_rejects_multiple_statements() -> None:
    """确认新增 parse_script 不会让 parse 接受第二条语句。"""
    with pytest.raises(ParseError) as error_info:
        parse("CREATE DATABASE shop; USE shop;")

    assert error_info.value.code == E_SYNTAX


# 此参数化测试验证开头或连续分号代表非法空语句，而不是被静默跳过。
@pytest.mark.parametrize("source", [";", "; USE shop;", "USE shop;;"])
def test_parse_script_rejects_empty_statements(source: str) -> None:
    """确认空语句按照脚本文法报告 E_SYNTAX。

    Args:
        source: 含开头分号或连续分号的脚本。
    """
    with pytest.raises(ParseError) as error_info:
        parse_script(source)

    assert error_info.value.code == E_SYNTAX


# 此测试验证相邻语句缺少分号时在第二条语句开头报告错误。
def test_parse_script_requires_separator_between_statements() -> None:
    """确认不能依靠空白分隔两条 SQL。"""
    with pytest.raises(ParseError) as error_info:
        parse_script("CREATE DATABASE shop USE shop;")

    assert error_info.value.code == E_SYNTAX
    assert (error_info.value.line, error_info.value.col) == (1, 22)


# 此测试验证第二条语句的错误位置不会被重置到脚本第一行。
def test_parse_script_reports_global_location_for_later_error() -> None:
    """确认后续语句语法错误携带完整输入中的真实行列号。"""
    with pytest.raises(ParseError) as error_info:
        parse_script("USE shop;\nSELECT FROM users;")

    assert error_info.value.code == E_SYNTAX
    assert (error_info.value.line, error_info.value.col) == (2, 8)
