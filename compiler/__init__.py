"""模块 A：编译层（词法 + LL(1) 语法分析）。对外入口就是本文件：

    from compiler import parse
    stmt = parse("SELECT * FROM users;")

本文件只导出 parse 一个函数（由 A 实现）：

    def parse(sql: str) -> Statement:
        - 把一条 SQL 文本解析成 AST；
        - 保证只做词法 + 语法分析，不查表、不碰磁盘；
        - 失败时抛 contracts.errors.ParseError（带行列号）。

支持语句：CREATE / DROP / USE DATABASE，以及 CREATE / DROP TABLE、
INSERT、SELECT、UPDATE、DELETE（文法见 docs/contract-v1.md 第 1 节）。

AST 类型定义见 contracts/ast.py（共享数据格式，唯一代码真相）；
文法见 docs/contract-v1.md 第 1 节。
本目录由模块 A 的开发独占；禁止 import storage / runner。
"""
