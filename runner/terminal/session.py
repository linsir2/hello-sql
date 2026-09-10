"""交互会话：使用公共接口，不依赖 compiler/storage 的具体实现。"""

from __future__ import annotations

from pathlib import Path
import shlex
import sys
from typing import TYPE_CHECKING

from prompt_toolkit import PromptSession
from prompt_toolkit.auto_suggest import AutoSuggestFromHistory
from prompt_toolkit.completion import Completer, Completion
from prompt_toolkit.history import FileHistory, InMemoryHistory
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.lexers import PygmentsLexer
from prompt_toolkit.styles import Style
from pygments.lexers.sql import SqlLexer

from contracts.errors import E_BAD_ARG, SqlError
from contracts.result import QueryResult, ScriptResult
from runner.terminal.render import HELP_ITEMS, TerminalRenderer, safe_text

if TYPE_CHECKING:
    from runner.runner import Runner


KEYWORDS = (
    "CREATE", "DATABASE", "DROP", "USE", "TABLE", "INSERT", "INTO", "VALUES",
    "SELECT", "FROM", "WHERE", "UPDATE", "SET", "DELETE", "AND", "OR", "NOT",
    "INNER", "JOIN", "ON", "AS", "INT", "TEXT", "REAL", "BOOLEAN", "TRUE", "FALSE",
)
COMMANDS = (
    "/help", "/databases", "/tables", "/describe", "/file",
    "/stop-on-error", "/clear", "/quit",
)
STYLE = Style.from_dict({
    "prompt": "bold #64d9c3",
    "rule": "#424955",
    "bottom-toolbar": "bg:default #9299a6",
    "completion-menu.completion.current": "bg:#383653 #ffffff",
    "auto-suggestion": "#697181",
    "pygments.keyword": "bold #8bb5fa",
    "pygments.literal.string": "#e5c07b",
    "pygments.literal.number": "#e5c07b",
})


class SqlCompleter(Completer):
    def __init__(self, runner: Runner) -> None:
        self.runner = runner

    def get_completions(self, document, complete_event):
        before = document.text_before_cursor
        word = document.get_word_before_cursor(WORD=True)
        prefix = before[:len(before) - len(word)].strip().upper()
        candidates = list(COMMANDS if before.lstrip().startswith("/") else KEYWORDS)
        try:
            if prefix.endswith("USE") or prefix.endswith("DATABASE"):
                candidates = self.runner.list_databases()
            elif prefix.endswith(("FROM", "INTO", "UPDATE", "TABLE", "/DESCRIBE")):
                candidates = self.runner.list_tables()
            elif not before.lstrip().startswith("/"):
                candidates += self.runner.list_tables()
        except SqlError:
            # 补全失败不妨碍继续编辑和执行 SQL。
            pass
        for candidate in sorted(set(candidates)):
            if candidate.lower().startswith(word.lower()):
                yield Completion(candidate, start_position=-len(word))


