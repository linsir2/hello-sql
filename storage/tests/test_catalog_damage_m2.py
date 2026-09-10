"""M3 Catalog 损坏矩阵：页级、记录级与结构级损坏都必须 E_STORAGE。"""

from __future__ import annotations

from pathlib import Path

import pytest

from contracts.ast import ColumnDef, SqlType
from contracts.errors import E_STORAGE, SqlError
from storage.cache import BufferPool
from storage.catalog import Catalog
from storage.constants import PAGE_SIZE, SYS_TABLES_FILE_NAME
from storage.pager import create_table_file
from storage.syscatalog import create_empty_system_catalog, open_system_tables


def _columns() -> tuple[ColumnDef, ...]:
    return (
        ColumnDef("id", SqlType.INT),
        ColumnDef("name", SqlType.TEXT),
    )


@pytest.fixture
def db_dir(tmp_path) -> Path:
    path = tmp_path / "main"
    path.mkdir()
    create_empty_system_catalog(path)
    return path


@pytest.fixture
def pool() -> BufferPool:
    return BufferPool(capacity=8)


def _expect_code(call, code: str) -> None:
    with pytest.raises(SqlError) as exc:
        call()
    assert exc.value.code == code


def _load(db_dir: Path, pool: BufferPool) -> Catalog:
    catalog = Catalog(db_dir, pool)
    catalog.load()
    return catalog


def _register(catalog: Catalog, db_dir: Path, name: str) -> None:
    create_table_file(db_dir / f"{name}.table")
    catalog.register(name, _columns())


# ---- 页级损坏 ----


def test_load_rejects_bad_page0_magic(db_dir, pool):
    """系统表页 0 magic 被改坏 → E_STORAGE。"""
    raw = bytearray((db_dir / SYS_TABLES_FILE_NAME).read_bytes())
    raw[0:4] = b"BAD!"
    (db_dir / SYS_TABLES_FILE_NAME).write_bytes(raw)

    _expect_code(lambda: _load(db_dir, BufferPool(8)), E_STORAGE)


def test_load_rejects_truncated_system_table(db_dir, pool):
    """系统表截成半页 → E_STORAGE。"""
    path = db_dir / SYS_TABLES_FILE_NAME
    path.write_bytes(path.read_bytes()[: PAGE_SIZE // 2])

    _expect_code(lambda: _load(db_dir, BufferPool(8)), E_STORAGE)


def test_load_rejects_non_page_aligned_system_table(db_dir, pool):
    """系统表长度非整页 → E_STORAGE。"""
    path = db_dir / SYS_TABLES_FILE_NAME
    path.write_bytes(path.read_bytes() + b"x")

    _expect_code(lambda: _load(db_dir, BufferPool(8)), E_STORAGE)


def test_load_rejects_corrupt_record_bytes(db_dir, pool):
    """记录里的 TEXT 长度字段被改坏 → decode E_STORAGE。"""
    _register(_load(db_dir, pool), db_dir, "users")
    path = db_dir / SYS_TABLES_FILE_NAME
    raw = bytearray(path.read_bytes())
    # 页 1 第一条记录：u64 rid + q table_id + u32 name_len ...
    name_len_offset = PAGE_SIZE + 8 + 8 + 8
    raw[name_len_offset : name_len_offset + 4] = (0xFFFF).to_bytes(4, "little")
    path.write_bytes(raw)

    _expect_code(lambda: _load(db_dir, BufferPool(8)), E_STORAGE)


# ---- 结构级损坏 ----


def test_load_rejects_uppercase_table_name_row(db_dir, pool):
    """系统表里的表名必须是小写标识符。"""
    _register(_load(db_dir, pool), db_dir, "users")
    opened = open_system_tables(db_dir, pool)
    opened.tables.update(1, (1, "Users", "users.table"))
    pool.flush()

    _expect_code(lambda: _load(db_dir, BufferPool(8)), E_STORAGE)


def test_load_rejects_invalid_column_name_row(db_dir, pool):
    """系统表里的列名必须是小写标识符。"""
    _register(_load(db_dir, pool), db_dir, "users")
    opened = open_system_tables(db_dir, pool)
    opened.columns.update(1, (1, 0, "Id", "INT"))
    pool.flush()

    _expect_code(lambda: _load(db_dir, BufferPool(8)), E_STORAGE)


def test_load_rejects_negative_ordinal(db_dir, pool):
    """ordinal 不允许为负。"""
    _register(_load(db_dir, pool), db_dir, "users")
    opened = open_system_tables(db_dir, pool)
    opened.columns.update(1, (1, -1, "id", "INT"))
    pool.flush()

    _expect_code(lambda: _load(db_dir, BufferPool(8)), E_STORAGE)


def test_load_rejects_duplicate_ordinal(db_dir, pool):
    """同一表的两条列行 ordinal 不允许重复。"""
    _register(_load(db_dir, pool), db_dir, "users")
    opened = open_system_tables(db_dir, pool)
    opened.columns.update(2, (1, 0, "name", "TEXT"))
    pool.flush()

    _expect_code(lambda: _load(db_dir, BufferPool(8)), E_STORAGE)


def test_load_rejects_table_without_column_rows(db_dir, pool):
    """有表行但一条列行都没有 → E_STORAGE。"""
    opened = open_system_tables(db_dir, pool)
    table_id = opened.tables.insert((0, "users", "users.table"))
    opened.tables.update(table_id, (table_id, "users", "users.table"))
    pool.flush()

    _expect_code(lambda: _load(db_dir, BufferPool(8)), E_STORAGE)


def test_load_rejects_file_name_path_traversal(db_dir, pool):
    """file_name 必须是 <表名>.table，不得指向其它路径。"""
    _register(_load(db_dir, pool), db_dir, "users")
    opened = open_system_tables(db_dir, pool)
    opened.tables.update(1, (1, "users", "../users.table"))
    pool.flush()

    _expect_code(lambda: _load(db_dir, BufferPool(8)), E_STORAGE)


def test_non_table_suffix_files_are_ignored(db_dir, pool):
    """非 .table 后缀的杂文件不参与“记录↔文件”一致性检查。"""
    (db_dir / "notes.table.bak").write_text("x", encoding="utf-8")

    assert _load(db_dir, BufferPool(8)).names() == []
