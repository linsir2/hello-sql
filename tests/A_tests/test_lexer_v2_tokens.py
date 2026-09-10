"""模块 A 的 V2 Token 扩展测试。

本文件只验证词法层职责：新增关键字应被识别为专用 Token，点号应与两侧
标识符分开。BOOLEAN 值转换、表达式优先级和 JOIN 结构留给后续 parser
步骤测试，避免在 Token 更新阶段混入尚未实现的语法功能。
"""

from __future__ import annotations

import pytest

from compiler.lexer import tokenize
from compiler.tokens import TokenType


# 此参数化测试验证全部 V2 新关键字以及已正式启用的 OR 都不区分大小写。
@pytest.mark.parametrize(
    ("source", "expected_type"),
    [
        ("BOOLEAN", TokenType.KW_BOOLEAN),
        ("true", TokenType.KW_TRUE),
        ("False", TokenType.KW_FALSE),
        ("nOt", TokenType.KW_NOT),
        ("AS", TokenType.KW_AS),
        ("inner", TokenType.KW_INNER),
        ("Join", TokenType.KW_JOIN),
        ("on", TokenType.KW_ON),
        ("oR", TokenType.KW_OR),
    ],
)
def test_v2_keywords_are_reserved_case_insensitively(
    source: str,
    expected_type: TokenType,
) -> None:
    """确认 V2 关键字产生专用 Token，同时保留用户输入的原始 lexeme。

    Args:
        source: 使用不同大小写书写的单个 SQL 关键字。
        expected_type: 该关键字在 TokenType 中对应的专用枚举值。
    """
    keyword, eof = tokenize(source)

    assert keyword.type is expected_type
    assert keyword.lexeme == source
    assert keyword.position.line == 1
    assert keyword.position.column == 1
    assert keyword.start_offset == 0
    assert keyword.end_offset == len(source)
    assert eof.type is TokenType.EOF
    assert eof.start_offset == eof.end_offset == len(source)


# 此测试验证限定列点号被独立识别，并检查后续列名的源码列位置。
def test_dot_separates_table_qualifier_and_column_name() -> None:
    """确认 ``u.id`` 被拆成标识符、DOT、标识符和 EOF 四个 Token。"""
    tokens = tokenize("u.id")

    assert [token.type for token in tokens] == [
        TokenType.IDENTIFIER,
        TokenType.DOT,
        TokenType.IDENTIFIER,
        TokenType.EOF,
    ]
    assert [token.lexeme for token in tokens] == ["u", ".", "id", ""]
    assert [token.position.column for token in tokens] == [1, 2, 3, 5]
    assert [(token.start_offset, token.end_offset) for token in tokens] == [
        (0, 1),
        (1, 2),
        (2, 4),
        (4, 4),
    ]


# 此测试保护小数扫描规则，确保加入 DOT 后实数字面量仍保持为一个 Token。
def test_dot_does_not_split_real_literal() -> None:
    """确认数字内部的小数点仍属于 REAL_LITERAL，而不是独立 DOT。"""
    real, eof = tokenize("18.5")

    assert real.type is TokenType.REAL_LITERAL
    assert real.lexeme == "18.5"
    assert (real.start_offset, real.end_offset) == (0, 4)
    assert eof.type is TokenType.EOF
