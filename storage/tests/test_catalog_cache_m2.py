"""M2 系统目录必须经过 BufferPool：命中/未命中/写回统计可观察。"""

from __future__ import annotations

from contracts.ast import ColumnDef, SqlType
from storage import DatabaseServer


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
