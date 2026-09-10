"""模块 A 的 Lexer 全局源码位置与字符偏移测试。

行列位置用于向用户展示错误位置，字符偏移用于后续从完整脚本中恢复每条
SQL 原文。两套坐标必须由同一个 Lexer 游标维护，才能避免多行和 CRLF 输入
中的位置偏差。
"""

from __future__ import annotations

from compiler.lexer import tokenize
from compiler.tokens import TokenType


# 此测试验证跨越 CRLF 和 LF 后，Token 仍记录完整脚本中的全局行列与偏移。
def test_tokens_keep_global_locations_across_mixed_line_endings() -> None:
    """确认混合换行脚本中的 Token 坐标不会按行或按语句重新计数。"""
    source = "SELECT u.id,\r\n  TRUE\nFROM users"
    tokens = tokenize(source)

    assert [token.type for token in tokens] == [
        TokenType.KW_SELECT,
        TokenType.IDENTIFIER,
        TokenType.DOT,
        TokenType.IDENTIFIER,
        TokenType.COMMA,
        TokenType.KW_TRUE,
        TokenType.KW_FROM,
        TokenType.IDENTIFIER,
        TokenType.EOF,
    ]
    assert [
        (token.position.line, token.position.column)
        for token in tokens
    ] == [
        (1, 1),
        (1, 8),
        (1, 9),
        (1, 10),
        (1, 12),
        (2, 3),
        (3, 1),
        (3, 6),
        (3, 11),
    ]
    assert [
        (token.start_offset, token.end_offset)
        for token in tokens
    ] == [
        (0, 6),
        (7, 8),
        (8, 9),
        (9, 11),
        (11, 12),
        (16, 20),
        (21, 25),
        (26, 31),
        (31, 31),
    ]


# 此测试逐个使用偏移切片恢复 Token，覆盖字符串分号和双字符比较运算符。
def test_token_offsets_recover_original_lexemes() -> None:
    """确认所有非 EOF Token 的半开区间都能无损切回原始 lexeme。"""
    source = "name <> 'a;b';\nactive >= TRUE"
    tokens = tokenize(source)

    for token in tokens[:-1]:
        assert source[token.start_offset : token.end_offset] == token.lexeme

    eof = tokens[-1]
    assert eof.type is TokenType.EOF
    assert eof.start_offset == eof.end_offset == len(source)
