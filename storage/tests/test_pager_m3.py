"""pager M3 测试：read/write 真正改道 BufferPool（D11/D17）。

M1/M2 是直通文件过渡；M3 起 write_page 只标脏，落盘由 flush 负责，
read_page 命中/未命中计入 pool 统计。这两个测试驱动 pager 换道。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from storage.cache import BufferPool
from storage.constants import PAGE_SIZE
from storage.pager import alloc_page, create_table_file, read_page, write_page


@pytest.fixture
def pool() -> BufferPool:
    return BufferPool(capacity=8)


@pytest.fixture
def table_path(tmp_path) -> Path:
    return tmp_path / "users.table"


def _page_bytes(table_path: Path, page_no: int) -> bytes:
    raw = table_path.read_bytes()
    return raw[page_no * PAGE_SIZE : (page_no + 1) * PAGE_SIZE]


def test_pager_read_page_goes_through_buffer_pool_stats(pool, table_path):
    """pager.read_page 必须走 pool：同页第二次读是 hit 而非再次读盘。

    断言的改动：pager 仍直读文件（pool 统计永远全 0）。
    """
    create_table_file(table_path)

    assert read_page(pool, table_path, 0) == table_path.read_bytes()[:PAGE_SIZE]
    assert read_page(pool, table_path, 0) == table_path.read_bytes()[:PAGE_SIZE]

    assert pool.stats["misses"] == 1
    assert pool.stats["hits"] == 1


def test_pager_write_page_buffers_dirty_until_flush(pool, table_path):
    """write_page 后磁盘必须仍是旧内容，只有 pool.flush 才真正落盘（D11）。

    断言的改动：pager 仍直写文件（flush 前磁盘就变了，重启语义转移到门面）。
    """
    create_table_file(table_path)
    alloc_page(pool, table_path)
    data = bytes(range(256)) * 16

    write_page(pool, table_path, 1, data)

    assert _page_bytes(table_path, 1) != data  # 还没落盘
    assert pool.stats["dirty_writes"] == 0

    pool.flush(table_path)

    assert _page_bytes(table_path, 1) == data
    assert pool.stats["dirty_writes"] == 1
