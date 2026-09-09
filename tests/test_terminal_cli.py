"""CLI 真实进程持久化、TUI 渲染与交互边界回归测试。"""

from __future__ import annotations

import io
import os
from pathlib import Path
import subprocess
import sys
from unittest.mock import patch

import pytest
from prompt_toolkit.completion import CompleteEvent
from prompt_toolkit.document import Document
from rich.cells import cell_len
from rich.console import Console

from compiler import parse
from contracts.result import QueryResult
from main import main, resolve_data_dir
from runner import Runner
from runner.terminal.render import TerminalRenderer, gradient_title, title_art
from runner.terminal.session import SqlCompleter, TerminalSession
from storage import DatabaseServer


ROOT = Path(__file__).resolve().parents[1]


def run_cli(tmp_path, *args, sql=None, cwd=None):
    return subprocess.run(
        [sys.executable, str(ROOT / "main.py"), "--data-dir", str(tmp_path / "db"), *args],
        input=sql, text=True, capture_output=True, cwd=cwd or tmp_path, timeout=15,
    )


def test_cli_persistence_across_processes_and_working_directories(tmp_path):
    written = run_cli(tmp_path, sql="CREATE TABLE users (id INT, name TEXT);\nINSERT INTO users VALUES (1, '张三');\n/quit\n")
    assert written.returncode == 0, written.stderr
    other = tmp_path / "other"
    other.mkdir()
    read = run_cli(tmp_path, "-e", "SELECT * FROM users;", cwd=other)
    assert read.returncode == 0, read.stderr
    assert read.stdout == "id\tname\n1\t张三\n"
    assert "\x1b" not in read.stdout
    assert not (other / "data").exists()


def test_batch_error_exit_code_and_continue(tmp_path):
    result = run_cli(tmp_path, sql="SELEC * FROM t;\nCREATE TABLE t (id INT);\n/tables\n")
    assert result.returncode == 1
    assert "[E_SYNTAX]" in result.stdout
    assert "table\nt\n" in result.stdout
    failed = run_cli(tmp_path, "-e", "SELECT * FROM missing;")
    assert failed.returncode == 1
    assert "[E_TABLE_NOT_FOUND]" in failed.stderr
    assert "Traceback" not in failed.stderr


def test_metadata_commands_follow_use(tmp_path):
    result = run_cli(tmp_path, sql=(
        "CREATE DATABASE shop;\nUSE shop;\nCREATE TABLE goods (id INT, name TEXT);\n"
        "/tables\n/describe goods\nUSE main;\n/tables\n/databases\nexit;\n"
    ))
    assert result.returncode == 0, result.stderr
    assert "table\ngoods\n" in result.stdout
    assert "column\ttype\nid\tINT\nname\tTEXT\n" in result.stdout
    assert "database\nmain\nshop\n" in result.stdout
    assert result.stdout.count("goods") == 1


def test_path_priority_is_independent_of_cwd(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path / "user")
    monkeypatch.delenv("HELLO_SQL_DATA_DIR", raising=False)
    expected = tmp_path / "user" / ".hello-sql" / "data"
    assert resolve_data_dir(None) == expected
    monkeypatch.chdir(tmp_path)
    assert resolve_data_dir(None) == expected
    monkeypatch.setenv("HELLO_SQL_DATA_DIR", str(tmp_path / "configured"))
    assert resolve_data_dir(None) == tmp_path / "configured"
    assert resolve_data_dir(tmp_path / "explicit") == tmp_path / "explicit"


def test_help_and_version_do_not_create_data(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("HELLO_SQL_DATA_DIR", str(tmp_path / "never-created"))
    for argument in ("--help", "--version"):
        with pytest.raises(SystemExit) as caught:
            main([argument])
        assert caught.value.code == 0
    assert "hello-sql" in capsys.readouterr().out
    assert not (tmp_path / "never-created").exists()


def test_completion_tracks_current_database(tmp_path):
    runner = Runner(DatabaseServer(tmp_path), parse)
    runner.execute("CREATE TABLE local_table (id INT)")
    runner.execute("CREATE DATABASE shop")
    completer = SqlCompleter(runner)

    def choices(text):
        return [c.text for c in completer.get_completions(Document(text), CompleteEvent())]

    assert choices("SELECT * FROM lo") == ["local_table"]
    assert choices("USE sh") == ["shop"]
    runner.execute("USE shop")
    assert choices("SELECT * FROM lo") == []
    assert choices("/ta") == ["/tables"]


@pytest.mark.parametrize("width", [24, 40, 80, 108, 120, 160])
def test_welcome_fits_terminal_width(width):
    stream = io.StringIO()
    renderer = TerminalRenderer(Console(file=stream, width=width, color_system=None))
    renderer.welcome("main", Path("/tmp/测试目录/hello-sql/data"))
    assert all(cell_len(line) <= width for line in stream.getvalue().splitlines())
    assert max(map(cell_len, title_art(width).splitlines())) <= width


def test_tables_preserve_unicode_markup_and_empty_headers():
    stream = io.StringIO()
    renderer = TerminalRenderer(Console(file=stream, width=100, color_system=None))
    renderer.result(QueryResult(columns=("name",), rows=(("张三 [red] ",), ("\x1b[2J\n",))))
    renderer.result(QueryResult(columns=("empty_column",), rows=()))
    output = stream.getvalue()
    assert "张三 [red]" in output
    assert "\\x1b[2J\\n" in output
    assert "\x1b" not in output
    assert "empty_column" in output and "0 rows" in output


def test_art_is_rendered_in_true_color():
    stream = io.StringIO()
    console = Console(file=stream, width=120, force_terminal=True, color_system="truecolor", no_color=False)
    console.print(gradient_title(100))
    assert "\x1b[38;2;" in stream.getvalue()
    assert "#3b95ff" in str(gradient_title(100).spans[0].style)


def test_interactive_ctrl_c_error_recovery_and_updated_prompt(tmp_path):
    runner = Runner(DatabaseServer(tmp_path), parse)
    session = TerminalSession(runner, data_dir=tmp_path, history=False)
    session.interactive = True
    stream = io.StringIO()
    session.renderer = TerminalRenderer(Console(file=stream, width=120, color_system=None))
    answers = iter([KeyboardInterrupt(), "SELEC", "CREATE DATABASE shop", "USE shop", EOFError()])
    prompts = []

    class FakePrompt:
        def prompt(self, message):
            prompts.append(message[0][1])
            value = next(answers)
            if isinstance(value, BaseException):
                raise value
            return value

    with patch.object(session, "_make_prompt", return_value=FakePrompt()), patch("sys.stdin.isatty", return_value=True):
        assert session.run() == 0
    assert prompts[-1] == "shop ❯ "
    assert "[E_SYNTAX]" in stream.getvalue()
    assert not (tmp_path / ".hello_sql_history").exists()


def test_installed_command_can_run_outside_repository(tmp_path):
    # 此项目按 README 安装后，入口必须不依赖 cwd/PYTHONPATH。
    command = Path(sys.executable).parent / ("hello-sql.exe" if os.name == "nt" else "hello-sql")
    if not command.exists():
        pytest.skip("需要先安装应用以测试 console_scripts")
    result = subprocess.run([str(command), "--version"], cwd=tmp_path, text=True, capture_output=True, timeout=15)
    assert result.returncode == 0, result.stderr
    assert result.stdout.startswith("hello-sql ")
