"""编排式集成测试：多库、多表、inline/溢出混存、缓存压力下的真实脚本。"""

from __future__ import annotations

from pathlib import Path

import pytest

from contracts.ast import ColumnDef, SqlType
from contracts.errors import E_TABLE_NOT_FOUND, SqlError
from storage import DatabaseServer
from storage.constants import TABLE_FILE_SUFFIX


def _small_columns() -> tuple[ColumnDef, ColumnDef, ColumnDef]:
    return (
        ColumnDef("id", SqlType.INT),
        ColumnDef("name", SqlType.TEXT),
        ColumnDef("score", SqlType.REAL),
    )


def _notes_columns() -> tuple[ColumnDef]:
    return (ColumnDef("body", SqlType.TEXT),)


@pytest.fixture
def data_dir(tmp_path) -> str:
    return str(tmp_path / "data")


def _reopen(data_dir: str):
    return DatabaseServer(data_dir)


def test_two_databases_independent_lifecycle(data_dir):
    """shop_a/shop_b 同名表各自独立，删除 a 不影响 b，重启后 b 仍在。"""
    server = _reopen(data_dir)
    server.create_database("shop_a")
    server.create_database("shop_b")
    a = server.connect("shop_a")
    b = server.connect("shop_b")
    for storage, prefix in ((a, "a"), (b, "b")):
        storage.create_table("users", _small_columns())
        storage.insert("users", (1, f"{prefix}-user", 1.5))

    server.drop_database("shop_a")
    server2 = _reopen(data_dir)
    fresh_b = server2.connect("shop_b")

    assert list(fresh_b.scan("users")) == [(1, (1, "b-user", 1.5))]
    with pytest.raises(SqlError) as exc:
        server2.connect("shop_a")
    assert exc.value.code == "E_DATABASE_NOT_FOUND"


def test_multi_table_mixed_inline_and_overflow_survives_restart(data_dir):
    """同一库里小表 + 大文本表并行操作，重启后各自完整。"""
    storage = _reopen(data_dir).connect("main")
    storage.create_table("users", _small_columns())
    storage.create_table("notes", _notes_columns())
    for i in range(1, 31):
        storage.insert("users", (i, f"u{i}", float(i) / 2))
    storage.insert("notes", ("n" * 9000,))
    storage.insert("notes", ("short",))
    storage.update_row("notes", 2, ("m" * 5000,))
    storage.delete_row("users", 7)

    reopened = _reopen(data_dir).connect("main")

    users = sorted(reopened.scan("users"), key=lambda r: r[0])
    assert [r[0] for r in users] == list(range(1, 31))[:6] + list(range(8, 31))
    assert sorted(reopened.scan("notes"), key=lambda r: r[0]) == [
        (1, ("n" * 9000,)),
        (2, ("m" * 5000,)),
    ]


def test_cache_pressure_under_choreography(data_dir):
    """缓存压到 2 帧时做多页/溢出/删除/更新，结果仍与完整 scan 一致。"""
    server = _reopen(data_dir)
    server._pool.capacity = 2
    storage = server.connect("main")
    storage.create_table("users", _small_columns())
    for i in range(1, 81):
        storage.insert("users", (i, f"tag-{i}", float(i)))
    overflow_rid = storage.insert("users", (101, "x" * 7000, 1.0))
    for rid in list(range(1, 81, 3)):
        storage.delete_row("users", rid)
    storage.update_row(
        "users", overflow_rid, (101, "back-to-small", 2.0)
    )

    reopened = _reopen(data_dir).connect("main")
    rows = list(reopened.scan("users"))

    assert (overflow_rid, (101, "back-to-small", 2.0)) in rows
    assert len(rows) == 54  # 80 + 1 溢出 - 27 删除 = 54


def test_drop_table_then_sibling_table_unaffected(data_dir):
    """drop 一张表不能影响同库其他表及其缓存帧。"""
    storage = _reopen(data_dir).connect("main")
    storage.create_table("users", _small_columns())
    storage.create_table("notes", _notes_columns())
    storage.insert("users", (1, "keep", 1.0))
    storage.insert("notes", ("n" * 6000,))

    storage.drop_table("notes")

    assert list(storage.scan("users")) == [(1, (1, "keep", 1.0))]
    with pytest.raises(SqlError) as exc:
        storage.describe("notes")
    assert exc.value.code == E_TABLE_NOT_FOUND


def test_path_file_lifecycle_after_drop_recreate_reopen(data_dir):
    """drop→重建→重启：同名表文件全新、无旧缓存/旧映射干扰。"""
    path = Path(data_dir) / "main" / f"users{TABLE_FILE_SUFFIX}"
    storage = _reopen(data_dir).connect("main")
    storage.create_table("users", _small_columns())
    storage.insert("users", (1, "old", 1.0))
    storage.drop_table("users")
    storage.create_table("users", _small_columns())
    new_id = storage.insert("users", (7, "new", 2.0))

    reopened = _reopen(data_dir).connect("main")

    assert new_id == 1
    assert list(reopened.scan("users")) == [(1, (7, "new", 2.0))]
    assert path.is_file()
