"""模块 A 的单语句 parse 向后兼容测试。

本文件锁定 V1.1 公开入口的结束规则：一条语句可带或不带一个末尾分号，
但空输入、连续分号和第二条语句仍必须报告 E_SYNTAX。
"""

from __future__ import annotations

import pytest

from compiler import parse
from contracts.ast import UseDatabaseStmt
from contracts.errors import E_SYNTAX, ParseError


# 此参数化测试验证单语句在无分号、单分号和外围空白下保持相同 AST。
@pytest.mark.parametrize(
    "sql",
    [
        "USE Shop",
        "USE Shop;",
        "  USE Shop;  \n",
    ],
)
def test_parse_keeps_optional_single_semicolon_behavior(sql: str) -> None:
    """确认 V1.1 合法单语句继续通过原有 parse 接口解析。

    Args:
        sql: 使用不同末尾分号和空白形式的单条 USE SQL。
    """
    assert parse(sql) == UseDatabaseStmt(name="shop")


# 此参数化测试验证空输入和空语句仍由单语句入口拒绝。
@pytest.mark.parametrize("sql", ["", "   \n", ";"])
def test_parse_still_rejects_empty_input(sql: str) -> None:
    """确认 parse 不采用 parse_script 的空输入返回空元组语义。

    Args:
        sql: 空字符串、纯空白或只有分号的无效单语句输入。
    """
    with pytest.raises(ParseError) as error_info:
        parse(sql)

    assert error_info.value.code == E_SYNTAX


# 此参数化测试验证 parse 不接受多余分号、第二条语句或缺少分隔符的输入。
@pytest.mark.parametrize(
    ("sql", "expected_col"),
    [
        ("USE shop;;", 10),
        ("USE shop; USE other;", 11),
        ("USE shop DROP DATABASE other", 10),
    ],
)
def test_parse_still_rejects_content_after_one_statement(
    sql: str,
    expected_col: int,
) -> None:
    """确认错误位置指向第一条语句后的首个多余 Token。

    Args:
        sql: 包含多余内容的单语句接口输入。
        expected_col: 首个多余 Token 的一基列号。
    """
    with pytest.raises(ParseError) as error_info:
        parse(sql)

    assert error_info.value.code == E_SYNTAX
    assert (error_info.value.line, error_info.value.col) == (1, expected_col)
