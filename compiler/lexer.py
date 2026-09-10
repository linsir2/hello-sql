"""模块 A 的词法分析器：把 SQL 源码文本转换为 Token 序列。

词法分析（Lexical Analysis）是编译流程的第一步。它不判断 SQL 语句的
结构是否正确，例如不会判断 SELECT 是否缺少 FROM；这些属于下一步的
语法分析器。Lexer 只回答三个问题：

1. 当前字符属于哪一个最小的 SQL 单元（Token）；
2. 这个 Token 在原 SQL 文本中的位置；
3. 文本中是否存在无法组成任何 Token 的非法内容。

本实现保留 docs/contract-v1.md 的 V1.1 词法规则，并按 V2 契约扩展 BOOLEAN、
逻辑表达式、表别名、INNER JOIN 与限定列所需的 Token。关键字大小写不敏感，
标识符仅支持 ASCII 字母、数字和下划线，字符串使用单引号及 ``''`` 转义。
词法器输出的 Token 会在 parser.py 中被消费，最终构建 contracts.ast 的 AST。
"""

from __future__ import annotations

from compiler.tokens import KEYWORDS, SourcePosition, Token, TokenType
from contracts.errors import ParseError


# 此表描述所有单字符分隔符和比较运算符。
# 双字符运算符（<>, <=, >=）必须优先处理，因此不放在此表中。
_SINGLE_CHAR_TOKENS: dict[str, TokenType] = {
    "(": TokenType.LPAREN,
    ")": TokenType.RPAREN,
    ",": TokenType.COMMA,
    ".": TokenType.DOT,
    "*": TokenType.STAR,
    ";": TokenType.SEMICOLON,
    "=": TokenType.EQ,
    "<": TokenType.LT,
    ">": TokenType.GT,
}


