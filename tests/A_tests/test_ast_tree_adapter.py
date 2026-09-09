"""AST 可视化转换器的纯逻辑测试。

这些测试不创建 tkinter 窗口，因此可在无图形界面的环境运行。它们验证展示树
确实来自真实 AST 的类型、字段和嵌套表达式，而不是在可视化层手写 SQL 规则。
"""

from __future__ import annotations

from contracts.ast import Cmp, Column, Literal, SelectStmt
from tests.A_tests.ast_tree_adapter import ast_to_tree


# 此测试确认 Statement 根节点、字段节点和嵌套 Cmp 条件都会被完整保留。
def test_ast_to_tree_preserves_select_statement_structure() -> None:
    """将带 WHERE 的 SelectStmt 转换后，应保留类型、字段和值的树形层级。"""
    statement = SelectStmt(
        columns=("name",),
        table="users",
        where=Cmp(left=Column("age"), op=">=", right=Literal(18)),
    )

    tree = ast_to_tree(statement)
    assert tree.label == "SelectStmt"
    assert [child.label for child in tree.children] == ["columns", "table", "where"]
    assert tree.children[0].children[0].label == "tuple[1]"
    assert tree.children[1].children[0].label == "'users'"
    assert tree.children[2].children[0].label == "Cmp"
