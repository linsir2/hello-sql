"""Storage M4 集成测试：跨库缓存隔离、带空闲页的 drop 清理、重启后复用。

跨库隔离与 drop discard 的机制 M3 已实现，M4 补验收：空闲页（脏帧）
存在时这些路径也必须正确。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from contracts.ast import ColumnDef, SqlType
from storage import DatabaseServer
from storage.constants import TABLE_FILE_SUFFIX


def _columns() -> tuple[ColumnDef, ColumnDef]:
    return (ColumnDef("id", SqlType.INT), ColumnDef("tag", SqlType.TEXT))


@pytest.fixture
def data_dir(tmp_path) -> str:
    return str(tmp_path / "data")


@pytest.fixture
def server(data_dir) -> DatabaseServer:
    return DatabaseServer(data_dir)


def _table_size(data_dir: str, db_name: str, table_name: str) -> int:
    path = Path(data_dir) / db_name / f"{table_name}{TABLE_FILE_SUFFIX}"
    return path.stat().st_size


def _fill_and_free_all(storage, name: str, count: int = 200) -> None:
    """建表插 count 行再全部删光，制造多页空闲链表。"""
    storage.create_table(name, _columns())
    for i in range(1, count + 1):
        storage.insert(name, (i, f"tag-{i}"))
    rows = list(storage.scan(name))
    for row_id, _values in rows:
        storage.delete_row(name, row_id)


def test_cross_database_same_table_name_do_not_share_frames(server):
    """两个库同名同页号的表必须各自独立（key=绝对路径，D09）。

    断言的改动：缓存只按页号不按路径隔离，跨库 scan 串数据。
    """
    server.create_database("shop_a")
    server.create_database("shop_b")
    a = server.connect("shop_a")
    b = server.connect("shop_b")
    a.create_table("users", _columns())
    b.create_table("users", _columns())
    a.insert("users", (1, "from-a"))
    b.insert("users", (2, "from-b"))
    server._pool.capacity = 1  # 强制两库页互相淘汰，仍不得串

    assert list(a.scan("users")) == [(1, (1, "from-a"))]
    assert list(b.scan("users")) == [(1, (2, "from-b"))]


def test_drop_table_with_free_pages_allows_same_name_recreate(server):
    """表里有空闲页（脏帧）时 drop 必须 discard 后删文件；同名重建计数归 1。

    断言的改动：drop 忘了 discard 空闲页帧，重建后 insert 命中旧页 0 计数。
    """
    storage = server.connect("main")
    _fill_and_free_all(storage, "users")

    storage.drop_table("users")
    storage.create_table("users", _columns())

    assert storage.insert("users", (7, "fresh")) == 1


def test_drop_database_with_free_pages_allows_same_name_recreate(server):
    """库里有空闲页时删库 → 重建同名库同名表，计数与数据都从新文件开始。

    断言的改动：drop_database 漏 discard 目录下空闲页帧。
    """
    server.create_database("shop")
    storage = server.connect("shop")
    _fill_and_free_all(storage, "users")

    server.drop_database("shop")
    server.create_database("shop")

    fresh = server.connect("shop")
    fresh.create_table("users", _columns())
    assert fresh.insert("users", (5, "new")) == 1
    assert list(fresh.scan("users")) == [(1, (5, "new"))]


def test_free_list_recovery_through_public_api_after_restart(
    server, data_dir
):
    """公开 API 删光多页后重启：scan 跳过空闲页，insert 复用旧页不扩文件。

    断言的改动：free list 没持久化到页 0，重启后文件继续增长/scan 报损坏。
    """
    storage = server.connect("main")
    _fill_and_free_all(storage, "users")
    size_before = _table_size(data_dir, "main", "users")

    reopened = DatabaseServer(data_dir).connect("main")
    assert list(reopened.scan("users")) == []
    assert reopened.insert("users", (201, "reborn")) == 201

    assert _table_size(data_dir, "main", "users") == size_before
    assert list(reopened.scan("users")) == [(201, (201, "reborn"))]
