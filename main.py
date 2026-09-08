"""main.py：装配层（唯一允许同时 import compiler / storage / runner 的文件）。

运行前提：A 已实现 parse、B 已实现 DatabaseServer、C 已实现 Runner。
启动后进入 REPL；数据目录为 data/，默认库 main 会自动创建。

    python3 main.py
"""

from compiler import parse
from runner import Runner
from storage import DatabaseServer


def main() -> None:
    server = DatabaseServer("data")
    runner = Runner(server=server, parse=parse)
    runner.repl()


if __name__ == "__main__":
    main()
