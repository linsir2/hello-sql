"""模块 A 的语法分析器基础设施。

本文件目前已实现全部 V1.1 语句的语法层工作：Token 游标读取、Token 类型
检查、标识符标准化、字面量转换、数据库/表 DDL、INSERT、SELECT、WHERE、
UPDATE 与 DELETE。Parser 的职责到构建 AST 为止，所有语义检查和执行均由
runner 与 storage 模块完成。

Parser 的输入是 lexer.tokenize() 生成、且以 EOF 结尾的 Token 序列。它的
输出最终会是 contracts.ast 中定义的 Statement。所有语法层的失败统一通过
contracts.errors.ParseError 报告，因此调用者可以稳定获得 E_SYNTAX 与行列号。
"""

from __future__ import annotations

from collections.abc import Sequence

from compiler.tokens import Token, TokenType
from contracts.ast import (
    And,
    Assignment,
    Cmp,
    Column,
    ColumnDef,
    CreateDatabaseStmt,
    CreateTableStmt,
    DropDatabaseStmt,
    DropTableStmt,
    DeleteStmt,
    Expr,
    InsertStmt,
    Literal,
    SelectStmt,
    SqlType,
    Statement,
    UseDatabaseStmt,
    UpdateStmt,
    Value,
)
from contracts.errors import ParseError


# 此映射定义 CREATE TABLE 中类型关键字与 AST SqlType 枚举的固定对应关系。
# Parser 使用它完成语法层转换；值是否匹配列类型仍属于 C/B 的职责。
_SQL_TYPE_TOKENS: dict[TokenType, SqlType] = {
    TokenType.KW_INT: SqlType.INT,
    TokenType.KW_TEXT: SqlType.TEXT,
    TokenType.KW_REAL: SqlType.REAL,
}


# 此映射定义 WHERE 中比较运算符 Token 与 AST Cmp.op 字符串的固定对应关系。
# 只包含契约允许的六种运算符；运算结果的真假判断由 C 在执行时完成。
_COMPARISON_OPERATORS: dict[TokenType, str] = {
    TokenType.EQ: "=",
    TokenType.NE: "<>",
    TokenType.LT: "<",
    TokenType.LE: "<=",
    TokenType.GT: ">",
    TokenType.GE: ">=",
}


