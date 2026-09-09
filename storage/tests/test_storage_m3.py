"""Storage/DatabaseServer M3 集成测试：方法末 flush、drop discard、cache_stats。

驱动点：M3 起 pager 只标脏，公开改数据方法返回前必须 flush（D11）；
drop 前必须 discard，否则同名重建会命中旧缓存帧。
"""

from __future__ import annotations

import pytest

from contracts.ast import ColumnDef, SqlType
from storage import DatabaseServer


def _columns() -> tuple[ColumnDef, ColumnDef]:
    return (ColumnDef("id", SqlType.INT), ColumnDef("tag", SqlType.TEXT))


@pytest.fixture
def data_dir(tmp_path) -> str:
    return str(tmp_path / "data")


@pytest.fixture
def server(data_dir) -> DatabaseServer:
    return DatabaseServer(data_dir)


def test_insert_update_delete_flush_dirty_pages_before_return(server):
    """每个改数据的公开方法结束前必须 flush 涉及脏页（dirty_writes 增长）。

    断言的改动：flush 挪进 engine/漏在门面外，dirty_writes 一直是 0。
    """
    storage = server.connect("main")
    storage.create_table("users", _columns())

    storage.insert("users", (1, "a"))
    writes_after_insert = server._pool.stats["dirty_writes"]
    storage.update_row("users", 1, (2, "b"))
    writes_after_update = server._pool.stats["dirty_writes"]
    storage.delete_row("users", 1)
    writes_after_delete = server._pool.stats["dirty_writes"]

    assert writes_after_insert >= 1
    assert writes_after_update > writes_after_insert
    assert writes_after_delete > writes_after_update


def test_drop_database_discards_frames_before_same_name_recreate(server):
    """删库再建同名库同名表时，不能命中旧库遗留的缓存帧（D09/D11）。

    断言的改动：drop_database 不 discard，重建后 insert 从旧页 0 帧拿到旧计数。
    """
    server.create_database("shop")
    storage = server.connect("shop")
    storage.create_table("items", _columns())
    storage.insert("items", (1, "old"))

    server.drop_database("shop")
    server.create_database("shop")

    fresh = server.connect("shop")
    fresh.create_table("items", _columns())
    assert fresh.insert("items", (7, "new")) == 1
    assert list(fresh.scan("items")) == [(1, (7, "new"))]


def test_cache_stats_exposes_pool_snapshot_on_database_server(server):
    """DatabaseServer.cache_stats 是只读属性，返回 pool 统计快照（D18）。

    断言的改动：cache_stats 缺失、或返回内部可变对象。
    """
    storage = server.connect("main")
    storage.create_table("users", _columns())
    storage.insert("users", (1, "a"))
    storage.insert("users", (2, "b"))

    stats = server.cache_stats

    assert set(stats) == {
        "capacity",
        "hits",
        "misses",
        "evictions",
        "dirty_writes",
        "hit_rate",
    }
    assert stats["dirty_writes"] >= 1
    snapshot = server.cache_stats
    assert snapshot == stats
