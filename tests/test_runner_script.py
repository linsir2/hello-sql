"""Runner 多语句与 SQL 文件执行的真实链路测试。"""

from __future__ import annotations

from pathlib import Path

import pytest

from compiler import parse, parse_script
from contracts.errors import (
    E_INPUT_FILE,
    E_SYNTAX,
    E_TABLE_NOT_FOUND,
    E_TYPE_MISMATCH,
    SqlError,
)
from runner import Runner
from storage import DatabaseServer


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SHOW_DIR = PROJECT_ROOT / "docs" / "zjt-docs" / "show"


def _runner(tmp_path: Path) -> Runner:
    return Runner(DatabaseServer(tmp_path / "data"), parse, parse_script=parse_script)


def test_execute_script_runs_multiline_statements_in_source_order(tmp_path) -> None:
    runner = _runner(tmp_path)
    source = """
        CREATE TABLE notes (id INT, body TEXT, visible BOOLEAN);
        INSERT INTO notes VALUES (1, 'a;b', TRUE);
        INSERT INTO notes VALUES (2, 'hidden', FALSE);
        SELECT id, body
        FROM notes
        WHERE visible;
    """

    result = runner.execute_script(source)

    assert result.stopped_early is False
    assert len(result.statements) == 4
    assert all(statement.error is None for statement in result.statements)
    assert all(statement.elapsed_ms >= 0 for statement in result.statements)
    assert result.statements[0].span.start_line == 2
    assert result.statements[-1].span.start_line == 5
    query = result.statements[-1].result
    assert query is not None
    assert query.columns == ("id", "body")
    assert query.rows == ((1, "a;b"),)


def test_execute_script_stop_on_error_controls_later_statements(tmp_path) -> None:
    stopped_runner = _runner(tmp_path / "stopped")
    source = """
        CREATE TABLE values_table (id INT);
        INSERT INTO values_table VALUES ('bad');
        INSERT INTO values_table VALUES (7);
    """

    stopped = stopped_runner.execute_script(source, stop_on_error=True)
    assert stopped.stopped_early is True
    assert len(stopped.statements) == 2
    assert stopped.statements[-1].error is not None
    assert stopped.statements[-1].error.code == E_TYPE_MISMATCH
    assert stopped_runner.execute("SELECT * FROM values_table;").rows == ()

    continued_runner = _runner(tmp_path / "continued")
    continued = continued_runner.execute_script(source, stop_on_error=False)
    assert continued.stopped_early is False
    assert len(continued.statements) == 3
    assert continued.statements[1].error is not None
    assert continued.statements[1].error.code == E_TYPE_MISMATCH
    assert continued.statements[2].result is not None
    assert continued_runner.execute("SELECT * FROM values_table;").rows == ((7,),)


def test_execute_script_accepts_empty_input(tmp_path) -> None:
    result = _runner(tmp_path).execute_script(" \n\t")
    assert result.statements == ()
    assert result.stopped_early is False


def test_execute_script_parse_error_prevents_partial_execution(tmp_path) -> None:
    runner = _runner(tmp_path)

    with pytest.raises(SqlError) as caught:
        runner.execute_script(
            "CREATE TABLE should_not_exist (id INT); SELECT FROM broken;"
        )

    assert caught.value.code == E_SYNTAX
    with pytest.raises(SqlError) as missing:
        runner.execute("SELECT * FROM should_not_exist;")
    assert missing.value.code == E_TABLE_NOT_FOUND


def test_execute_file_reads_utf8_multiline_script(tmp_path) -> None:
    runner = _runner(tmp_path)
    sql_file = tmp_path / "演示脚本.sql"
    sql_file.write_text(
        "CREATE TABLE messages (id INT, body TEXT);\n"
        "INSERT INTO messages VALUES (1, '你好');\n"
        "SELECT *\nFROM messages;\n",
        encoding="utf-8",
    )

    result = runner.execute_file(sql_file)

    assert len(result.statements) == 3
    query = result.statements[-1].result
    assert query is not None
    assert query.rows == ((1, "你好"),)


@pytest.mark.parametrize("kind", ["missing", "invalid_utf8"])
def test_execute_file_wraps_input_errors(tmp_path, kind: str) -> None:
    runner = _runner(tmp_path)
    path = tmp_path / "input.sql"
    if kind == "invalid_utf8":
        path.write_bytes(b"\xff\xfe\x00")

    with pytest.raises(SqlError) as caught:
        runner.execute_file(path)

    assert caught.value.code == E_INPUT_FILE


def test_checked_in_show_scripts_are_directly_executable(tmp_path) -> None:
    demo = _runner(tmp_path / "demo").execute_file(
        SHOW_DIR / "v2_runner_demo.sql"
    )
    assert demo.stopped_early is False
    assert all(statement.error is None for statement in demo.statements)
    final_query = demo.statements[-1].result
    assert final_query is not None
    assert set(final_query.rows or ()) == {
        ("alice", 10, "book", 35.5),
        ("bob", 12, "keyboard", 199.0),
    }

    continued = _runner(tmp_path / "continue").execute_file(
        SHOW_DIR / "v2_runner_continue_on_error.sql",
        stop_on_error=False,
    )
    assert continued.stopped_early is False
    assert len(continued.statements) == 5
    assert continued.statements[2].error is not None
    continued_query = continued.statements[-1].result
    assert continued_query is not None
    assert continued_query.rows == ((1, True), (2, False))
