"""错误优先级矩阵：多个错误同时满足时，先抛哪个码（拍板顺序固定成契约行为）。

顺序：名称非法(E_BAD_ARG) → 库/表不存在 → EXISTS/DUP → 值个数 → 值类型
→ row_id 类型/不存在 → E_STORAGE。
"""

from __future__ import annotations

import pytest

from contracts.ast import ColumnDef, SqlType
from contracts.errors import (
    E_BAD_ARG,
    E_DATABASE_EXISTS,
    E_DATABASE_IN_USE,
    E_DUP_COLUMN,
    E_ROW_NOT_FOUND,
    E_TABLE_EXISTS,
    E_TABLE_NOT_FOUND,
    E_TYPE_MISMATCH,
    E_VALUE_COUNT,
    SqlError,
)
from storage import DatabaseServer


def _columns() -> tuple[ColumnDef, ColumnDef]:
    return (ColumnDef("id", SqlType.INT), ColumnDef("tag", SqlType.TEXT))


@pytest.fixture
def data_dir(tmp_path) -> str:
    return str(tmp_path / "data")


def _storage(data_dir: str):
    return DatabaseServer(data_dir).connect("main")


def _expect(call, code: str) -> None:
    with pytest.raises(SqlError) as exc:
        call()
    assert exc.value.code == code


def test_invalid_name_beats_missing_table(data_dir):
    """非法表名 + 表不存在 → E_BAD_ARG 优先于 E_TABLE_NOT_FOUND。"""
    storage = _storage(data_dir)
    _expect(lambda: storage.describe("Bad"), E_BAD_ARG)
    _expect(lambda: storage.drop_table("Bad"), E_BAD_ARG)
    _expect(lambda: storage.scan("Bad"), E_BAD_ARG)
    _expect(lambda: storage.insert("Bad", (1, "a")), E_BAD_ARG)
    _expect(lambda: storage.update_row("Bad", 1, (1, "a")), E_BAD_ARG)
    _expect(lambda: storage.delete_row("Bad", 1), E_BAD_ARG)


def test_missing_table_beats_value_errors(data_dir):
    """表不存在 + 值个数/类型错 → E_TABLE_NOT_FOUND 优先。"""
    storage = _storage(data_dir)
    _expect(lambda: storage.insert("ghost", (1,)), E_TABLE_NOT_FOUND)
    _expect(
        lambda: storage.insert("ghost", (True, "x")), E_TABLE_NOT_FOUND
    )
    _expect(
        lambda: storage.update_row("ghost", 1, (1, "x")), E_TABLE_NOT_FOUND
    )
    _expect(
        lambda: storage.delete_row("ghost", 1), E_TABLE_NOT_FOUND
    )


def test_table_exists_beats_dup_columns(data_dir):
    """同名表已存在 + 空列/重复列 → E_TABLE_EXISTS 优先于 E_DUP_COLUMN。"""
    storage = _storage(data_dir)
    storage.create_table("users", _columns())

    _expect(lambda: storage.create_table("users", ()), E_TABLE_EXISTS)
    _expect(
        lambda: storage.create_table(
            "users", (ColumnDef("id", SqlType.INT),) * 2
        ),
        E_TABLE_EXISTS,
    )


def test_value_count_beats_type_mismatch(data_dir):
    """个数错 + 类型错同时存在 → E_VALUE_COUNT 先（个数检查在前）。"""
    storage = _storage(data_dir)
    storage.create_table("users", _columns())

    _expect(
        lambda: storage.insert("users", (True, "x", "extra")), E_VALUE_COUNT
    )


def test_type_mismatch_reports_first_bad_column(data_dir):
    """类型检查按列序：第一列错先报，即使后列也错。"""
    storage = _storage(data_dir)
    storage.create_table("users", _columns())

    _expect(
        lambda: storage.insert("users", ("not-int", True)), E_TYPE_MISMATCH
    )


def test_value_errors_beat_row_not_found(data_dir):
    """update 值错 + 行不存在 → 值错误先（值检查先于 rid 定位）。"""
    storage = _storage(data_dir)
    storage.create_table("users", _columns())

    _expect(
        lambda: storage.update_row("users", 99, (1,)), E_VALUE_COUNT
    )
    _expect(
        lambda: storage.update_row("users", 99, (True, "x")), E_TYPE_MISMATCH
    )
    _expect(
        lambda: storage.update_row("users", 1, (True, "x")), E_TYPE_MISMATCH
    )


def test_bool_row_id_is_row_not_found_not_type_error(data_dir):
    """bool 当 row_id 交给 update/delete → E_ROW_NOT_FOUND（不是类型错）。"""
    storage = _storage(data_dir)
    storage.create_table("users", _columns())
    storage.insert("users", (1, "a"))

    _expect(
        lambda: storage.update_row("users", True, (2, "b")), E_ROW_NOT_FOUND
    )
    _expect(lambda: storage.delete_row("users", True), E_ROW_NOT_FOUND)


def test_database_precedence_rules(data_dir):
    """库层：main 不可删优先于其它；已存在优先于目录杂项判断。"""
    server = DatabaseServer(data_dir)

    _expect(lambda: server.drop_database("main"), E_DATABASE_IN_USE)
    _expect(
        lambda: server.create_database("main"), E_DATABASE_EXISTS
    )


def test_scan_validation_is_eager_but_row_errors_lazy(data_dir):
    """scan 缺表在调用时抛；行解码损坏错误在迭代时抛（由 damage 测试覆盖）。"""
    storage = _storage(data_dir)
    _expect(lambda: storage.scan("ghost"), E_TABLE_NOT_FOUND)
