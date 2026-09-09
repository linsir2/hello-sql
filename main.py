"""hello-sql 命令入口，也是 compiler / storage / runner 的装配层。"""

from __future__ import annotations

import argparse
import os
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
import sys

from compiler import parse
from contracts.errors import SqlError
from runner import Runner
from storage import DatabaseServer


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="hello-sql · 轻量级本地 SQL 数据库")
    parser.add_argument("-D", "--database", default="main", help="初始数据库（默认 main）")
    parser.add_argument(
        "--data-dir", type=Path,
        help="数据目录（默认 HELLO_SQL_DATA_DIR 或 ~/.hello-sql/data）",
    )
    parser.add_argument("--plain", action="store_true", help="使用纯文本交互，关闭艺术字和颜色")
    parser.add_argument("--no-history", action="store_true", help="不读取或保存磁盘输入历史")
    parser.add_argument("-e", "--execute", metavar="SQL", help="执行一条 SQL 后退出")
    try:
        app_version = version("hello-sql")
    except PackageNotFoundError:
        app_version = "1.1.0 (source)"
    parser.add_argument("--version", action="version", version=f"hello-sql {app_version}")
    return parser


def resolve_data_dir(argument: Path | None) -> Path:
    configured = os.environ.get("HELLO_SQL_DATA_DIR")
    path = argument if argument is not None else (
        Path(configured) if configured else Path.home() / ".hello-sql" / "data"
    )
    return path.expanduser().resolve()


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        data_dir = resolve_data_dir(args.data_dir)
        server = DatabaseServer(data_dir)
        runner = Runner(server=server, parse=parse, current_database=args.database.lower())
        if args.execute is not None:
            result = runner.execute(args.execute)
            if sys.stdout.isatty() and not args.plain:
                from runner.terminal.render import TerminalRenderer
                TerminalRenderer().result(result)
            else:
                runner._print_result(result)
            return 0
        return runner.repl(data_dir=data_dir, plain=args.plain, history=not args.no_history)
    except SqlError as error:
        # Literal text: do not interpret error contents as terminal markup.
        from runner.terminal.render import safe_text
        print(f"[{error.code}] {safe_text(error.message)}", file=sys.stderr)
        return 1
    except (OSError, ValueError) as error:
        print(f"hello-sql: {error}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\n操作被中断。", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
