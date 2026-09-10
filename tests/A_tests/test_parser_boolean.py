"""模块 A 的 BOOLEAN 类型及布尔字面量解析测试。

本文件验证 Parser 将 V2 BOOLEAN 语法转换为共享 AST。它不创建数据库或表，
也不检查列和值的类型是否匹配；这些语义与存储行为属于 C、B 模块。
"""

from __future__ import annotations

import pytest

from compiler import parse
from contracts.ast import (
    Assignment,
    Column,
    ColumnDef,
    Cmp,
    CreateTableStmt,
    InsertStmt,
    Literal,
    SelectStmt,
    SqlType,
    UpdateStmt,
)


# 此测试验证 CREATE TABLE 能将 BOOLEAN 类型关键字映射到共享 SqlType 枚举。
def test_create_table_parses_boolean_column_type() -> None:
    """确认 BOOLEAN 列与原有列类型可以按声明顺序进入 CreateTableStmt。"""
    statement = parse("CREATE TABLE Flags (id INT, Enabled BOOLEAN);")

    assert statement == CreateTableStmt(
        table="flags",
        columns=(
            ColumnDef(name="id", type=SqlType.INT),
            ColumnDef(name="enabled", type=SqlType.BOOLEAN),
        ),
    )


# 此参数化测试验证 TRUE/FALSE 不区分大小写，并转换为严格的 Python bool。
@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ("TRUE", True),
        ("true", True),
        ("FALSE", False),
        ("FaLsE", False),
    ],
)
def test_insert_converts_boolean_literals_to_python_bool(
    source: str,
    expected: bool,
) -> None:
    """确认 INSERT 中的布尔字面量进入 AST 后不再是字符串或整数。

    Args:
        source: 不同大小写形式的 SQL 布尔字面量。
        expected: 对应的 Python True 或 False。
    """
    statement = parse(f"INSERT INTO Flags VALUES ({source});")

    assert statement == InsertStmt(table="flags", values=(expected,))
    assert type(statement.values[0]) is bool


# 此测试验证通用 parse_value 路径也让 UPDATE 赋值支持布尔字面量。
def test_update_assignment_accepts_boolean_literal() -> None:
    """确认 UPDATE SET 复用布尔值转换，并构建值为 bool 的 Assignment。"""
    statement = parse("UPDATE flags SET enabled = FALSE;")

    assert statement == UpdateStmt(
        table="flags",
        assignments=(Assignment(column="enabled", value=False),),
        where=None,
    )
    assert type(statement.assignments[0].value) is bool


# 此测试验证现有比较解析器能够把布尔字面量放入 Literal AST 节点。
def test_where_comparison_accepts_boolean_literal() -> None:
    """确认 WHERE 右侧 TRUE 被转换为 Literal(True)，但不执行类型检查。"""
    statement = parse("SELECT * FROM flags WHERE enabled = TRUE;")

    assert isinstance(statement, SelectStmt)
    assert statement.where == Cmp(
        left=Column(name="enabled"),
        op="=",
        right=Literal(value=True),
    )
    assert type(statement.where.right.value) is bool
