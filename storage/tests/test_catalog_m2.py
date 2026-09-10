"""Catalog 页式核心 M2 测试：两张系统表的加载、注册、注销与结构校验。

M1 已提供 syscatalog 自举；M2 把 Catalog 的权威持久化切到系统表。
命名规则：写清“会让它失败的生产改动”。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from contracts.ast import ColumnDef, SqlType
from contracts.errors import (
    E_DUP_COLUMN,
    E_STORAGE,
    E_TABLE_EXISTS,
    E_TABLE_NOT_FOUND,
    SqlError,
)
from storage.cache import BufferPool
from storage.catalog import Catalog
from storage.pager import create_table_file
from storage.syscatalog import (
    create_empty_system_catalog,
    open_system_tables,
)


def _columns() -> tuple[ColumnDef, ...]:
    return (
        ColumnDef("id", SqlType.INT),
        ColumnDef("name", SqlType.TEXT),
        ColumnDef("flag", SqlType.BOOLEAN),
    )


@pytest.fixture
def db_dir(tmp_path) -> Path:
    path = tmp_path / "main"
    path.mkdir()
    create_empty_system_catalog(path)
    return path


@pytest.fixture
def pool() -> BufferPool:
    return BufferPool(capacity=16)


def _load(db_dir: Path, pool: BufferPool) -> Catalog:
    catalog = Catalog(db_dir, pool)
    catalog.load()
    return catalog


def _expect_code(call, code: str) -> None:
    with pytest.raises(SqlError) as exc:
        call()
    assert exc.value.code == code


def _rows(db_dir: Path, pool: BufferPool):
    opened = open_system_tables(db_dir, pool)
    return list(opened.tables.scan()), list(opened.columns.scan())


def _register(catalog: Catalog, db_dir: Path, name: str, columns) -> None:
    """按真实调用顺序登记：先建用户表文件，再写系统表行。"""
    create_table_file(db_dir / f"{name}.table")
    catalog.register(name, columns)


# ---- 加载与查询 ----


def test_load_empty_system_tables_builds_empty_registry(db_dir, pool):
    """空系统表 load 后应为空目录，查缺失表报 E_TABLE_NOT_FOUND。

    断言的改动：把空表当损坏、或 get 返回 None/泄漏 KeyError。
    """
    catalog = _load(db_dir, pool)

    assert catalog.names() == []
    _expect_code(lambda: catalog.get("nobody"), E_TABLE_NOT_FOUND)


def test_register_writes_documented_rows_and_survives_reload(db_dir, pool):
    """register 必须按 D20 行形状落两张系统表，并可按列序重载。

    断言的改动：table_id 列没写、ordinal 错、类型串丢 BOOLEAN、只写一张表。
    """
    catalog = _load(db_dir, pool)

    _register(catalog, db_dir, "users", _columns())

    table_rows, column_rows = _rows(db_dir, pool)
    assert [(rid, values) for rid, values in table_rows] == [
        (1, (1, "users", "users.table"))
    ]
    assert [(rid, values) for rid, values in column_rows] == [
        (1, (1, 0, "id", "INT")),
        (2, (1, 1, "name", "TEXT")),
        (3, (1, 2, "flag", "BOOLEAN")),
    ]

    reloaded = _load(db_dir, pool)
    assert reloaded.get("users") == _columns()


def test_names_returns_sorted_user_tables(db_dir, pool):
    """names 必须稳定排序，不依赖系统表行序。

    断言的改动：names 依赖插入顺序或把系统表名混入结果。
    """
    catalog = _load(db_dir, pool)
    _register(catalog, db_dir, "zeta", (ColumnDef("id", SqlType.INT),))
    _register(catalog, db_dir, "alpha", (ColumnDef("id", SqlType.INT),))

    assert catalog.names() == ["alpha", "zeta"]


# ---- 注册边界 ----


def test_register_duplicate_table_leaves_system_rows_unchanged(db_dir, pool):
    """重复表名必须 E_TABLE_EXISTS，且不得追加任何系统行。

    断言的改动：重复注册覆盖旧表或留下半行。
    """
    catalog = _load(db_dir, pool)
    _register(catalog, db_dir, "users", _columns())
    before = _rows(db_dir, pool)

    _expect_code(lambda: catalog.register("users", _columns()), E_TABLE_EXISTS)

    assert _rows(db_dir, pool) == before


def test_register_empty_columns_leaves_system_rows_unchanged(db_dir, pool):
    """空列必须 E_DUP_COLUMN，且不得留下系统行。

    断言的改动：先写系统行再校验空列。
    """
    catalog = _load(db_dir, pool)

    _expect_code(lambda: catalog.register("users", ()), E_DUP_COLUMN)

    assert _rows(db_dir, pool) == ([], [])


def test_register_duplicate_columns_leaves_system_rows_unchanged(db_dir, pool):
    """重复列名必须 E_DUP_COLUMN，且不得留下系统行。

    断言的改动：重复列校验发生在写系统行之后。
    """
    catalog = _load(db_dir, pool)
    columns = (ColumnDef("id", SqlType.INT), ColumnDef("id", SqlType.TEXT))

    _expect_code(lambda: catalog.register("users", columns), E_DUP_COLUMN)

    assert _rows(db_dir, pool) == ([], [])


# ---- 注销边界 ----


def test_unregister_removes_rows_and_survives_reload(db_dir, pool):
    """unregister 必须清掉两表中的该表行，重载后不可见。

    断言的改动：只删表行或只删列行、内存与磁盘不一致。
    """
    catalog = _load(db_dir, pool)
    _register(catalog, db_dir, "users", _columns())
    _register(catalog, db_dir, "orders", (ColumnDef("oid", SqlType.INT),))

    catalog.unregister("users")
    # 用户表文件由 Storage 门面在注销成功后删除；这里模拟完整删除流程。
    (db_dir / "users.table").unlink()

    table_rows, column_rows = _rows(db_dir, pool)
    assert [values[1] for _rid, values in table_rows] == ["orders"]
    assert all(values[0] == table_rows[0][0] for _rid, values in column_rows)
    reloaded = _load(db_dir, pool)
    assert reloaded.names() == ["orders"]


def test_unregister_missing_table_raises_not_found(db_dir, pool):
    """注销不存在的表必须 E_TABLE_NOT_FOUND。

    断言的改动：unregister 静默通过。
    """
    catalog = _load(db_dir, pool)

    _expect_code(lambda: catalog.unregister("nobody"), E_TABLE_NOT_FOUND)


# ---- 系统表结构损坏 ----


def test_load_rejects_table_id_column_mismatch(db_dir, pool):
    """__sys_tables.table_id 必须等于本行 row_id（D22）。

    断言的改动：只信列值、不做 rid 一致性校验。
    """
    catalog = _load(db_dir, pool)
    _register(catalog, db_dir, "users", _columns())
    opened = open_system_tables(db_dir, pool)
    opened.tables.update(1, (99, "users", "users.table"))

    _expect_code(lambda: _load(db_dir, pool), E_STORAGE)


def test_load_rejects_column_row_without_table_row(db_dir, pool):
    """__sys_columns 的 table_id 必须引用存在的表行。

    断言的改动：忽略孤立列行、把不存在的表恢复出来。
    """
    opened = open_system_tables(db_dir, pool)
    opened.columns.insert((99, 0, "id", "INT"))

    _expect_code(lambda: _load(db_dir, pool), E_STORAGE)


def test_load_rejects_ordinal_gap(db_dir, pool):
    """表的 ordinal 必须是 0..N-1 连续，断号视为损坏。

    断言的改动：按存在顺序拼列、不检查 ordinal。
    """
    catalog = _load(db_dir, pool)
    _register(catalog, db_dir, "users", _columns())
    opened = open_system_tables(db_dir, pool)
    opened.columns.delete(2)  # 删 ordinal=1，留下 0 与 2

    _expect_code(lambda: _load(db_dir, pool), E_STORAGE)


def test_load_rejects_unknown_column_type(db_dir, pool):
    """column_type 必须属于 INT/TEXT/REAL/BOOLEAN。

    断言的改动：未知类型被放过、或由 SqlType 泄漏 ValueError。
    """
    catalog = _load(db_dir, pool)
    _register(catalog, db_dir, "users", _columns())
    opened = open_system_tables(db_dir, pool)
    opened.columns.update(1, (1, 0, "id", "FLOAT"))

    _expect_code(lambda: _load(db_dir, pool), E_STORAGE)


def test_load_rejects_duplicate_column_rows(db_dir, pool):
    """同一表的系统列行不得出现重复列名。

    断言的改动：重复列被后一行覆盖、或静默保留。
    """
    catalog = _load(db_dir, pool)
    _register(catalog, db_dir, "users", _columns())
    opened = open_system_tables(db_dir, pool)
    opened.columns.update(2, (1, 1, "id", "TEXT"))

    _expect_code(lambda: _load(db_dir, pool), E_STORAGE)


def test_load_rejects_file_name_mismatch(db_dir, pool):
    """file_name 必须等于 <表名>.table。

    断言的改动：相信任意 file_name、指向越界路径。
    """
    catalog = _load(db_dir, pool)
    _register(catalog, db_dir, "users", _columns())
    opened = open_system_tables(db_dir, pool)
    opened.tables.update(1, (1, "users", "other.table"))

    _expect_code(lambda: _load(db_dir, pool), E_STORAGE)


def test_load_rejects_invalid_stored_table_name(db_dir, pool):
    """系统表里出现非法/保留表名视为损坏。

    断言的改动：加载时不做名字格式校验。
    """
    catalog = _load(db_dir, pool)
    _register(catalog, db_dir, "users", _columns())
    opened = open_system_tables(db_dir, pool)
    opened.tables.update(1, (1, "__sys_hack", "users.table"))

    _expect_code(lambda: _load(db_dir, pool), E_STORAGE)


def test_load_rejects_missing_user_table_file(db_dir, pool):
    """有目录记录但用户 .table 文件缺失 → E_STORAGE。

    断言的改动：加载只信系统表、不检查文件存在性。
    """
    catalog = _load(db_dir, pool)
    _register(catalog, db_dir, "users", _columns())
    (db_dir / "users.table").unlink()

    _expect_code(lambda: _load(db_dir, pool), E_STORAGE)


def test_load_rejects_orphan_user_table_file(db_dir, pool):
    """目录里有 .table 但系统表无记录 → E_STORAGE（孤儿容忍废止）。

    断言的改动：忽略多余文件、静默清理。
    """
    (db_dir / "ghost.table").write_bytes(b"not a real table")

    _expect_code(lambda: _load(db_dir, pool), E_STORAGE)


def test_load_rejects_duplicate_table_name_rows(db_dir, pool):
    """__sys_tables 不得出现重复表名行。

    断言的改动：后一行覆盖前一行、或静默合并。
    """
    opened = open_system_tables(db_dir, pool)
    first = opened.tables.insert((0, "users", "users.table"))
    opened.tables.update(first, (first, "users", "users.table"))
    second = opened.tables.insert((0, "users", "users.table"))
    opened.tables.update(second, (second, "users", "users.table"))
    opened.columns.insert((first, 0, "id", "INT"))
    opened.columns.insert((second, 0, "id", "INT"))

    _expect_code(lambda: _load(db_dir, pool), E_STORAGE)