class Parser:
    """消费 Token 序列、维护当前游标并提供语法解析通用操作。

    Parser 不保存 SQL 原始字符串，也不扫描字符；其唯一输入是 Lexer 已生成的
    Token 序列。这种职责拆分使词法规则和文法规则彼此独立：lexer 负责把
    ``SELECT`` 识别为 ``KW_SELECT``，parser 负责判断它在当前位置是否符合
    文法要求。

    Args:
        tokens: 必须以 TokenType.EOF 结束的 Token 序列。Lexer 始终满足这个
            前提；若其他代码手动构造不完整序列，则视为 Parser 的使用错误。
    """

    # 此构造函数固定 Token 序列，并把读取游标放在第一个 Token 上。
    def __init__(self, tokens: Sequence[Token]) -> None:
        """初始化语法分析状态，并验证 Token 序列的基本完整性。

        将输入转为 tuple 可避免外部代码在解析过程中修改 Token 列表，导致游标位置与 Token 内容不一致。
        EOF 是 parser 判断输入结束的唯一哨兵，
        因此缺失 EOF 属于内部调用错误，而不是用户 SQL 的语法错误。

        Raises:
            ValueError: tokens 为空，或最后一个 Token 不是 EOF 时抛出。
        """
        self._tokens = tuple(tokens)
        if not self._tokens:
            raise ValueError("parser requires a non-empty token sequence")
        if self._tokens[-1].type is not TokenType.EOF:
            raise ValueError("parser token sequence must end with EOF")
        self._index = 0

    # 此公共方法查看当前或后续 Token，但不移动 parser 的读取游标。
    def peek(self, offset: int = 0) -> Token:
        """返回当前 Token 之后 offset 个位置的 Token，且不消费任何 Token。

        ``peek()`` 等同于 ``peek(0)``，常用于判断当前语句以哪个关键字开头；
        ``peek(1)`` 常用于区分 ``CREATE DATABASE`` 与 ``CREATE TABLE``。
        当请求位置超过 EOF 时，持续返回最后一个 EOF Token，使后续解析方法
        能将“输入意外结束”报告为 ParseError，而不是抛出数组越界异常。

        Args:
            offset: 相对于当前游标的非负偏移量。

        Raises:
            ValueError: offset 为负数时抛出，避免向后查看已消费的语法上下文。
        """
        if offset < 0:
            raise ValueError("peek offset must not be negative")
        target_index = self._index + offset
        if target_index >= len(self._tokens):
            return self._tokens[-1]
        return self._tokens[target_index]

    # 此公共方法消费当前 Token，并将读取游标移动到下一个 Token。
    def advance(self) -> Token:
        """返回当前 Token，并在非 EOF 时向前移动一个位置。

        EOF 不会被越过：即使重复调用 advance()，也持续返回最后一个 EOF Token。
        该设计使 parser 在处理不完整输入时保持稳定，所有错误仍可使用 EOF 的
        源码位置构造 ParseError。
        """
        token = self.peek()
        if token.type is not TokenType.EOF:
            self._index += 1
        return token

    # 此公共方法断言当前位置必须是指定类型，并在成功时消费该 Token。
    def expect(
        self,
        expected: TokenType | tuple[TokenType, ...],
        description: str | None = None,
    ) -> Token:
        """校验当前 Token 类型并消费它；不匹配时抛出带位置的 ParseError。

        解析具体文法时应优先使用本方法，例如 ``expect(TokenType.KW_FROM)``。
        它把“检查类型、生成错误信息、移动游标”统一封装，避免每个语句解析
        函数自行实现不同风格的错误处理。多个允许类型可通过 tuple 传入。

        Args:
            expected: 允许出现的一个 TokenType，或由多个 TokenType 组成的 tuple。
            description: 可选的人类可读说明；提供后将用于错误信息中的 expected
                部分，例如 ``"a column type"``。

        Returns:
            已经被消费的、类型符合要求的 Token。

        Raises:
            ParseError: 当前 Token 类型不在 expected 中时抛出，位置指向当前
                Token 的起始位置；若当前为 EOF，则指向输入结束位置。
            ValueError: expected 为空 tuple 时抛出，表示 Parser 内部调用错误。
        """
        expected_types = (expected,) if isinstance(expected, TokenType) else expected
        if not expected_types:
            raise ValueError("expected token types must not be empty")

        current = self.peek()
        if current.type in expected_types:
            return self.advance()

        expected_text = description or self._format_expected_types(expected_types)
        actual_text = self._format_actual_token(current)
        raise ParseError(
            current.position.line,
            current.position.column,
            f"expected {expected_text}, found {actual_text}",
        )

    # 此公共方法读取一个非保留字标识符，并转换为 AST 规定的小写名称。
    def parse_identifier(self) -> str:
        """消费一个 IDENTIFIER Token，并返回其小写形式。

        Lexer 已将保留字标记为 KW_* 类型，因此 ``parse_identifier`` 只接受普通 IDENTIFIER；
        这保证 CREATE TABLE select (...) 之类的输入不会把保留字错误地当成表名或列名。
        返回值统一小写，满足 AST 对 database、table、column 名称的冻结不变式。

        Returns:
            小写的标识符文本，例如 ``"Users"`` 返回 ``"users"``。

        Raises:
            ParseError: 当前 Token 不是普通标识符时抛出。
        """
        token = self.expect(TokenType.IDENTIFIER, "an identifier")
        return token.lexeme.lower()

    # 此公共方法读取一个 SQL 字面量，并转换成 contracts.ast.Value 原生值。
    def parse_value(self) -> Value:
        """消费整数、小数或字符串 Token，并返回对应的 Python 原生值。

        AST 中禁止把数字保留为字符串，所以 ``"18"`` 必须转换为 int 18，
        ``"18.5"`` 必须转换为 float 18.5。字符串 Token 保留原始引号和转义，
        因此通过 ``_decode_string_literal`` 去除外层单引号，并把 ``''`` 恢复为
        单个单引号。值是否符合某张表列的 SQL 类型不由 A 判断，属于 C/B。

        Returns:
            int、float 或 str，符合 contracts.ast.Value 的类型别名。

        Raises:
            ParseError: 当前 Token 不是字面量时抛出。若手工构造了格式错误的
                STRING_LITERAL Token，也会在当前位置抛出。
        """
        token = self.peek()
        if token.type is TokenType.INTEGER_LITERAL:
            self.advance()
            return int(token.lexeme)
        if token.type is TokenType.REAL_LITERAL:
            self.advance()
            return float(token.lexeme)
        if token.type is TokenType.STRING_LITERAL:
            self.advance()
            return self._decode_string_literal(token)

        expected_types = (
            TokenType.INTEGER_LITERAL,
            TokenType.REAL_LITERAL,
            TokenType.STRING_LITERAL,
        )
        expected_text = self._format_expected_types(expected_types)
        raise ParseError(
            token.position.line,
            token.position.column,
            f"expected {expected_text}, found {self._format_actual_token(token)}",
        )

    # 此公共方法解析一条完整 SQL 输入，并保证末尾没有第二条语句或多余内容。
    def parse(self) -> Statement:
        """解析一条完整 SQL 语句，允许末尾至多存在一个分号。

        契约规定一次 parse 只接收一条 SQL，因此本方法先调用
        ``parse_statement`` 解析主体，再消费可选分号，最后强制要求 EOF。
        例如 ``CREATE DATABASE shop;`` 合法，而 ``USE main;;`` 与
        ``USE main DROP DATABASE shop`` 都会在剩余 Token 处报 E_SYNTAX。

        Returns:
            当前已实现语句对应的 AST Statement。

        Raises:
            ParseError: 输入为空、语句类型不受支持、语法结构不完整，或语句后
                仍存在第二个分号、第二条语句或其他多余内容时抛出。
        """
        statement = self.parse_statement()
        if self.peek().type is TokenType.SEMICOLON:
            self.advance()
        self.expect(TokenType.EOF, "end of input after one SQL statement")
        return statement

    # 此公共方法根据语句第一个关键字分派到相应的具体语句解析方法。
    def parse_statement(self) -> Statement:
        """解析当前游标处的一条 SQL 语句主体，但不处理末尾分号和 EOF。

        当前阶段已分派 V1.1 的全部九类语句：CREATE、DROP、USE、INSERT、
        SELECT、UPDATE、DELETE。CREATE 和 DROP 会在第二个关键字处继续区分
        DATABASE 与 TABLE；其他关键字各有唯一的语句解析函数。

        Returns:
            已构造的任意 V1.1 Statement AST 节点。

        Raises:
            ParseError: 当前 Token 不是本阶段支持的语句起始关键字时抛出。
        """
        current = self.peek()
        if current.type is TokenType.KW_CREATE:
            return self._parse_create_statement()
        if current.type is TokenType.KW_DROP:
            return self._parse_drop_statement()
        if current.type is TokenType.KW_USE:
            return self._parse_use_database_statement()
        if current.type is TokenType.KW_INSERT:
            return self._parse_insert_statement()
        if current.type is TokenType.KW_SELECT:
            return self._parse_select_statement()
        if current.type is TokenType.KW_UPDATE:
            return self._parse_update_statement()
        if current.type is TokenType.KW_DELETE:
            return self._parse_delete_statement()

        raise ParseError(
            current.position.line,
            current.position.column,
            "expected CREATE, DROP, USE, INSERT, SELECT, UPDATE, or DELETE, "
            f"found {self._format_actual_token(current)}",
        )

    # 此内部方法在消费 CREATE 后，识别它创建的是数据库还是表。
    def _parse_create_statement(self) -> CreateDatabaseStmt | CreateTableStmt:
        """解析 ``CREATE DATABASE id`` 或 ``CREATE TABLE id (...)``。

        CREATE 后的第二个关键字决定分支。这里仅依据 SQL 语法选择 AST 类型，
        不检查同名数据库或表是否已经存在；那些状态相关检查必须由 C/B 完成。

        Returns:
            CreateDatabaseStmt 或 CreateTableStmt。

        Raises:
            ParseError: CREATE 后缺少 DATABASE/TABLE，或后续结构不符合相应
                文法时抛出。
        """
        self.expect(TokenType.KW_CREATE)
        target = self.peek()
        if target.type is TokenType.KW_DATABASE:
            return self._parse_create_database_statement()
        if target.type is TokenType.KW_TABLE:
            return self._parse_create_table_statement()

        raise ParseError(
            target.position.line,
            target.position.column,
            f"expected DATABASE or TABLE after CREATE, found {self._format_actual_token(target)}",
        )

    # 此内部方法在消费 DROP 后，识别它删除的是数据库还是表。
    def _parse_drop_statement(self) -> DropDatabaseStmt | DropTableStmt:
        """解析 ``DROP DATABASE id`` 或 ``DROP TABLE id``。

        DROP 后的第二个关键字决定 AST 节点。Parser 只验证语法形式，不能也
        不应判断数据库是否为 main、是否正在使用，或目标是否真实存在。

        Returns:
            DropDatabaseStmt 或 DropTableStmt。

        Raises:
            ParseError: DROP 后没有 DATABASE/TABLE，或没有合法名称时抛出。
        """
        self.expect(TokenType.KW_DROP)
        target = self.peek()
        if target.type is TokenType.KW_DATABASE:
            return self._parse_drop_database_statement()
        if target.type is TokenType.KW_TABLE:
            return self._parse_drop_table_statement()

        raise ParseError(
            target.position.line,
            target.position.column,
            f"expected DATABASE or TABLE after DROP, found {self._format_actual_token(target)}",
        )

    # 此内部方法解析 CREATE DATABASE 的名称，并直接构建其 AST 节点。
    def _parse_create_database_statement(self) -> CreateDatabaseStmt:
        """解析已消费 CREATE 后的 ``DATABASE id``，返回 CreateDatabaseStmt。

        ``parse_identifier`` 统一将库名转小写，因此 ``CREATE DATABASE Shop``
        产出的 AST 为 ``CreateDatabaseStmt(name=\"shop\")``。重名库的
        E_DATABASE_EXISTS 不是词法或语法错误，必须留给 DatabaseServer。
        """
        self.expect(TokenType.KW_DATABASE)
        return CreateDatabaseStmt(name=self.parse_identifier())

    # 此内部方法解析 DROP DATABASE 的名称，并直接构建其 AST 节点。
    def _parse_drop_database_statement(self) -> DropDatabaseStmt:
        """解析已消费 DROP 后的 ``DATABASE id``，返回 DropDatabaseStmt。

        例如 ``DROP DATABASE Shop`` 会产生 name 为 ``\"shop\"`` 的 AST。
        默认库 main 能否删除、当前库能否删除及库是否存在均属于运行/存储层，
        因此本方法只消费语法所需的关键字和标识符。
        """
        self.expect(TokenType.KW_DATABASE)
        return DropDatabaseStmt(name=self.parse_identifier())

    # 此内部方法解析 USE id；正式文法中 USE 后不能再出现 DATABASE 关键字。
    def _parse_use_database_statement(self) -> UseDatabaseStmt:
        """解析 ``USE id``，返回 UseDatabaseStmt。

        V1.1 的正式文法和 golden 用例规定形式是 ``USE shop``，而不是
        ``USE DATABASE shop``。因此本方法在 USE 后直接调用 parse_identifier；
        若遇到保留字 DATABASE，会正确报告 E_SYNTAX。
        """
        self.expect(TokenType.KW_USE)
        return UseDatabaseStmt(name=self.parse_identifier())

    # 此内部方法解析 CREATE TABLE 的表名和一组有序列定义。
    def _parse_create_table_statement(self) -> CreateTableStmt:
        """解析 ``TABLE id '(' colDef (',' colDef)* ')'``，返回 CreateTableStmt。

        列定义必须至少有一项，并按 SQL 书写顺序保存在 AST 中；这个顺序将来
        决定 B 的物理存储列顺序和 SELECT * 的展开顺序。A 不检查列名是否重复：
        g18 要求重复列在 B.create_table 边界以 E_DUP_COLUMN 报告。
        """
        self.expect(TokenType.KW_TABLE)
        table_name = self.parse_identifier()
        columns = self._parse_column_definitions()
        return CreateTableStmt(table=table_name, columns=columns)

    # 此内部方法解析 DROP TABLE 的表名，并直接构建其 AST 节点。
    def _parse_drop_table_statement(self) -> DropTableStmt:
        """解析已消费 DROP 后的 ``TABLE id``，返回 DropTableStmt。

        本方法只负责将表名标准化并放入 AST；表是否存在、是否需要删除文件等
        运行期问题不属于 A。``DROP TABLE users`` 的表名始终输出为 ``\"users\"``。
        """
        self.expect(TokenType.KW_TABLE)
        return DropTableStmt(table=self.parse_identifier())

    # 此内部方法解析 INSERT INTO ... VALUES (...)，并构建 InsertStmt 节点。
    def _parse_insert_statement(self) -> InsertStmt:
        """解析 ``INSERT INTO id VALUES '(' value (',' value)* ')'``。

        INSERT 的 values 至少要有一个字面量；每个字面量通过 parse_value 转为
        int、float 或 str，并按书写顺序放入不可变元组。A 不知道目标表的列数
        或列类型，因此“值数量不匹配”和“值类型不匹配”都不能在此处判断，必须
        把完整 InsertStmt 交给 C/B 处理。

        Returns:
            包含小写表名与原生 Python 值元组的 InsertStmt。

        Raises:
            ParseError: 缺少 INTO、表名、VALUES、括号、字面量、逗号，或使用
                非法值类型时抛出。
        """
        self.expect(TokenType.KW_INSERT)
        self.expect(TokenType.KW_INTO, "INTO after INSERT")
        table_name = self.parse_identifier()
        self.expect(TokenType.KW_VALUES, "VALUES after INSERT table name")
        values = self._parse_insert_values()
        return InsertStmt(table=table_name, values=values)

    # 此内部方法解析 INSERT 的圆括号值列表，并保证列表至少包含一个字面量。
    def _parse_insert_values(self) -> tuple[Value, ...]:
        """解析 ``'(' value (',' value)* ')'``，返回按输入顺序排列的值元组。

        文法中 value 没有方括号，因此空列表 ``VALUES ()`` 不合法。本方法在
        左括号后立刻调用 parse_value，从而在右括号位置得到准确的语法错误。
        逗号后也必须再出现一个 value，因此尾逗号同样被拒绝。

        Returns:
            至少包含一个 int、float 或 str 的 tuple。
        """
        self.expect(TokenType.LPAREN, "'(' after VALUES")
        values = [self.parse_value()]

        while self.peek().type is TokenType.COMMA:
            self.advance()
            values.append(self.parse_value())

        self.expect(TokenType.RPAREN, "')' after INSERT values")
        return tuple(values)

    # 此内部方法解析 SELECT 的投影、FROM 和可选 WHERE 子句，并构建 SelectStmt。
    def _parse_select_statement(self) -> SelectStmt:
        """解析 ``SELECT selectList FROM id [WHERE cond]``，返回 SelectStmt。

        SELECT 的投影列表可以是 ``*``，也可以是一个或多个列名；显式列名按
        原书写顺序保留，并允许重复。WHERE 存在时由 ``_parse_optional_where``
        构建 Cmp/And 表达式；不存在时 AST 的 where 字段为 None。A 仅记录
        条件结构，不能扫描数据或判断条件是否命中任何行。

        Returns:
            columns 为 None（SELECT *）或列名元组的 SelectStmt。

        Raises:
            ParseError: 缺少选择列表、FROM、表名，或 WHERE 不符合 V1 表达式
                文法时抛出。
        """
        self.expect(TokenType.KW_SELECT)
        columns = self._parse_select_list()
        self.expect(TokenType.KW_FROM, "FROM after SELECT list")
        table_name = self.parse_identifier()
        where = self._parse_optional_where()
        return SelectStmt(columns=columns, table=table_name, where=where)

    # 此内部方法解析 SELECT 的 * 或显式列名列表，并保留列名的书写顺序。
    def _parse_select_list(self) -> tuple[str, ...] | None:
        """解析 ``'*'`` 或 ``id (',' id)*``，返回 None 或小写列名元组。

        AST 契约约定 SELECT * 必须用 columns=None 表示，不能把星号保存为
        ``('*',)``。显式列列表允许重复，例如 ``SELECT id, id FROM users``；
        A 只保留语法顺序，C 在执行时按该顺序投影结果。

        Returns:
            SELECT * 时返回 None；显式列列表时返回至少一个小写列名的 tuple。

        Raises:
            ParseError: SELECT 后没有 * 或标识符、逗号后缺少列名，或 * 后仍有
                逗号/额外列名时，会由本方法或后续 FROM 检查抛出。
        """
        if self.peek().type is TokenType.STAR:
            self.advance()
            return None

        columns = [self.parse_identifier()]
        while self.peek().type is TokenType.COMMA:
            self.advance()
            columns.append(self.parse_identifier())
        return tuple(columns)

    # 此内部方法识别可选 WHERE，并在存在时把条件子句交给完整表达式解析。
    def _parse_optional_where(self) -> Expr | None:
        """解析可选的 ``WHERE cond``；没有 WHERE 时返回 None。

        SELECT、UPDATE、DELETE 共用同一套 WHERE 文法，因此将可选入口集中在
        一个方法中，保证三类语句产生完全相同的 Cmp/And AST 形状。A 不执行
        条件判断，只将列名、比较符和字面量保存给 C 的运行层。

        Returns:
            不存在 WHERE 时返回 None；存在时返回 Cmp 或由 And 连接的 Expr。
        """
        if self.peek().type is not TokenType.KW_WHERE:
            return None
        self.advance()
        return self._parse_where_expression()

    # 此内部方法解析至少一个比较条件，并将多个 AND 条件组织为左结合 And 树。
    def _parse_where_expression(self) -> Expr:
        """解析 ``cmp { AND cmp }``，返回 Cmp 或左结合的 And 表达式树。

        例如 ``a = 1 AND b >= 2 AND c <> 'x'`` 被构造为
        ``And(And(cmp_a, cmp_b), cmp_c)``。左结合形状稳定、易于 C 逐层求值，
        同时满足 V1 只支持 AND、没有 OR 和括号的契约限制。

        Returns:
            单个条件时返回 Cmp；两个及以上条件时返回 And。

        Raises:
            ParseError: WHERE 后缺少比较条件，或 AND 后缺少下一个比较条件时抛出。
        """
        expression: Expr = self._parse_comparison()

        while self.peek().type is TokenType.KW_AND:
            self.advance()
            expression = And(left=expression, right=self._parse_comparison())

        return expression

    # 此内部方法解析“列 比较运算符 字面量”这一条 V1 最小比较表达式。
    def _parse_comparison(self) -> Cmp:
        """解析 ``id op value``，并构建 ``Cmp(Column, op, Literal)``。

        左操作数只能是列名，因此使用 parse_identifier；右操作数只能是字面量，
        因此使用 parse_value。列与列比较、函数、算术表达式、OR、括号都没有
        对应的解析分支，会在其出现位置抛出 E_SYNTAX。

        Returns:
            左侧为 Column、右侧为 Literal 的 Cmp AST 节点。

        Raises:
            ParseError: 缺少列名、使用了不支持的比较符，或右侧不是字面量时抛出。
        """
        column = Column(name=self.parse_identifier())
        operator = self._parse_comparison_operator()
        value = Literal(value=self.parse_value())
        return Cmp(left=column, op=operator, right=value)

    # 此内部方法将当前比较运算符 Token 转换为 AST Cmp.op 所需的字符串。
    def _parse_comparison_operator(self) -> str:
        """消费 ``= <> < <= > >=`` 中的一种比较运算符，返回其原 SQL 写法。

        Lexer 已经保证多字符运算符不会被拆开；本方法只需用映射验证它是否属于
        契约允许的六种类型。运算符的类型兼容性和实际比较结果由 C 负责。

        Returns:
            六种契约比较符之一。

        Raises:
            ParseError: 当前 Token 不是允许的比较运算符时抛出。
        """
        token = self.peek()
        operator = _COMPARISON_OPERATORS.get(token.type)
        if operator is None:
            raise ParseError(
                token.position.line,
                token.position.column,
                "expected =, <>, <, <=, >, or >=, "
                f"found {self._format_actual_token(token)}",
            )
        self.advance()
        return operator

    # 此内部方法解析 UPDATE 的表名、SET 赋值列表和可选 WHERE，并构建 UpdateStmt。
    def _parse_update_statement(self) -> UpdateStmt:
        """解析 ``UPDATE id SET assign (',' assign)* [WHERE cond]``。

        UPDATE 至少包含一项赋值；可选 WHERE 与 SELECT、DELETE 使用同一个
        表达式解析方法。A 只保留赋值的 SQL 书写顺序，不能也不应查找表、列或
        行。C 在执行前校验列和值，并落实“同列重复赋值时后者覆盖前者”。

        Returns:
            包含小写表名、赋值元组和可选 Expr 的 UpdateStmt。

        Raises:
            ParseError: 缺少表名、SET、赋值、逗号后的赋值，或 WHERE 文法错误
                时抛出。
        """
        self.expect(TokenType.KW_UPDATE)
        table_name = self.parse_identifier()
        self.expect(TokenType.KW_SET, "SET after UPDATE table name")
        assignments = self._parse_assignments()
        where = self._parse_optional_where()
        return UpdateStmt(table=table_name, assignments=assignments, where=where)

    # 此内部方法解析 UPDATE SET 后至少一项赋值和其逗号分隔关系。
    def _parse_assignments(self) -> tuple[Assignment, ...]:
        """解析 ``assign (',' assign)*``，返回按 SQL 顺序排列的 Assignment 元组。

        文法要求至少一项赋值，因此 UPDATE ... SET WHERE ... 会在 WHERE 处
        报错。重复列名不在 A 去重：例如 ``SET age=1, age=2`` 必须保留两项，
        由 C 按顺序执行“后者覆盖前者”的已冻结语义。

        Returns:
            至少包含一个 Assignment 的 tuple。
        """
        assignments = [self._parse_assignment()]

        while self.peek().type is TokenType.COMMA:
            self.advance()
            assignments.append(self._parse_assignment())

        return tuple(assignments)

    # 此内部方法解析一项“列名 = 字面量”赋值，并构建 Assignment AST 节点。
    def _parse_assignment(self) -> Assignment:
        """解析 ``id '=' value``，返回列名已小写、值已原生化的 Assignment。

        Assignment 使用单独的 ``=``，不接受 WHERE 比较中的 <、>= 等运算符。
        A 不检查目标列是否存在，也不检查值是否符合列类型；这些均依赖 C/B 的
        schema 和存储边界。

        Returns:
            一个 Assignment AST 节点。

        Raises:
            ParseError: 缺少列名、等号或字面量时抛出。
        """
        column_name = self.parse_identifier()
        self.expect(TokenType.EQ, "'=' in UPDATE assignment")
        return Assignment(column=column_name, value=self.parse_value())

    # 此内部方法解析 DELETE 的 FROM、表名和可选 WHERE，并构建 DeleteStmt。
    def _parse_delete_statement(self) -> DeleteStmt:
        """解析 ``DELETE FROM id [WHERE cond]``，返回 DeleteStmt。

        没有 WHERE 的 DELETE 在语法上合法，where 字段为 None，执行时由 C
        解释为删除当前表的全部行。带 WHERE 时只构建条件 AST；行扫描、匹配
        row_id 收集和逐行删除均属于 C 与 B 的协作边界。

        Returns:
            包含小写表名和可选 Expr 的 DeleteStmt。

        Raises:
            ParseError: 缺少 FROM、表名，或 WHERE 条件不符合 V1 文法时抛出。
        """
        self.expect(TokenType.KW_DELETE)
        self.expect(TokenType.KW_FROM, "FROM after DELETE")
        table_name = self.parse_identifier()
        where = self._parse_optional_where()
        return DeleteStmt(table=table_name, where=where)

    # 此内部方法解析 CREATE TABLE 圆括号中的至少一个列定义及其逗号分隔关系。
    def _parse_column_definitions(self) -> tuple[ColumnDef, ...]:
        """解析 ``'(' colDef (',' colDef)* ')'``，并返回不可变的列定义元组。

        此方法在读取左括号后立即要求一个列定义，因此空列列表 ``()`` 会在
        右括号处报语法错误。每逗号后必须再有一项列定义，故尾逗号
        ``(id INT,)`` 同样会报错。重复列名保持原样交给 B 校验。

        Returns:
            按输入顺序排列、至少包含一个 ColumnDef 的 tuple。
        """
        self.expect(TokenType.LPAREN, "'(' after table name")
        columns = [self._parse_column_definition()]

        while self.peek().type is TokenType.COMMA:
            self.advance()
            columns.append(self._parse_column_definition())

        self.expect(TokenType.RPAREN, "')' after column definitions")
        return tuple(columns)

    # 此内部方法解析单个“列名 + SQL 类型”结构，并构建 ColumnDef。
    def _parse_column_definition(self) -> ColumnDef:
        """解析 ``id type``，并返回一个 ColumnDef AST 节点。

        列名经 parse_identifier 统一转为小写，类型经 parse_sql_type 转为
        SqlType 枚举。该方法只验证 CREATE TABLE 的局部语法，不检查与前面
        列名是否重复，也不产生任何存储元数据。
        """
        column_name = self.parse_identifier()
        column_type = self._parse_sql_type()
        return ColumnDef(name=column_name, type=column_type)

    # 此内部方法把 INT、TEXT、REAL 类型关键字映射为 contracts.ast.SqlType。
    def _parse_sql_type(self) -> SqlType:
        """消费一个 SQL 类型关键字，并返回对应的 SqlType 枚举成员。

        V1.1 只支持 INT、TEXT、REAL。遇到其他关键字、标识符或 EOF 时，方法
        使用当前 Token 的位置抛出 ParseError；这样 CREATE TABLE 的列类型错误
        能准确定位到错误位置。

        Returns:
            SqlType.INT、SqlType.TEXT 或 SqlType.REAL。

        Raises:
            ParseError: 当前 Token 不是三种受支持类型关键字之一时抛出。
        """
        token = self.peek()
        sql_type = _SQL_TYPE_TOKENS.get(token.type)
        if sql_type is None:
            raise ParseError(
                token.position.line,
                token.position.column,
                f"expected INT, TEXT, or REAL, found {self._format_actual_token(token)}",
            )
        self.advance()
        return sql_type

    # 此内部方法将 lexer 已验证过的单引号字符串转换为最终的 Python str。
    def _decode_string_literal(self, token: Token) -> str:
        """移除字符串的外层单引号，并将 SQL 的 ``''`` 转义还原为 ``'``。

        Lexer 正常生成的 STRING_LITERAL 一定具有首尾单引号且不跨行。本方法仍
        做防御性检查，避免未来单元测试或其他模块手工传入错误 Token 时泄漏
        ValueError。此检查失败仍按语法错误报告，位置指向字符串开头。

        Args:
            token: 类型应为 STRING_LITERAL 的 Token。

        Returns:
            不含外层引号、已完成单引号反转义的 Python 字符串。

        Raises:
            ParseError: token 的文本不具有合法字符串外层引号时抛出。
        """
        lexeme = token.lexeme
        if len(lexeme) < 2 or not lexeme.startswith("'") or not lexeme.endswith("'"):
            raise ParseError(
                token.position.line,
                token.position.column,
                "invalid string literal token",
            )
        return lexeme[1:-1].replace("''", "'")

    # 此内部方法把一组 TokenType 格式化为适合错误消息阅读的文本。
    @staticmethod
    def _format_expected_types(expected_types: tuple[TokenType, ...]) -> str:
        """将允许的 Token 类型转换为错误消息中的 expected 描述。

        关键字显示为其 SQL 写法，例如 KW_FROM 显示为 ``FROM``；其他 Token
        显示为小写的类型名。多个候选项使用 ``or`` 连接，使报错能直接说明
        当前位置可接受哪些 token。
        """
        return " or ".join(Parser._format_token_type(token_type) for token_type in expected_types)

    # 此内部方法把实际遇到的 Token 格式化为适合错误消息阅读的文本。
    @staticmethod
    def _format_actual_token(token: Token) -> str:
        """生成错误消息中的 found 描述，并保留实际 token 文本。

        EOF 没有原始文本，因此显示为 ``end of input``。其他 Token 同时显示
        类型和 lexeme，例如 ``identifier 'users'``，便于用户定位错误原因。
        """
        if token.type is TokenType.EOF:
            return "end of input"
        return f"{Parser._format_token_type(token.type)} {token.lexeme!r}"

    # 此内部方法将 TokenType 转成 SQL 风格、稳定且易读的名称。
    @staticmethod
    def _format_token_type(token_type: TokenType) -> str:
        """将内部 TokenType 名称转换为面向用户的错误消息文本。

        KW_ 前缀的枚举值代表 SQL 保留字，应显示为大写 SQL 拼写；其余类型
        使用小写枚举名，例如 INTEGER_LITERAL 显示为 ``integer_literal``。
        该方法只负责展示，不影响 parser 的类型比较逻辑。
        """
        if token_type.name.startswith("KW_"):
            return token_type.name.removeprefix("KW_")
        return token_type.name.lower()


__all__ = ["Parser"]
