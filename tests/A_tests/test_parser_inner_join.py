"""模块 A 的 V2 INNER JOIN 解析测试。

本文件验证显式 INNER JOIN、简写 JOIN、多个连接的保存顺序及 ON 表达式 AST。
表和列是否存在、别名是否冲突、ON 是否为 BOOLEAN 均由 C 负责验证。
"""

from __future__ import annotations

import pytest

from compiler import parse
from contracts.ast import (
    And,
    Column,
    Cmp,
    JoinClause,
    JoinType,
    Literal,
    Not,
    Or,
    SelectStmt,
    TableRef,
)
from contracts.errors import E_SYNTAX, ParseError


# 此辅助函数解析完整 SELECT，并收窄为测试需要的 SelectStmt 类型。
def _parse_select(sql: str) -> SelectStmt:
    """解析 SQL 并确认结果是 SelectStmt。

    Args:
        sql: 一条完整的 SELECT SQL。

    Returns:
        Parser 构建的 SelectStmt。
    """
    statement = parse(sql)
    assert isinstance(statement, SelectStmt)
    return statement


# 此测试验证题目中的显式 INNER JOIN 示例生成完整且准确的 AST。
def test_parse_explicit_inner_join() -> None:
    """确认 FROM、右表别名、ON 列列比较及 INNER 类型全部进入 AST。"""
    statement = _parse_select(
        "SELECT u.id "
        "FROM users u "
        "INNER JOIN orders o ON u.id = o.user_id;"
    )

    assert statement == SelectStmt(
        columns=(Column(name="id", qualifier="u"),),
        table=TableRef(name="users", alias="u"),
        where=None,
        joins=(
            JoinClause(
                right=TableRef(name="orders", alias="o"),
                on=Cmp(
                    left=Column(name="id", qualifier="u"),
                    op="=",
                    right=Column(name="user_id", qualifier="o"),
                ),
                kind=JoinType.INNER,
            ),
        ),
    )


# 此测试验证省略 INNER 的 JOIN 简写仍生成 JoinType.INNER。
def test_parse_join_without_inner_keyword() -> None:
    """确认 V2 的 ``JOIN ... ON`` 简写与显式 INNER JOIN 语义相同。"""
    statement = _parse_select(
        "SELECT users.id FROM users JOIN orders ON users.id = orders.user_id;"
    )

    assert len(statement.joins) == 1
    assert statement.joins[0].kind is JoinType.INNER
    assert statement.joins[0].right == TableRef(name="orders")


# 此测试验证多个 JOIN 按源码从左到右保存在 SelectStmt.joins 中。
def test_multiple_joins_preserve_source_order() -> None:
    """确认连续连接不会覆盖前项，并在 WHERE 之前全部完成解析。"""
    statement = _parse_select(
        "SELECT u.id FROM users u "
        "JOIN orders o ON u.id = o.user_id "
        "INNER JOIN items i ON o.id = i.order_id "
        "WHERE i.enabled = TRUE;"
    )

    assert [join.right for join in statement.joins] == [
        TableRef(name="orders", alias="o"),
        TableRef(name="items", alias="i"),
    ]
    assert statement.where == Cmp(
        left=Column(name="enabled", qualifier="i"),
        op="=",
        right=Literal(value=True),
    )


# 此测试验证 ON 条件复用完整表达式解析器及既定优先级。
def test_join_on_accepts_parentheses_not_and_or() -> None:
    """确认复杂 ON 条件生成括号优先、NOT、AND、OR 对应的表达式树。"""
    statement = _parse_select(
        "SELECT * FROM users u JOIN orders o ON "
        "(u.id = o.user_id OR o.public = TRUE) AND NOT o.deleted;"
    )

    assert statement.joins[0].on == And(
        left=Or(
            left=Cmp(
                left=Column(name="id", qualifier="u"),
                op="=",
                right=Column(name="user_id", qualifier="o"),
            ),
            right=Cmp(
                left=Column(name="public", qualifier="o"),
                op="=",
                right=Literal(value=True),
            ),
        ),
        right=Not(operand=Column(name="deleted", qualifier="o")),
    )


# 此参数化测试验证缺失 JOIN 关键结构时统一报告 E_SYNTAX。
@pytest.mark.parametrize(
    "sql",
    [
        "SELECT * FROM users INNER orders ON users.id = orders.user_id;",
        "SELECT * FROM users JOIN ON users.id = orders.user_id;",
        "SELECT * FROM users JOIN orders;",
        "SELECT * FROM users JOIN orders ON;",
        "SELECT * FROM users JOIN orders ON WHERE users.id = 1;",
    ],
)
def test_incomplete_join_reports_syntax_error(sql: str) -> None:
    """确认残缺 JOIN 不会产生部分 JoinClause。

    Args:
        sql: 缺少 JOIN、右表、ON 或条件之一的 SELECT SQL。
    """
    with pytest.raises(ParseError) as error_info:
        parse(sql)

    assert error_info.value.code == E_SYNTAX


# 此参数化测试验证 V2 非目标 JOIN 类型不会被隐式别名逻辑错误接受。
@pytest.mark.parametrize("modifier", ["LEFT", "RIGHT", "FULL", "CROSS", "NATURAL"])
def test_unsupported_join_types_report_syntax_error(modifier: str) -> None:
    """确认仅 INNER JOIN 可以进入 AST。

    Args:
        modifier: V2 明确不支持的 JOIN 类型修饰词。
    """
    sql = (
        f"SELECT * FROM users {modifier} JOIN orders "
        "ON users.id = orders.user_id;"
    )

    with pytest.raises(ParseError) as error_info:
        parse(sql)

    assert error_info.value.code == E_SYNTAX
