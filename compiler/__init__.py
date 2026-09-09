"""模块 A：SQL 编译层的唯一公开入口。

外部模块只能通过以下方式使用 A：

    from compiler import parse
    statement = parse("SELECT * FROM users;")

``parse`` 依次协调 lexer 和 parser：先将 SQL 字符串切分为带位置的 Token，
再将 Token 按 V1.1 文法构建为 contracts.ast.Statement。A 的职责在 AST
产生时结束；它绝不查询数据库、访问磁盘、调用 Storage，也不执行 WHERE。

本包只导出 parse，避免 runner 等调用方依赖 Lexer、Parser 的内部实现细节。
支持 CREATE/DROP DATABASE、USE、CREATE/DROP TABLE、INSERT、SELECT、
UPDATE、DELETE。错误统一抛出带行列号的 contracts.errors.ParseError。
"""

from __future__ import annotations

from compiler.lexer import tokenize
from compiler.parser import Parser
from contracts.ast import Statement


# 此函数是模块 A 唯一面向 runner、main.py 和测试代码的稳定公开 API。
def parse(sql: str) -> Statement:
    """把一条完整 SQL 文本编译为契约规定的 AST Statement。

    编译过程固定分为两个内部阶段：

    1. ``tokenize(sql)``：词法分析，识别关键字、标识符、字面量、运算符，
       并为每个 Token 记录从 1 开始的行列位置；
    2. ``Parser(tokens).parse()``：语法分析，验证 V1.1 文法、将标识符统一
       转小写、将字面量转换为 Python 原生值，并构建对应的 AST 数据类。

    本函数不进行任何语义或运行时检查：数据库/表/列是否存在、INSERT 的值
    数量和类型是否匹配、WHERE 条件是否命中行，均是 runner 与 storage 的职责。
    输入只能包含一条 SQL，结尾允许至多一个分号。

    Args:
        sql: 待编译的一条 SQL 文本。关键字大小写不敏感；表名、库名和列名
            会在 AST 中统一保存为小写。

    Returns:
        contracts.ast.Statement 联合类型中的一个具体 AST 节点。

    Raises:
        ParseError: SQL 含非法字符、未闭合字符串、文法错误、多条语句，或
            不属于 V1.1 支持子集时抛出。异常携带 E_SYNTAX 及精确行列号。
    """
    tokens = tokenize(sql)
    return Parser(tokens).parse()


__all__ = ["parse"]