class TerminalSession:
    def __init__(
        self, runner: Runner, *, data_dir: Path | None = None,
        plain: bool = False, history: bool = True, stop_on_error: bool = True,
    ) -> None:
        self.runner = runner
        self.data_dir = data_dir
        self.interactive = not plain and sys.stdin.isatty() and sys.stdout.isatty()
        self.history_enabled = history
        self.stop_on_error = stop_on_error
        self.renderer = TerminalRenderer()

    def _make_prompt(self) -> PromptSession:
        bindings = KeyBindings()

        @bindings.add("enter")
        def _execute_buffer(event) -> None:
            event.current_buffer.validate_and_handle()

        @bindings.add("escape", "enter")
        def _insert_newline(event) -> None:
            event.current_buffer.insert_text("\n")

        history = InMemoryHistory()
        if self.history_enabled and self.data_dir is not None:
            try:
                history_path = self.data_dir / ".hello_sql_history"
                # 在会话开始时发现无法写入的情况，避免每次提交输入时失败。
                with history_path.open("a", encoding="utf-8"):
                    pass
                history = FileHistory(str(history_path))
            except OSError:
                self.renderer.console.print("历史文件不可写，本次仅保留内存历史。", style="yellow")
        return PromptSession(
            history=history,
            auto_suggest=AutoSuggestFromHistory(),
            completer=SqlCompleter(self.runner),
            complete_while_typing=False,
            multiline=True,
            key_bindings=bindings,
            lexer=PygmentsLexer(SqlLexer),
            style=STYLE,
            bottom_toolbar=lambda: [
                ("class:rule", "─" * self.renderer.console.width + "\n"),
                ("", "Enter 执行 · Alt+Enter 换行 · Tab 补全 · ↑↓ 历史 · Ctrl+D 退出"),
            ],
            reserve_space_for_menu=3,
        )

    def _result(self, result: QueryResult, elapsed: float | None = None) -> None:
        if self.interactive:
            self.renderer.result(result, elapsed)
        else:
            self.runner._print_result(result)

    def _script_result(self, result: ScriptResult) -> bool:
        """渲染脚本结果，返回其中是否至少有一条失败。"""
        if self.interactive:
            self.renderer.script_result(result)
        else:
            for statement in result.statements:
                if statement.result is not None:
                    self.runner._print_result(statement.result)
                else:
                    assert statement.error is not None
                    print(f"[{statement.error.code}] {safe_text(statement.error.message)}")
        return any(statement.error is not None for statement in result.statements)

    def _command(self, sql: str) -> tuple[bool, bool]:
        """返回（是否为界面命令，命令执行是否失败）。"""
        if not sql.startswith("/"):
            return False, False
        try:
            parts = shlex.split(sql)
        except ValueError as error:
            raise SqlError(E_BAD_ARG, f"命令参数无效：{error}") from None
        if not parts:
            return False, False
        command = parts[0].lower()
        expected = 2 if command in ("/describe", "/file", "/stop-on-error") else 1
        if command not in COMMANDS or len(parts) != expected:
            raise SqlError(E_BAD_ARG, "未知命令或参数不正确，请输入 /help")
        if command == "/help":
            if self.interactive:
                self.renderer.help()
            else:
                for key, description in HELP_ITEMS:
                    print(f"{key}\t{description}")
        elif command == "/clear":
            if self.interactive:
                self.renderer.console.clear()
        elif command == "/databases":
            self._result(QueryResult(columns=("database",), rows=tuple((name,) for name in sorted(self.runner.list_databases()))))
        elif command == "/tables":
            self._result(QueryResult(columns=("table",), rows=tuple((name,) for name in sorted(self.runner.list_tables()))))
        elif command == "/describe":
            info = self.runner.describe_table(parts[1].lower())
            self._result(QueryResult(columns=("column", "type"), rows=tuple((col.name, col.type.value) for col in info.columns)))
        elif command == "/file":
            result = self.runner.execute_file(
                Path(parts[1]).expanduser(),
                stop_on_error=self.stop_on_error,
            )
            return True, self._script_result(result)
        elif command == "/stop-on-error":
            value = parts[1].lower()
            if value not in ("on", "off"):
                raise SqlError(E_BAD_ARG, "/stop-on-error 只接受 on 或 off")
            self.stop_on_error = value == "on"
            message = f"stop-on-error = {value}"
            if self.interactive:
                self.renderer.console.print(message, style="#9299a6")
            else:
                print(message)
        return True, False

    def _execute_input(self, sql: str) -> bool:
        """执行一个完整输入缓冲区，返回是否出现 SQL 错误。"""
        recognized, failed = self._command(sql)
        if recognized:
            return failed
        result = self.runner.execute_script(sql, stop_on_error=self.stop_on_error)
        return self._script_result(result)

    def run(self) -> int:
        prompt = None
        if self.interactive:
            self.renderer.welcome(self.runner.current_database, self.data_dir)
            prompt = self._make_prompt()
        failed = False
        while True:
            try:
                if prompt is not None:
                    sql = prompt.prompt([("class:prompt", f"{self.runner.current_database} ❯ ")])
                else:
                    sql = input(f"{self.runner.current_database}> " if sys.stdin.isatty() else "")
            except EOFError:
                return int(failed)
            except KeyboardInterrupt:
                # 这里只拦截编辑阶段，绝不声称能回滚已开始执行的 DML。
                continue
            sql = sql.strip()
            if not sql:
                continue
            if sql.lower().rstrip(";") in ("/quit", "quit", "exit", "\\q"):
                return int(failed)
            try:
                input_failed = self._execute_input(sql)
                failed = failed or (input_failed and not sys.stdin.isatty())
            except SqlError as error:
                # 交互纠错后仍可正常退出；管道输入出现错误则返回非零退出码。
                failed = failed or not sys.stdin.isatty()
                if self.interactive:
                    self.renderer.error(error)
                else:
                    print(f"[{error.code}] {safe_text(error.message)}")
