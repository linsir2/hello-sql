"""从根目录装配真实模块并执行完整 Golden SQL 序列。"""

from compiler import parse
from contracts.errors import SqlError
from runner import Runner
from storage import DatabaseServer
from tests.golden_sql import GOLDEN_SQL


def test_runner_executes_golden_sql_in_one_session(tmp_path) -> None:
    runner = Runner(DatabaseServer(tmp_path), parse)

    for case in GOLDEN_SQL:
        try:
            result = runner.execute(case["sql"])
        except SqlError as error:
            assert case["expect"] == "error", case["id"]
            assert error.code == case["code"], case["id"]
            continue

        assert case["expect"] == "ok", case["id"]
        if "affected" in case:
            assert result.affected_rows == case["affected"], case["id"]
        if "header" in case:
            assert result.columns == case["header"], case["id"]
            assert result.rows is not None, case["id"]
            assert len(result.rows) == case["row_count"], case["id"]
            assert set(result.rows) == set(case["rows_any_order"]), case["id"]

    assert runner.current_database == "main"
