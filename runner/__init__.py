"""模块 C：运行层 / 软件操作（语义检查 + 执行 + REPL）。对外入口就是本文件：

    from runner import Runner
    runner = Runner(storage=..., parse=...)   # 由 main.py 装配时注入
    result = runner.execute("SELECT * FROM users;")

Runner 类（execute / repl 等方法的签名与实现）由 C 定义在本文件里，
是 C 方法契约的唯一代码真相。跨模块的结果格式见 contracts.result。

本目录由模块 C 的开发独占；禁止 import compiler / storage
（A 的 parse 与 B 的 Storage 都由 main.py 注入进来）。
"""
