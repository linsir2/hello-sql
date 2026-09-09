"""模块 A 的 golden SQL 解析验收测试。

本测试只验证编译层的职责边界：给定一条 SQL，``compiler.parse`` 能否将其
转换为 contracts.ast 中的 Statement，或者在词法/语法错误时抛出 E_SYNTAX。
测试不会创建数据目录、不会调用 Storage 或 Runner，也不会验证 SELECT 的
查询结果、INSERT 的类型匹配等运行期语义；这些属于模块 B 与模块 C。

golden_sql.py 的 37 条用例按端到端执行时有状态依赖，但对 A 来说每条 SQL
都可以独立解析。因此本文件逐条参数化执行，专门验收 SQL -> AST 这一交接链。
"""

from __future__ import annotations

from typing import Any

import pytest

from compiler import parse
from contracts.ast import (
    CreateDatabaseStmt,
    CreateTableStmt,
    DeleteStmt,
    DropDatabaseStmt,
    DropTableStmt,
    InsertStmt,
    SelectStmt,
    Statement,
    UpdateStmt,
    UseDatabaseStmt,
)
from contracts.errors import E_SYNTAX, ParseError
from tests.golden_sql import GOLDEN_SQL


# 这三个 golden 用例明确属于 A：它们在词法或语法分析阶段即应失败，
# 而不是生成 AST 后交给 C/B 处理。
_SYNTAX_ERROR_CASE_IDS = frozenset({"g19", "g20", "g21"})


# Python 的 Statement 是类型别名，不能直接用于 isinstance。因此列出它的
# 全部具体 AST 数据类，用于确认每一条合法 SQL 确实交付了一个 AST 节点。
_STATEMENT_TYPES = (
    CreateDatabaseStmt,
    DropDatabaseStmt,
    UseDatabaseStmt,
    CreateTableStmt,
    DropTableStmt,
    InsertStmt,
    SelectStmt,
    UpdateStmt,
    DeleteStmt,
)


# 此参数化测试逐条执行 37 条 golden SQL，并以 g01 至 g37 作为 pytest 用例名。
@pytest.mark.parametrize("case", GOLDEN_SQL, ids=[case["id"] for case in GOLDEN_SQL])
def test_compiler_parses_every_golden_sql_case(case: dict[str, Any]) -> None:
    """验证每条 golden SQL 都遵守模块 A 的解析阶段预期。

    g19、g20、g21 是唯一应在 A 阶段失败的 SQL：它们分别包含未知语句关键字、
    缺失 SELECT 列表和未闭合字符串。测试要求它们抛出 ParseError，且错误码
    必须是 E_SYNTAX。

    其他 34 条即使在完整系统执行时会报业务错误（例如数据库已存在、表不
    存在、列重复、值类型不匹配），也都必须先成功解析为 AST。这正是 A 不做
    语义检查、将状态相关错误留给 C/B 的契约边界。

    Args:
        case: tests.golden_sql.GOLDEN_SQL 中的一条用例字典，至少包含 id 和 sql。
    """
    if case["id"] in _SYNTAX_ERROR_CASE_IDS:
        with pytest.raises(ParseError) as error_info:
            parse(case["sql"])

        # 同时比对 golden 声明和共享常量，防止测试或错误码定义被单边改动。
        assert error_info.value.code == case["code"] == E_SYNTAX
        return

    statement: Statement = parse(case["sql"])
    assert isinstance(statement, _STATEMENT_TYPES)
