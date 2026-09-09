"""Rich 渲染与 pyfiglet 字体；没有手工拼接的字母图案。"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
import re

from pyfiglet import Figlet
from rich import box
from rich.console import Console, Group
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from contracts.errors import SqlError
from contracts.result import QueryResult


ACCENT = "#64d9c3"
MUTED = "#9299a6"


def safe_text(value: object) -> str:
    """将记录里的换行、ESC 等控制字符显示为字面量，保持表格/终端完整。"""
    return re.sub(r"[\x00-\x1f\x7f-\x9f]", lambda m: repr(m[0])[1:-1], str(value))


@lru_cache(maxsize=8)
def title_art(width: int) -> str:
    # 字形完全来自 pyfiglet 的字体资源，宽度不够时换用紧凑字体。
    for font in ("ansi_shadow", "small"):
        art = Figlet(font=font, width=1000).renderText("HELLO-SQL").rstrip()
        if max(map(len, art.splitlines()), default=0) <= width:
            return art
    return "HELLO-SQL"


def gradient_title(width: int) -> Text:
    art = title_art(width)
    longest = max(map(len, art.splitlines()), default=1)
    stops = ((59, 149, 255), (175, 117, 244), (255, 110, 161))
    result = Text(no_wrap=True)
    for row, line in enumerate(art.splitlines()):
        if row:
            result.append("\n")
        for column, char in enumerate(line):
            position = column / max(longest - 1, 1) * 2
            segment = min(int(position), 1)
            mix = position - segment
            rgb = tuple(round(a + (b - a) * mix) for a, b in zip(stops[segment], stops[segment + 1]))
            result.append(char, style="#{:02x}{:02x}{:02x}".format(*rgb))
    return result


class TerminalRenderer:
    def __init__(self, console: Console | None = None) -> None:
        self.console = console if console is not None else Console(highlight=False)

    def welcome(self, database: str, data_dir: Path | None) -> None:
        width = self.console.width
        session = Text(
            f">_ hello-sql\n\n当前数据库  {safe_text(database)}\n运行模式    本地\n"
            f"数据目录\n{safe_text(data_dir) if data_dir else '由调用方管理'}",
            overflow="fold",
        )
        session.stylize("bold #b09cf7", 0, 12)
        info = Panel(session, border_style="#9b8cdd", padding=(0, 1))
        self.console.print()
        if width >= 108:
            header = Table.grid(padding=(0, 3), expand=True)
            header.add_column(width=width - 39)
            header.add_column(width=36)
            header.add_row(
                Group(gradient_title(width - 39), Text("一个轻量级 SQL 数据库", style=MUTED)),
                info,
            )
            self.console.print(header)
        else:
            self.console.print(gradient_title(width))
            self.console.print(Text("一个轻量级 SQL 数据库", style=MUTED))
            self.console.print(info)
        self.console.print()
        self.console.print(Text("快速开始  /help 帮助 · /tables 查看表 · /databases 查看数据库", style=MUTED))
        self.console.rule(style="#424955")

    def result(self, result: QueryResult, elapsed: float | None = None) -> None:
        suffix = f" · {elapsed:.3f}s" if elapsed is not None else ""
        if result.columns is not None and result.rows is not None:
            table = Table(box=box.SQUARE, border_style=ACCENT, header_style="bold", highlight=False)
            for index, column in enumerate(result.columns):
                numeric = bool(result.rows) and all(
                    isinstance(row[index], (int, float)) for row in result.rows
                )
                table.add_column(Text(safe_text(column)), justify="right" if numeric else "left", overflow="fold")
            for row in result.rows:
                table.add_row(*(Text(safe_text(value)) for value in row))
            self.console.print(table)
            count = len(result.rows)
            self.console.print(Text(f"{count} row{'s' if count != 1 else ''}{suffix}", style=MUTED))
        else:
            count = result.affected_rows or 0
            self.console.print(Text(f"✓ {count} row{'s' if count != 1 else ''} affected{suffix}", style="#95df88"))
        self.console.print()

    def error(self, error: SqlError) -> None:
        self.console.print(Text(f"[{error.code}] {safe_text(error.message)}", style="#ff777f"))
        self.console.print()

    def help(self) -> None:
        table = Table(box=box.SIMPLE, border_style=ACCENT, title="hello-sql 帮助", highlight=False)
        table.add_column("命令 / 快捷键", style=ACCENT)
        table.add_column("说明")
        for command, description in HELP_ITEMS:
            table.add_row(command, description)
        self.console.print(table)
        self.console.print(Text(
            "SQL：CREATE/DROP DATABASE、USE、CREATE/DROP TABLE、INSERT、SELECT、UPDATE、DELETE\n"
            "类型：INT / TEXT / REAL；WHERE 支持比较和 AND。一次输入一条 SQL，末尾分号可省略。",
            style=MUTED,
        ))


HELP_ITEMS = (
    ("/help", "查看帮助"),
    ("/databases", "查看数据库"),
    ("/tables", "查看当前库的表"),
    ("/describe 表名", "查看表结构"),
    ("/clear", "清理屏幕"),
    ("/quit、quit、exit", "退出程序"),
    ("Tab / ↑↓", "补全 / 浏览输入历史"),
    ("Ctrl+C / Ctrl+D", "清空尚未提交的输入 / 空输入时退出"),
)
