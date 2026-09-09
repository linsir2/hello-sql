"""pager M2 补充测试：page_count 原语（engine scan/遍历需要，PRD §6.5 未列）。

命名规则沿用前几阶段：每个测试写清“会让它失败的生产改动”。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from contracts.errors import E_STORAGE, SqlError
from storage.cache import BufferPool
from storage.constants import PAGE_SIZE
from storage.pager import alloc_page, create_table_file, page_count


@pytest.fixture
def pool() -> BufferPool:
    return BufferPool(capacity=8)


@pytest.fixture
def table_path(tmp_path) -> Path:
    return tmp_path / "users.table"


def _expect_storage_error(call) -> None:
    with pytest.raises(SqlError) as exc:
        call()
    assert exc.value.code == E_STORAGE


def test_page_count_is_one_right_after_create(pool, table_path):
    """建表后文件只有页 0，page_count 必须返回 1。

    断言的改动：page_count 把页 0 漏算或把长度算成字节数。
    """
    create_table_file(table_path)

    assert page_count(pool, table_path) == 1


def test_page_count_grows_with_each_alloc(pool, table_path):
    """alloc 一页 page_count 就 +1，与文件长度/4096 一致。

    断言的改动：page_count 用陈旧缓存、或没按文件实际长度算。
    """
    create_table_file(table_path)
    alloc_page(pool, table_path)
    alloc_page(pool, table_path)

    assert page_count(pool, table_path) == 3
    assert table_path.stat().st_size == PAGE_SIZE * 3


def test_page_count_missing_file_raises_storage(pool, table_path):
    """文件不存在时 page_count 抛 E_STORAGE，不静默返回 0。

    断言的改动：page_count 把缺失文件当空表返回 0。
    """
    _expect_storage_error(lambda: page_count(pool, table_path))


def test_page_count_half_page_file_raises_storage(pool, table_path):
    """文件出现半页时 page_count 抛 E_STORAGE（损坏路径同 read/alloc）。

    断言的改动：page_count 无视非整页长度向下取整页数。
    """
    create_table_file(table_path)
    table_path.write_bytes(table_path.read_bytes() + b"x" * 100)

    _expect_storage_error(lambda: page_count(pool, table_path))
