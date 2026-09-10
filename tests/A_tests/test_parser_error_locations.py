"""模块 A 的 V2 全局错误位置测试。

本文件集中验证 Lexer 与 Parser 在完整多语句源码中共享同一套一基行列坐标。
每个错误都位于第二条或后续行，防止实现只在单条、单行 SQL 中碰巧正确。
"""

from __future__ import annotations

import pytest

from compiler import parse_script
from contracts.errors import E_SYNTAX, ParseError


# 此参数化测试覆盖非法字符、括号、限定列和 JOIN 结构的全局错误位置。
@pytest.mark.parametrize(
    ("source", "expected_line", "expected_col"),
    [
        (
            "USE shop;\nSELECT * FROM users\nWHERE active @ TRUE;",
            3,
            14,
        ),
        (
            "USE shop;\nSELECT * FROM users\nWHERE (active = TRUE;",
            3,
            21,
        ),
        (
            "USE shop;\nSELECT * FROM users u\nJOIN orders o u.id = o.user_id;",
            3,
            15,
        ),
        (
            "USE shop;\nSELECT u. FROM users u;",
            2,
            11,
        ),
    ],
    ids=[
        "lexer-illegal-character",
        "parser-missing-right-parenthesis",
        "parser-join-missing-on",
        "parser-qualified-column-missing-name",
    ],
)
def test_parse_script_reports_global_error_position(
    source: str,
    expected_line: int,
    expected_col: int,
) -> None:
    """确认不同解析阶段均报告相对于完整脚本的准确行列位置。

    Args:
        source: 在第一条合法语句之后包含错误的完整 SQL 脚本。
        expected_line: ParseError 应报告的一基全局行号。
        expected_col: ParseError 应报告的一基全局列号。
    """
    with pytest.raises(ParseError) as error_info:
        parse_script(source)

    assert error_info.value.code == E_SYNTAX
    assert (error_info.value.line, error_info.value.col) == (
        expected_line,
        expected_col,
    )
