"""模块 A 的列引用和通用标量操作数解析测试。

本文件只检查 SQL 到 AST 的语法转换：普通列、限定列和字面量均可形成标量，
比较运算两侧也可使用任意标量。表、限定符和列是否真实存在由 C 负责验证。
"""

from __future__ import annotations

import pytest

from compiler import parse
from contracts.ast import Column, Cmp, Literal, SelectStmt
from contracts.errors import E_SYNTAX, ParseError


# 此测试验证 SELECT 投影中的普通列和限定列都会生成 Column 而非字符串。
def test_select_list_builds_unqualified_and_qualified_columns() -> None:
    """确认投影列保留输入顺序、限定符，并将所有标识符统一为小写。"""
    statement = parse("SELECT ID, U.Name FROM Users;")

    assert isinstance(statement, SelectStmt)
    assert statement.columns == (
        Column(name="id"),
        Column(name="name", qualifier="u"),
    )


# 此参数化测试验证普通列、限定列、数字和布尔值都可作为裸标量表达式。
@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ("id", Column(name="id")),
        ("U.ID", Column(name="id", qualifier="u")),
        ("18", Literal(value=18)),
        ("TRUE", Literal(value=True)),
    ],
)
def test_where_accepts_each_scalar_operand(
    source: str,
    expected: Column | Literal,
) -> None:
    """确认单独的标量可进入 Expr，布尔上下文合法性留给 C 判断。

    Args:
        source: WHERE 后的普通列、限定列或字面量源码。
        expected: Parser 应构造的 Column 或 Literal 节点。
    """
    statement = parse(f"SELECT * FROM users WHERE {source};")

    assert isinstance(statement, SelectStmt)
    assert statement.where == expected


# 此参数化测试验证比较运算符两侧都使用通用标量，而非固定“列和值”。
@pytest.mark.parametrize(
    ("condition", "expected_left", "expected_right"),
    [
        ("u.id = o.user_id", Column("id", "u"), Column("user_id", "o")),
        ("age >= 18", Column("age"), Literal(18)),
        ("18 <= age", Literal(18), Column("age")),
        ("TRUE <> FALSE", Literal(True), Literal(False)),
    ],
)
def test_comparison_accepts_scalar_on_both_sides(
    condition: str,
    expected_left: Column | Literal,
    expected_right: Column | Literal,
) -> None:
    """确认四种操作数组合均能生成左右节点正确的 Cmp。

    Args:
        condition: 包含两个标量和比较符的 WHERE 条件。
        expected_left: 预期的左侧 Column 或 Literal。
        expected_right: 预期的右侧 Column 或 Literal。
    """
    statement = parse(f"SELECT * FROM users WHERE {condition};")

    assert isinstance(statement, SelectStmt)
    assert isinstance(statement.where, Cmp)
    assert statement.where.left == expected_left
    assert statement.where.right == expected_right


# 此参数化测试验证限定列必须在点号两侧各有且仅有一个标识符。
@pytest.mark.parametrize(
    "sql",
    [
        "SELECT u. FROM users;",
        "SELECT u.id.extra FROM users;",
        "SELECT * FROM users WHERE u. = 1;",
    ],
)
def test_invalid_qualified_column_reports_syntax_error(sql: str) -> None:
    """确认残缺或多段限定列不会生成错误的 Column 节点。

    Args:
        sql: 包含非法限定列结构的完整 SQL。
    """
    with pytest.raises(ParseError) as error_info:
        parse(sql)

    assert error_info.value.code == E_SYNTAX
