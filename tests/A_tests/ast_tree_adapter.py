"""将模块 A 输出的 AST 转换为可视化工具可消费的通用树结构。

本模块位于 tests/，只服务于 AST 演示和答辩，不参与 SQL 编译过程。
它不重新解析 SQL，也不解释 WHERE 的真假；唯一工作是读取 ``compiler.parse``
返回的数据类对象，并保留其父子关系，转换为适合 tkinter Canvas 绘制的树节点。
"""

from __future__ import annotations

from dataclasses import dataclass, fields, is_dataclass
from enum import Enum
from typing import Any


@dataclass(frozen=True)
class TreeNode:
    """表示 AST 可视化中的一个通用树节点。

    Attributes:
        label: 节点显示给用户的文字，例如 ``SelectStmt``、``table`` 或
            ``'users'``。标签表达结构或叶子值，不承担 SQL 语义判断。
        children: 从当前节点向下连接的子节点。tuple 保证转换结果不可变，
            方便测试，也避免 UI 渲染时误修改 AST 展示结构。
    """

    label: str
    children: tuple[TreeNode, ...] = ()


# 此公开函数是可视化层读取真实 AST 的唯一入口。
def ast_to_tree(ast_node: Any) -> TreeNode:
    """将一个 AST 数据类根节点转换为包含字段和值的 TreeNode 树。

    根节点通常是 ``SelectStmt``、``InsertStmt`` 或其他 Statement。每个数据类
    字段会生成一个字段节点，字段值再递归转换为数据类、元组、枚举或普通叶子
    节点，因此右侧 Canvas 能完整显示 AST 的层级关系。

    Args:
        ast_node: ``compiler.parse`` 成功返回的 Statement 或其中任意子节点。

    Returns:
        以 AST 类型名称为根标签的 TreeNode；若输入不是数据类，也会返回对应的
        叶子节点，便于独立测试转换逻辑。
    """
    return _convert_value(ast_node)


# 此内部函数按值的运行时类别递归建立展示树，是转换算法的核心。
def _convert_value(value: Any) -> TreeNode:
    """把数据类、元组、枚举和普通值分别转换为可显示的 TreeNode。

    数据类节点用类名作为标签，并为每个字段创建“字段名 → 字段值”的层级；
    tuple 节点用 ``tuple[n]`` 表示长度，并为每项保留索引；枚举显示枚举类和
    成员名；其余 int、float、str、None 等值显示 repr，避免字符串引号丢失。

    Args:
        value: 当前需要转换的 AST 子对象或叶子值。

    Returns:
        与 value 的结构一一对应的 TreeNode。
    """
    if is_dataclass(value) and not isinstance(value, type):
        field_nodes = tuple(
            TreeNode(
                label=field.name,
                children=(_convert_value(getattr(value, field.name)),),
            )
            for field in fields(value)
        )
        return TreeNode(label=type(value).__name__, children=field_nodes)

    if isinstance(value, tuple):
        item_nodes = tuple(
            TreeNode(label=f"[{index}]", children=(_convert_value(item),))
            for index, item in enumerate(value)
        )
        return TreeNode(label=f"tuple[{len(value)}]", children=item_nodes)

    if isinstance(value, Enum):
        return TreeNode(label=f"{type(value).__name__}.{value.name}")

    return TreeNode(label=repr(value))


__all__ = ["TreeNode", "ast_to_tree"]
