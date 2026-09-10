"""M2 系统目录必须经过 BufferPool：命中/未命中/写回统计可观察。"""

from __future__ import annotations

from pathlib import Path

from contracts.ast import ColumnDef, SqlType
from storage import DatabaseServer
from storage.cache import BufferPool
from storage.catalog import Catalog
from storage.constants import SYS_TABLES_FILE_NAME
from storage.pager import create_table_file
from storage.syscatalog import create_empty_system_catalog


def test_create_table_catalog_io_uses_buffer_pool_stats(tmp_path):
    """建表产生的系统表读写必须体现在 cache_stats 的 misses/dirty_writes。

    断言的改动：Catalog 绕过 BufferPool 直接读写系统表文件。
    """
    data_dir = str(tmp_path / "data")
    server = DatabaseServer(data_dir)
    before = dict(server.cache_stats)

    storage = server.connect("main")
    storage.create_table("users", (ColumnDef("id", SqlType.INT),))

    after = dict(server.cache_stats)
    assert after["capacity"] == before["capacity"]
    assert after["misses"] > before["misses"]
    assert after["dirty_writes"] > before["dirty_writes"]


def test_restart_catalog_load_is_cache_observable(tmp_path):
    """重启加载系统表同样计入 misses/hits，证明走页缓存链路。"""
    data_dir = str(tmp_path / "data")
    server = DatabaseServer(data_dir)
    server.connect("main").create_table(
        "users", (ColumnDef("id", SqlType.INT),)
    )

    reopened = DatabaseServer(data_dir)
    reopened.connect("main")

    after = dict(reopened.cache_stats)
    assert after["misses"] > 0


def test_system_table_pages_evict_under_tiny_pool_and_reload(tmp_path):
    """容量 2 的池下反复注册必须发生淘汰，但重载后 Catalog 仍完整。"""
    db_dir = tmp_path / "main"
    db_dir.mkdir()
    create_empty_system_catalog(db_dir)
    pool = BufferPool(capacity=2)
    catalog = Catalog(db_dir, pool)
    catalog.load()

    for index in range(12):
        name = f"t{index}"
        create_table_file(db_dir / f"{name}.table")
        catalog.register(name, (ColumnDef("id", SqlType.INT),))

    stats = pool.stats
    assert stats["evictions"] > 0
    assert stats["dirty_writes"] > 0

    reloaded = Catalog(db_dir, BufferPool(capacity=16))
    reloaded.load()
    assert len(reloaded.names()) == 12


def test_read_only_catalog_operations_do_not_write_dirty_pages(tmp_path):
    """describe/list/scan/connect 重扫只读，不得增加 dirty_writes。"""
    data_dir = str(tmp_path / "data")
    server = DatabaseServer(data_dir)
    storage = server.connect("main")
    storage.create_table("users", (ColumnDef("id", SqlType.INT),))
    before = dict(server.cache_stats)

    storage.list_tables()
    storage.describe("users")
    list(storage.scan("users"))
    server.connect("main")

    after = dict(server.cache_stats)
    assert after["hits"] + after["misses"] > before["hits"] + before["misses"]
    assert after["dirty_writes"] == before["dirty_writes"]


def test_drop_table_keeps_system_catalog_usable(tmp_path):
    """drop_table 只 discard 用户表帧；系统表仍可继续注册与重启加载。"""
    data_dir = str(tmp_path / "data")
    server = DatabaseServer(data_dir)
    storage = server.connect("main")
    storage.create_table("users", (ColumnDef("id", SqlType.INT),))

    storage.drop_table("users")
    storage.create_table("orders", (ColumnDef("oid", SqlType.INT),))

    assert storage.list_tables() == ["orders"]
    reopened = DatabaseServer(data_dir).connect("main")
    assert reopened.describe("orders").columns == (ColumnDef("oid", SqlType.INT),)


def test_cache_stats_hit_rate_and_keys_are_consistent(tmp_path):
    """cache_stats 键齐全，hit_rate 与 hits/(hits+misses) 一致。"""
    server = DatabaseServer(str(tmp_path / "data"))
    server.connect("main")
    stats = server.cache_stats

    accesses = stats["hits"] + stats["misses"]
    expected = (stats["hits"] / accesses) if accesses else 0.0
    assert stats["hit_rate"] == expected
    assert set(stats) == {
        "capacity",
        "hits",
        "misses",
        "evictions",
        "dirty_writes",
        "hit_rate",
    }


def test_user_table_named_sys_tables_does_not_collide_with_system_file(tmp_path):
    """用户表 sys_tables 写 sys_tables.table，不得碰 sys_tables.db。"""
    data_dir = str(tmp_path / "data")
    server = DatabaseServer(data_dir)
    storage = server.connect("main")

    storage.create_table("sys_tables", (ColumnDef("id", SqlType.INT),))
    rid = storage.insert("sys_tables", (1,))
    assert list(storage.scan("sys_tables")) == [(rid, (1,))]
    assert (Path(data_dir) / "main" / SYS_TABLES_FILE_NAME).is_file()

    storage.drop_table("sys_tables")
    assert DatabaseServer(data_dir).connect("main").list_tables() == []
