"""模块 A 的 V2 逻辑表达式与优先级解析测试。

本文件验证 Parser 仅通过 AST 嵌套表达括号、比较、NOT、AND、OR 的优先级。
表达式是否最终得到 BOOLEAN、列是否存在及比较类型是否兼容由 C 负责。
"""

from __future__ import annotations

import pytest

from compiler import parse
from contracts.ast import And, Column, Cmp, Expr, Literal, Not, Or, SelectStmt
from contracts.errors import E_SYNTAX, ParseError


# 此辅助函数生成重复使用的“普通列等于整数”比较节点，简化预期 AST。
def _equals(column_name: str, value: int) -> Cmp:
    """构造测试预期使用的 ``column = integer`` Cmp 节点。

    Args:
        column_name: 已规范化的小写列名。
        value: 比较右侧的 Python 整数。

    Returns:
        左侧为 Column、右侧为 Literal 的 Cmp。
    """
    return Cmp(
        left=Column(name=column_name),
        op="=",
        right=Literal(value=value),
    )


# 此辅助函数解析 SELECT 的 WHERE，并返回测试需要核对的表达式 AST。
def _parse_where(condition: str) -> Expr:
    """把条件嵌入合法 SELECT，并返回非空的 WHERE 表达式。

    Args:
        condition: WHERE 关键字之后的表达式源码。

    Returns:
        Parser 生成的 Expr 根节点。
    """
    statement = parse(f"SELECT * FROM users WHERE {condition};")
    assert isinstance(statement, SelectStmt)
    assert statement.where is not None
    return statement.where


# 此测试验证 AND 比 OR 优先，不需要括号也会先构建 And 子树。
def test_and_has_higher_precedence_than_or() -> None:
    """确认 ``a=1 OR b=2 AND c=3`` 解析为 ``Or(a, And(b, c))``。"""
    expression = _parse_where("a = 1 OR b = 2 AND c = 3")

    assert expression == Or(
        left=_equals("a", 1),
        right=And(
            left=_equals("b", 2),
            right=_equals("c", 3),
        ),
    )


# 此测试验证比较先于 NOT、NOT 又先于 AND 构建 AST。
def test_comparison_and_not_bind_before_and() -> None:
    """确认 ``NOT a=1 AND b=2`` 解析为 ``And(Not(Cmp), Cmp)``。"""
    expression = _parse_where("NOT a = 1 AND b = 2")

    assert expression == And(
        left=Not(operand=_equals("a", 1)),
        right=_equals("b", 2),
    )


# 此测试验证括号递归解析完整表达式，并覆盖 AND 高于 OR 的默认顺序。
def test_parentheses_override_default_precedence() -> None:
    """确认括号中的 OR 先形成子树，再与外侧条件构建 And。"""
    expression = _parse_where("(a = 1 OR b = 2) AND c = 3")

    assert expression == And(
        left=Or(
            left=_equals("a", 1),
            right=_equals("b", 2),
        ),
        right=_equals("c", 3),
    )


# 此测试验证连续 NOT 使用递归结构，且括号不会残留为额外 AST 节点。
def test_repeated_not_builds_nested_not_nodes() -> None:
    """确认 ``NOT NOT (active)`` 生成两个嵌套 Not 和一个 Column。"""
    expression = _parse_where("NOT NOT (active)")

    assert expression == Not(
        operand=Not(operand=Column(name="active")),
    )


# 此测试验证同优先级的 OR 按源码顺序构建左结合树。
def test_repeated_or_is_left_associative() -> None:
    """确认 ``a OR b OR c`` 解析为 ``Or(Or(a, b), c)``。"""
    expression = _parse_where("a OR b OR c")

    assert expression == Or(
        left=Or(left=Column("a"), right=Column("b")),
        right=Column("c"),
    )


# 此测试验证复杂表达式内的列列、列值和值值比较均沿用通用标量解析。
def test_logical_expression_accepts_all_comparison_operand_shapes() -> None:
    """确认多种比较操作数可以共同组成 NOT、AND 和 OR 表达式树。"""
    expression = _parse_where(
        "u.id = o.user_id AND age >= 18 OR NOT TRUE = FALSE"
    )

    assert expression == Or(
        left=And(
            left=Cmp(
                left=Column(name="id", qualifier="u"),
                op="=",
                right=Column(name="user_id", qualifier="o"),
            ),
            right=Cmp(
                left=Column(name="age"),
                op=">=",
                right=Literal(value=18),
            ),
        ),
        right=Not(
            operand=Cmp(
                left=Literal(value=True),
                op="=",
                right=Literal(value=False),
            )
        ),
    )


# 此参数化测试验证残缺括号和逻辑运算符统一产生 E_SYNTAX。
@pytest.mark.parametrize(
    "condition",
    [
        "()",
        "(a = 1",
        "a = 1)",
        "NOT",
        "a OR",
        "a AND OR b",
    ],
)
def test_incomplete_logical_expression_reports_syntax_error(
    condition: str,
) -> None:
    """确认不完整表达式不会返回部分 AST，而是在准确位置报告语法错误。

    Args:
        condition: 缺少操作数或括号不匹配的 WHERE 表达式。
    """
    with pytest.raises(ParseError) as error_info:
        _parse_where(condition)

    assert error_info.value.code == E_SYNTAX
