"""模块 A：编译层（词法 + LL(1) 语法分析）。

本目录由模块 A 的开发独占。对外唯一入口（由 A 实现）：

    def parse(sql: str) -> Statement:
        """把一条 SQL 文本解析成 AST。
        保证：只做词法 + 语法分析；不查表、不碰磁盘。
        失败：抛 contracts.errors.ParseError（带行列号）。"""

语法与 AST 规范见 docs/contract-v1.md 与 contracts/ast.py。
"""