class Lexer:
    """扫描一条 SQL 文本并生成 Token 序列的有状态词法分析器。

    Lexer 保存当前扫描下标、行号和列号。每次调用 ``_advance`` 后，位置
    都会同步移动；因此所有生成的 Token 都能记录其第一个字符的准确位置。
    行与列遵循用户可见的 1-based 约定：第一个字符位于第 1 行第 1 列；
    Token 的字符偏移采用零基、左闭右开的范围，便于后续生成 SourceSpan。

    Args:
        sql: 本次需要扫描的一条原始 SQL 字符串。语句是否完整、是否只有一条，
            由后续 parser 判断；lexer 可以扫描多个分号或多个语句对应的 Token。
    """

    # 此构造函数初始化一次扫描所需的游标和源码位置状态。
    def __init__(self, sql: str) -> None:
        """保存 SQL 源码，并把扫描游标重置到输入的第一个字符。

        ``index`` 是 Python 字符串下标，用来切出 Token 的原始 lexeme；
        ``line``、``column`` 是面向错误提示的 1-based 源码坐标。三者都由
        ``_advance`` 统一维护，其他扫描函数不直接修改位置状态。
        """
        self._sql = sql
        self._index = 0
        self._line = 1
        self._column = 1

    # 此公开方法循环读取 SQL，产出供 parser 消费的完整 Token 列表。
    def tokenize(self) -> list[Token]:
        """将当前 SQL 文本扫描为 Token 列表，并在结尾追加 EOF Token。

        扫描优先级很重要：标识符、数字和字符串需要连续读取多个字符；比较
        运算符需要先识别双字符形式；最后才处理单字符符号。无法归入任何类别
        的字符会立刻抛出带准确位置的 ParseError（错误码为 E_SYNTAX）。

        Returns:
            按 SQL 原文顺序排列的 Token 列表，最后一个元素始终是 EOF。

        Raises:
            ParseError: 当输入出现非法字符、未闭合字符串或跨行字符串时抛出。
        """
        tokens: list[Token] = []

        while self._current_char() is not None:
            self._skip_whitespace()
            current = self._current_char()

            # 跳过末尾空白后可能已经到达输入结束，避免继续访问空字符。
            if current is None:
                break

            if self._is_identifier_start(current):
                tokens.append(self._scan_identifier_or_keyword())
            elif self._is_ascii_digit(current) or (
                current == "-" and self._is_ascii_digit(self._peek_char())
            ):
                tokens.append(self._scan_number())
            elif current == "'":
                tokens.append(self._scan_string())
            elif current in "<>":
                tokens.append(self._scan_comparison_operator())
            elif current in _SINGLE_CHAR_TOKENS:
                tokens.append(self._scan_single_char_token())
            else:
                raise ParseError(
                    self._line,
                    self._column,
                    f"unexpected character {current!r}",
                )

        # EOF 的位置是 SQL 最后一个字符之后的位置。parser 用它报告“输入
        # 意外结束”或检查语句后是否还有多余 Token。
        tokens.append(
            Token(
                type=TokenType.EOF,
                lexeme="",
                position=SourcePosition(self._line, self._column),
                start_offset=self._index,
                end_offset=self._index,
            )
        )
        return tokens

    # 此辅助方法读取当前字符，但不会移动扫描游标。
    def _current_char(self) -> str | None:
        """返回当前待扫描字符；如果游标已到输入末尾则返回 None。

        所有扫描函数先通过此方法观察字符，再决定是否调用 ``_advance``。
        这样可以避免越界访问，并使 EOF 的判断逻辑集中在一个位置。
        """
        if self._index >= len(self._sql):
            return None
        return self._sql[self._index]

    # 此辅助方法向前看一个字符，但不移动扫描游标。
    def _peek_char(self) -> str | None:
        """返回当前字符后的一个字符；没有下一个字符时返回 None。

        它用于识别带前导负号的数字，以及 ``<>``、``<=``、``>=`` 等双字符
        比较运算符。peek 不改变任何状态，因此后续仍由对应扫描函数实际消费。
        """
        next_index = self._index + 1
        if next_index >= len(self._sql):
            return None
        return self._sql[next_index]

    # 此辅助方法消费一个字符，并同步更新下标及 1-based 行列位置。
    def _advance(self) -> str:
        """消费当前字符并将游标移到下一个字符，返回被消费的字符。

        普通字符令列号加一。``\\n`` 会令行号加一并将列号重置为一；Windows
        风格的 ``\\r\\n`` 作为一个换行处理，避免把它误算成两行。单独的
        ``\\r`` 也被视为换行，便于在不同平台上给出稳定的错误位置。

        Raises:
            RuntimeError: 仅在内部调用错误、即已位于 EOF 时仍试图消费字符时抛出。
        """
        current = self._current_char()
        if current is None:
            raise RuntimeError("cannot advance past end of SQL input")

        if current == "\r":
            self._index += 1
            # 将 CRLF 视为一个换行：跳过紧随其后的 LF，但行号只增加一次。
            if self._current_char() == "\n":
                self._index += 1
            self._line += 1
            self._column = 1
        elif current == "\n":
            self._index += 1
            self._line += 1
            self._column = 1
        else:
            self._index += 1
            self._column += 1

        return current

    # 此辅助方法跳过 SQL Token 之间允许存在的空白字符。
    def _skip_whitespace(self) -> None:
        """连续跳过空格、制表符、换页符、垂直制表符和换行符。

        SQL 文法不把空白本身当作 Token。换行在普通语句位置与空格等价，
        但在字符串内部不允许；字符串扫描函数会在遇到换行时直接报错。
        """
        while True:
            current = self._current_char()
            if current is None:
                return
            if current in " \t\f\v\r\n":
                self._advance()
                continue
            return

    # 此辅助方法扫描一个普通标识符，或将其识别为大小写不敏感的保留字。
    def _scan_identifier_or_keyword(self) -> Token:
        """扫描 ``[A-Za-z_][A-Za-z0-9_]*``，生成 IDENTIFIER 或关键字 Token。

        词法契约要求关键字大小写不敏感，因此使用 ``upper()`` 在 KEYWORDS 中
        查找。若不在关键字表中，Token 类型为 IDENTIFIER，原始写法仍保留在lexeme 中；
        后续 parser 构建 AST 时负责统一转换成小写。
        """
        start_index = self._index
        start_position = SourcePosition(self._line, self._column)

        self._advance()
        while self._is_identifier_part(self._current_char()):
            self._advance()

        lexeme = self._sql[start_index : self._index]
        token_type = KEYWORDS.get(lexeme.upper(), TokenType.IDENTIFIER)
        return Token(
            token_type,
            lexeme,
            start_position,
            start_index,
            self._index,
        )

    # 此辅助方法扫描一个整数或小数字面量，并保留其原始文本。
    def _scan_number(self) -> Token:
        """扫描带可选前导负号的数字，返回 INTEGER_LITERAL 或 REAL_LITERAL。

        当前实现采用保守的 V1 数字形式：``-?digits`` 或
        ``-?digits '.' digits``。因此接受 18、-18、18.5、-0.5；
        不接受 .5、1. 或单独的 -。最终转换为 int/float 的工作由 parser 的``parse_value`` 完成，
        lexer 只负责识别数字的边界与类别。
        """
        start_index = self._index
        start_position = SourcePosition(self._line, self._column)

        if self._current_char() == "-":
            self._advance()

        while self._is_ascii_digit(self._current_char()):
            self._advance()

        token_type = TokenType.INTEGER_LITERAL
        # 小数点只有在其后紧跟至少一个数字时才属于当前数字 Token。
        if self._current_char() == "." and self._is_ascii_digit(self._peek_char()):
            token_type = TokenType.REAL_LITERAL
            self._advance()
            while self._is_ascii_digit(self._current_char()):
                self._advance()

        lexeme = self._sql[start_index : self._index]
        return Token(
            token_type,
            lexeme,
            start_position,
            start_index,
            self._index,
        )

    # 此辅助方法扫描一个单引号字符串，并验证字符串转义和换行约束。
    def _scan_string(self) -> Token:
        """扫描以单引号包围的字符串，支持 ``''`` 表示一个单引号。

        Token.lexeme 保留包括首尾引号在内的原 SQL 文本，例如 ``'it''s'``；
        parser 之后会去除外层引号并把连续的两个单引号还原成一个字符。若
        字符串在闭合前遇到换行或 EOF，则按照契约抛出 E_SYNTAX。
        """
        start_index = self._index
        start_position = SourcePosition(self._line, self._column)

        # 先消费字符串的开头单引号。
        self._advance()

        while True:
            current = self._current_char()
            if current is None:
                raise ParseError(
                    start_position.line,
                    start_position.column,
                    "unterminated string literal",
                )
            if current in "\r\n":
                raise ParseError(
                    start_position.line,
                    start_position.column,
                    "string literal cannot span lines",
                )
            if current != "'":
                self._advance()
                continue

            # 消费一个单引号后，连续的第二个单引号表示转义，而非字符串结束。
            self._advance()
            if self._current_char() == "'":
                self._advance()
                continue
            break

        lexeme = self._sql[start_index : self._index]
        return Token(
            TokenType.STRING_LITERAL,
            lexeme,
            start_position,
            start_index,
            self._index,
        )

    # 此辅助方法扫描 <、> 开头的比较运算符，并优先识别双字符形式。
    def _scan_comparison_operator(self) -> Token:
        """扫描 ``<``、``>``、``<>``、``<=`` 或 ``>=``，返回对应 Token。

        双字符运算符必须先处理，否则 ``>=`` 会被错误拆为 ``>`` 和 ``=``。
        由于调用方已确认当前字符是 < 或 >，本函数只处理契约允许的比较符号。
        """
        start_index = self._index
        start_position = SourcePosition(self._line, self._column)
        first = self._advance()
        second = self._current_char()

        if first == "<" and second == ">":
            self._advance()
            return Token(TokenType.NE, "<>", start_position, start_index, self._index)
        if first == "<" and second == "=":
            self._advance()
            return Token(TokenType.LE, "<=", start_position, start_index, self._index)
        if first == ">" and second == "=":
            self._advance()
            return Token(TokenType.GE, ">=", start_position, start_index, self._index)
        if first == "<":
            return Token(TokenType.LT, "<", start_position, start_index, self._index)
        return Token(TokenType.GT, ">", start_position, start_index, self._index)

    # 此辅助方法扫描一个确定只有一个字符的分隔符或等号。
    def _scan_single_char_token(self) -> Token:
        """扫描单字符符号，并根据 _SINGLE_CHAR_TOKENS 返回其 Token 类型。

        调用本函数前，tokenize 已保证当前字符存在于映射中；该前提使此函数
        只承担“消费一个字符并生成 Token”的职责，而不混入错误分支。
        """
        start_index = self._index
        start_position = SourcePosition(self._line, self._column)
        character = self._advance()
        return Token(
            _SINGLE_CHAR_TOKENS[character],
            character,
            start_position,
            start_index,
            self._index,
        )

    # 此静态辅助方法判断字符能否作为 ASCII 标识符的第一个字符。
    @staticmethod
    def _is_identifier_start(character: str | None) -> bool:
        """判断字符是否满足标识符首字符规则 ``[A-Za-z_]``。

        不能直接使用 str.isalpha()，因为该方法会接受中文等 Unicode 字母，
        而项目契约只允许 ASCII 英文字母。None 表示 EOF，不属于标识符。
        """
        if character is None:
            return False
        return (
            "a" <= character <= "z"
            or "A" <= character <= "Z"
            or character == "_"
        )

    # 此静态辅助方法判断字符能否作为 ASCII 标识符的后续字符。
    @staticmethod
    def _is_identifier_part(character: str | None) -> bool:
        """判断字符是否满足标识符后续字符规则 ``[A-Za-z0-9_]``。

        该规则在首字符规则上增加数字；因此 users2 合法，而 2users 会在
        lexer 中被识别为数字 2 后再识别为标识符 users，最终由 parser 报错。
        """
        return Lexer._is_identifier_start(character) or Lexer._is_ascii_digit(character)

    # 此静态辅助方法判断字符是否为 ASCII 数字，避免接受其他 Unicode 数字。
    @staticmethod
    def _is_ascii_digit(character: str | None) -> bool:
        """判断字符是否属于 ASCII 数字范围 ``0`` 到 ``9``。

        None 表示 EOF，返回 False。采用显式范围判断而非 str.isdigit()，
        是为了严格遵守 SQL 词法规则并避免非 ASCII 数字进入数值字面量。
        """
        return character is not None and "0" <= character <= "9"


# 此便捷函数是 lexer 模块对 parser 的简单入口，避免 parser 关心 Lexer 的
# 构造细节；它不增加任何语义，只是创建 Lexer 后立即调用 tokenize。
def tokenize(sql: str) -> list[Token]:
    """将一条 SQL 字符串转换为 Token 列表。

    Args:
        sql: 待扫描的原始 SQL 文本。

    Returns:
        以 EOF 结尾的 Token 列表。

    Raises:
        ParseError: 当文本中出现无法词法化的内容时抛出。
    """
    return Lexer(sql).tokenize()


__all__ = ["Lexer", "tokenize"]
