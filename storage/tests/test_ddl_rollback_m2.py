"""M2 DDL 回滚与保留表名测试：表级影响范围 + __sys_ 前缀边界。

故障注入只替换最底层 TableEngine 的写/删动作（I/O 级），
断言仍落在真实系统表、真实用户文件和真实内存 Catalog 上。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from contracts.ast import ColumnDef, SqlType
from contracts.errors import E_BAD_ARG, E_STORAGE, SqlError
from storage import DatabaseServer
from storage.cache import BufferPool
from storage.engine import TableEngine
from storage.syscatalog import open_system_tables


def _columns() -> tuple[ColumnDef, ...]:
    return (
        ColumnDef("id", SqlType.INT),
        ColumnDef("name", SqlType.TEXT),
        ColumnDef("flag", SqlType.BOOLEAN),
    )


@pytest.fixture
def data_dir(tmp_path) -> str:
    return str(tmp_path / "data")


@pytest.fixture
def storage(data_dir):
    return DatabaseServer(data_dir).connect("main")


def _expect_code(call, code: str) -> None:
    with pytest.raises(SqlError) as exc:
        call()
    assert exc.value.code == code


def _fail_nth(
    monkeypatch,
    method_name: str,
    fail_on_call: int,
) -> None:
    """让 TableEngine.<method> 的第 N 次调用抛 E_STORAGE，之后恢复正常。"""
    original = getattr(TableEngine, method_name)
    state = {"calls": 0}

    def wrapper(self, *args, **kwargs):
        state["calls"] += 1
        if state["calls"] == fail_on_call:
            raise SqlError(E_STORAGE, "injected storage failure")
        return original(self, *args, **kwargs)

    monkeypatch.setattr(TableEngine, method_name, wrapper)


def _fail_nth_flush(monkeypatch, fail_on_call: int) -> None:
    """让 BufferPool.flush 的第 N 次调用抛 E_STORAGE，之后恢复正常。"""
    original = BufferPool.flush
    state = {"calls": 0}

    def wrapper(self, file_path=None):
        state["calls"] += 1
        if state["calls"] == fail_on_call:
            raise SqlError(E_STORAGE, "injected flush failure")
        return original(self, file_path)

    monkeypatch.setattr(BufferPool, "flush", wrapper)


def _system_rows(data_dir: str, pool: BufferPool):
    opened = open_system_tables(Path(data_dir) / "main", pool)
    return list(opened.tables.scan()), list(opened.columns.scan())


# ---- CREATE 失败整表回滚 ----


def test_create_table_failure_mid_register_rolls_back_rows_and_file(
    storage, data_dir, monkeypatch
):
    """列行写到一半失败时：系统行清零、用户文件删除、内存无表、可重建。

    断言的改动：失败留下半张系统表或孤儿 .table。
    """
    _fail_nth(monkeypatch, "insert", fail_on_call=3)  # 表行 + 第 1 列行之后

    _expect_code(lambda: storage.create_table("users", _columns()), E_STORAGE)

    pool = BufferPool(capacity=16)
    table_rows, column_rows = _system_rows(data_dir, pool)
    assert table_rows == []
    assert column_rows == []
    assert not (Path(data_dir) / "main" / "users.table").exists()
    assert storage.list_tables() == []

    storage.create_table("users", _columns())  # 注入只失败一次
    assert storage.describe("users").columns == _columns()


# ---- DROP 失败整表恢复 ----


def test_drop_table_failure_mid_unregister_restores_whole_table(
    storage, data_dir, monkeypatch
):
    """系统行写回途中失败时：按快照恢复，内存与磁盘都仍有该表。

    断言的改动：失败后表半消失、describe 找不到、重启损坏。
    """
    storage.create_table("users", _columns())
    storage.insert("users", (1, "alice", True))
    # 删行后 Catalog.flush 先写 sys_tables、再写 sys_columns；
    # 第 2 次 flush 失败 → 表行已落盘删除、列行还没写回。
    _fail_nth_flush(monkeypatch, fail_on_call=2)

    _expect_code(lambda: storage.drop_table("users"), E_STORAGE)

    assert storage.describe("users").columns == _columns()
    assert list(storage.scan("users")) == [(1, (1, "alice", True))]
    reopened = DatabaseServer(data_dir).connect("main")
    assert reopened.describe("users").columns == _columns()


def test_drop_table_unlink_failure_restores_catalog_row(
    storage, data_dir, monkeypatch
):
    """系统行删掉后 unlink 失败：必须恢复目录行，表仍可读，操作报 E_STORAGE。

    断言的改动：unlink 失败后目录无记录但文件仍在（下次启动 E_STORAGE）。
    """
    storage.create_table("users", _columns())
    storage.insert("users", (1, "alice", True))
    target = Path(data_dir) / "main" / "users.table"
    original_unlink = Path.unlink

    def flaky_unlink(self, missing_ok: bool = False):
        if self == target:
            raise OSError("injected unlink failure")
        return original_unlink(self, missing_ok=missing_ok)

    monkeypatch.setattr(Path, "unlink", flaky_unlink)

    _expect_code(lambda: storage.drop_table("users"), E_STORAGE)
    monkeypatch.undo()

    assert target.is_file()
    assert storage.list_tables() == ["users"]
    assert list(storage.scan("users")) == [(1, (1, "alice", True))]
    reopened = DatabaseServer(data_dir).connect("main")
    assert reopened.describe("users").columns == _columns()


def test_drop_table_success_removes_file_and_rows(storage, data_dir):
    """删表成功必须同时清系统行与用户文件，重启后不可见。"""
    storage.create_table("users", _columns())

    storage.drop_table("users")

    assert storage.list_tables() == []
    assert not (Path(data_dir) / "main" / "users.table").exists()
    reopened = DatabaseServer(data_dir).connect("main")
    assert reopened.list_tables() == []


# ---- __sys_ 保留前缀 ----


def test_reserved_prefix_rejected_on_all_named_table_methods(storage, data_dir):
    """7 个带表名的方法对 __sys_ 前缀一律 E_BAD_ARG，且系统表零变化。

    断言的改动：describe/scan/直接 API 能访问系统表，或前缀拦截漏掉某个方法。
    """
    before = (
        (Path(data_dir) / "main" / "sys_tables.db").read_bytes(),
        (Path(data_dir) / "main" / "sys_columns.db").read_bytes(),
    )

    _expect_code(
        lambda: storage.create_table("__sys_x", _columns()), E_BAD_ARG
    )
    _expect_code(lambda: storage.drop_table("__sys_x"), E_BAD_ARG)
    _expect_code(lambda: storage.describe("__sys_x"), E_BAD_ARG)
    _expect_code(lambda: storage.insert("__sys_x", (1, "a", True)), E_BAD_ARG)
    _expect_code(lambda: storage.scan("__sys_x"), E_BAD_ARG)
    _expect_code(
        lambda: storage.update_row("__sys_x", 1, (1, "a", True)), E_BAD_ARG
    )
    _expect_code(lambda: storage.delete_row("__sys_x", 1), E_BAD_ARG)

    after = (
        (Path(data_dir) / "main" / "sys_tables.db").read_bytes(),
        (Path(data_dir) / "main" / "sys_columns.db").read_bytes(),
    )
    assert before == after


def test_reserved_prefix_beats_table_not_found(storage):
    """保留前缀优先于 E_TABLE_NOT_FOUND（与错误优先级矩阵一致）。"""
    _expect_code(lambda: storage.describe("__sys_missing"), E_BAD_ARG)
