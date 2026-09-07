"""模块 C：运行层 / 软件操作（语义检查 + 执行 + REPL）。对外入口就是本文件：

    from runner import Runner
    runner = Runner(server=..., parse=...)   # 由 main.py 装配时注入
    result = runner.execute("SELECT * FROM users;")

Runner 类由 C 定义在本文件里，作为 C 方法契约的唯一代码真相：

    def __init__(self, server, parse):
        # server 是 B 的 DatabaseServer；进入时自动 connect("main")
        # 作为当前库；parse 是 (str) -> Statement 的函数
    def execute(self, sql: str) -> QueryResult:
        # CREATE/DROP/USE DATABASE 会改“当前库”会话状态；
        # 表语句只作用于当前库；不能 DROP 当前库或默认库 main
    def repl(self) -> None:
        # 一行一条 SQL；提供 .databases / .tables / .quit 等命令

跨模块的结果格式见 contracts.result。
本目录由模块 C 的开发独占；禁止 import compiler / storage
（A 的 parse 与 B 的 DatabaseServer 都由 main.py 注入进来）。
"""
