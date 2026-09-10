"""模块 A 的 V2 FROM 表别名解析测试。

本文件验证显式 ``AS`` 与隐式表别名都进入 TableRef.alias，并检查名称规范化。
别名是否与其他表重复、限定列是否真实存在等语义问题仍由 C 负责。
"""

from __future__ import annotations

import pytest

from compiler import parse
from contracts.ast import Column, SelectStmt, TableRef
from contracts.errors import E_SYNTAX, ParseError


# 此参数化测试验证显式和隐式两种表别名写法生成完全相同的 TableRef。
@pytest.mark.parametrize(
    "from_clause",
    [
        "Users U",
        "Users AS U",
        "Users as u",
    ],
)
def test_select_parses_explicit_and_implicit_table_aliases(
    from_clause: str,
) -> None:
    """确认两种别名语法均转为小写的 name 和 alias。

    Args:
        from_clause: FROM 后包含基础表名及别名的源码片段。
    """
    statement = parse(f"SELECT U.ID FROM {from_clause};")

    assert statement == SelectStmt(
        columns=(Column(name="id", qualifier="u"),),
        table=TableRef(name="users", alias="u"),
        where=None,
    )
    assert statement.table.qualifier == "u"


# 此测试验证没有别名时不会误消费 WHERE，并保留 TableRef 的基础表限定符。
def test_table_reference_without_alias_stops_before_where() -> None:
    """确认 WHERE 保留字不是隐式别名，alias 保持 None。"""
    statement = parse("SELECT id FROM Users WHERE active;")

    assert isinstance(statement, SelectStmt)
    assert statement.table == TableRef(name="users", alias=None)
    assert statement.table.qualifier == "users"
    assert statement.where == Column(name="active")


# 此测试验证表名和别名使用同一个标识符规范化规则统一转为小写。
def test_table_name_and_alias_are_normalized_to_lowercase() -> None:
    """确认混合大小写的基础表名和别名不会原样泄漏到 AST。"""
    statement = parse("SELECT account.id FROM UserAccounts AcCoUnT;")

    assert isinstance(statement, SelectStmt)
    assert statement.table == TableRef(
        name="useraccounts",
        alias="account",
    )


# 此参数化测试验证 AS 后必须提供一个非保留字标识符作为别名。
@pytest.mark.parametrize(
    "sql",
    [
        "SELECT * FROM users AS;",
        "SELECT * FROM users AS WHERE active;",
        "SELECT * FROM users AS SELECT;",
    ],
)
def test_explicit_as_requires_identifier_alias(sql: str) -> None:
    """确认残缺或使用保留字的显式别名统一报告 E_SYNTAX。

    Args:
        sql: AS 后缺少合法普通标识符的完整 SELECT SQL。
    """
    with pytest.raises(ParseError) as error_info:
        parse(sql)

    assert error_info.value.code == E_SYNTAX
