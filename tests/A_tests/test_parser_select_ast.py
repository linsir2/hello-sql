"""模块 A 的 V2 SelectStmt 结构升级测试。

本文件验证 SELECT 的显式投影由 Column 组成、FROM 来源由 TableRef 表示。
本组聚焦不带别名和 JOIN 的基础 SELECT；别名与 JOIN 由对应专项测试覆盖。
"""

from __future__ import annotations

from compiler import parse
from contracts.ast import Column, SelectStmt, TableRef


# 此测试验证显式 SELECT 投影和 FROM 表都使用 V2 AST 数据类。
def test_select_builds_columns_and_table_reference() -> None:
    """确认普通投影列为 Column，基础表名为 alias=None 的 TableRef。"""
    statement = parse("SELECT ID FROM Users;")

    assert statement == SelectStmt(
        columns=(Column(name="id"),),
        table=TableRef(name="users"),
        where=None,
    )


# 此测试验证限定投影列与 TableRef 可以共同出现在同一 SelectStmt 中。
def test_select_preserves_qualified_projection_column() -> None:
    """确认限定符只进入 Column，FROM 表则独立保存在 TableRef 中。"""
    statement = parse("SELECT Users.ID FROM Users;")

    assert statement == SelectStmt(
        columns=(Column(name="id", qualifier="users"),),
        table=TableRef(name="users"),
        where=None,
    )


# 此测试验证 SELECT 星号仍以 columns=None 表示，并不影响 TableRef 升级。
def test_select_star_keeps_none_projection_with_table_reference() -> None:
    """确认 SELECT * 不生成虚假 Column，但 FROM 仍生成 TableRef。"""
    statement = parse("SELECT * FROM Users;")

    assert statement == SelectStmt(
        columns=None,
        table=TableRef(name="users"),
        where=None,
    )
