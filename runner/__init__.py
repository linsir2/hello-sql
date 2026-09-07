"""模块 C：运行层 / 软件操作（语义检查 + 执行 + REPL）。

本目录由模块 C 的开发独占。对外唯一入口（由 C 实现）：

    class Runner:
        def __init__(self, storage, parse): ...
            # storage 实现 contracts.storage.Storage
            # parse 是 (str) -> Statement 的函数，由 main.py 注入
        def execute(self, sql: str) -> QueryResult: ...
        def repl(self) -> None: ...

数据流与错误归属见 docs/contract-v1.md。
"""

