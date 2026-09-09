"""用实际 TUI 渲染器导出 SVG 示例：python -m scripts.preview_terminal 输出.svg。"""

from __future__ import annotations

import argparse
import io
from pathlib import Path

from rich.console import Console
from rich.terminal_theme import TerminalTheme
from rich.text import Text

from contracts.errors import E_COLUMN_NOT_FOUND, SqlError
from contracts.result import QueryResult
from runner.terminal.render import TerminalRenderer


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path)
    parser.add_argument("--width", type=int, default=120)
    args = parser.parse_args()
    console = Console(
        file=io.StringIO(), record=True, width=args.width,
        force_terminal=True, color_system="truecolor", no_color=False,
    )
    renderer = TerminalRenderer(console)
    console.print(Text("% hello-sql"))
    renderer.welcome("main", Path("/Users/demo/.hello-sql/data"))
    console.print(Text("main ❯ SELECT * FROM users;", style="#8bb5fa"))
    renderer.result(QueryResult(columns=("id", "name", "age"), rows=((1, "alice", 18.0), (2, "bob", 20.0))), 0.003)
    console.print(Text("main ❯ UPDATE users SET age = 19 WHERE id = 1;", style="#8bb5fa"))
    renderer.result(QueryResult(affected_rows=1), 0.002)
    console.print(Text("main ❯ SELECT nope FROM users;", style="#8bb5fa"))
    renderer.error(SqlError(E_COLUMN_NOT_FOUND, "column not found: nope"))
    console.rule(style="#424955")
    console.print(Text("main ❯ ▌", style="#64d9c3"))
    console.rule(style="#424955")
    console.print(Text("Enter 执行 · Tab 补全 · ↑↓ 历史 · Ctrl+D 退出", style="#9299a6"))
    theme = TerminalTheme(
        (22, 27, 34), (224, 228, 235),
        [(22, 27, 34), (255, 119, 127), (149, 223, 136), (229, 192, 123),
         (139, 181, 250), (175, 117, 244), (100, 217, 195), (224, 228, 235)],
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    console.save_svg(str(args.output), title="hello-sql · 实际渲染示例", theme=theme)
    print(args.output.resolve())


if __name__ == "__main__":
    main()
