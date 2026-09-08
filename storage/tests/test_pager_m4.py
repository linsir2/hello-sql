"""pager M4 测试：空闲页链表（D05/D06）——free_page、alloc 弹链、free_pages。

命名规则沿用前几阶段；链表行为断言在显式 flush 后读真实文件字节。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from contracts.errors import E_STORAGE, SqlError
from storage.cache import BufferPool
from storage.constants import PAGE0_FREE_HEAD_OFFSET, PAGE_SIZE
from storage.pager import (
    alloc_page,
    create_table_file,
    free_page,
    free_pages,
    page_count,
)


@pytest.fixture
def pool() -> BufferPool:
    return BufferPool(capacity=16)


@pytest.fixture
def table_path(tmp_path) -> Path:
    return tmp_path / "users.table"


def _make_pages(table_path: Path, count: int) -> None:
    create_table_file(table_path)
    tmp_pool = BufferPool(capacity=4)
    for _ in range(count):
        alloc_page(tmp_pool, table_path)


def _flush(pool: BufferPool, table_path: Path) -> None:
    pool.flush(table_path)


def _free_head(table_path: Path) -> int:
    raw = table_path.read_bytes()
    return int.from_bytes(
        raw[PAGE0_FREE_HEAD_OFFSET : PAGE0_FREE_HEAD_OFFSET + 4], "little"
    )


def _first4(table_path: Path, page_no: int) -> int:
    raw = table_path.read_bytes()
    start = page_no * PAGE_SIZE
    return int.from_bytes(raw[start : start + 4], "little")


def _expect_storage_error(call) -> None:
    with pytest.raises(SqlError) as exc:
        call()
    assert exc.value.code == E_STORAGE


# ---- free_page / free_pages / alloc 弹链 ----


def test_alloc_without_free_list_keeps_appending(pool, table_path):
    """没有空闲页时 alloc 仍在文件末尾追加（M1 行为不变，D06）。

    断言的改动：无 free list 时 alloc 报错或提前复用错误页号。
    """
    _make_pages(table_path, 2)

    assert alloc_page(pool, table_path) == 3
    assert page_count(pool, table_path) == 4
    assert free_pages(pool, table_path) == []


def test_free_page_pushes_onto_chain_and_writes_next(pool, table_path):
    """free_page：页 0 free_head 指向新空页，该页前 4 B 存旧链头。

    断言的改动：free_head/next 写反、链头不更新、或 next 占用别的位置。
    """
    _make_pages(table_path, 3)
    free_page(pool, table_path, 2)
    _flush(pool, table_path)
    assert free_pages(pool, table_path) == [2]
    assert _free_head(table_path) == 2
    assert _first4(table_path, 2) == 0  # 旧链头为空 → next=0

    free_page(pool, table_path, 1)
    _flush(pool, table_path)
    assert free_pages(pool, table_path) == [1, 2]  # 链序 = 最新在前
    assert _free_head(table_path) == 1
    assert _first4(table_path, 1) == 2
    assert _first4(table_path, 2) == 0


def test_alloc_pops_newest_free_page_before_appending(pool, table_path):
    """alloc 优先弹链头（LIFO）：free 1、2 后 alloc 应依次返回 2、1，不扩文件。

    断言的改动：alloc 忽略 free list 直接追加，文件不断增长。
    """
    _make_pages(table_path, 3)
    free_page(pool, table_path, 1)
    free_page(pool, table_path, 2)

    assert alloc_page(pool, table_path) == 2
    assert alloc_page(pool, table_path) == 1
    assert page_count(pool, table_path) == 4  # 未追加，仍是 0..3
    _flush(pool, table_path)
    assert _free_head(table_path) == 0

    assert alloc_page(pool, table_path) == 4  # 链表空 → 追加
    assert page_count(pool, table_path) == 5


def test_free_page_does_not_shrink_file(pool, table_path):
    """free 只改账本不改文件长度（D06：只增不减、不收缩）。

    断言的改动：free_page 截断文件或把页清零撑大。
    """
    _make_pages(table_path, 3)
    before = table_path.stat().st_size

    free_page(pool, table_path, 2)
    _flush(pool, table_path)

    assert table_path.stat().st_size == before


def test_free_pages_empty_on_fresh_file(pool, table_path):
    """新表无空闲页：free_pages 返回空列表。

    断言的改动：free_pages 把数据页误判为空闲页。
    """
    create_table_file(table_path)

    assert free_pages(pool, table_path) == []


def test_free_page_rejects_page0_and_out_of_range(pool, table_path):
    """页 0 不可释放（永存文件头）；越界页号 free 抛 E_STORAGE。

    断言的改动：free_page 把页 0/不存在的页加进链表。
    """
    _make_pages(table_path, 2)

    _expect_storage_error(lambda: free_page(pool, table_path, 0))
    _expect_storage_error(lambda: free_page(pool, table_path, 99))


def test_free_page_missing_file_raises_storage(pool, table_path):
    """free 不存在的文件抛 E_STORAGE。

    断言的改动：free_page 静默新建文件。
    """
    _expect_storage_error(lambda: free_page(pool, table_path, 1))


def test_free_list_survives_new_pool_restart(pool, table_path):
    """flush 后换新 BufferPool，free 链仍可读（free_head 持久化在页 0）。

    断言的改动：free list 只存在旧 pool 内存里，重启后丢失。
    """
    _make_pages(table_path, 3)
    free_page(pool, table_path, 2)
    free_page(pool, table_path, 1)
    _flush(pool, table_path)

    fresh = BufferPool(capacity=8)

    assert free_pages(fresh, table_path) == [1, 2]
    assert alloc_page(fresh, table_path) == 1


# ---- 损坏路径 ----


def test_free_pages_detects_self_loop_chain(pool, table_path):
    """空闲页 next 指向自己 = 链表成环 → E_STORAGE（防 alloc 死循环）。

    断言的改动：free_pages 遇环无限循环或静默返回半截链表。
    """
    _make_pages(table_path, 2)
    raw = bytearray(table_path.read_bytes())
    raw[PAGE0_FREE_HEAD_OFFSET : PAGE0_FREE_HEAD_OFFSET + 4] = (1).to_bytes(
        4, "little"
    )
    raw[PAGE_SIZE : PAGE_SIZE + 4] = (1).to_bytes(4, "little")  # 页 1 next=自己
    table_path.write_bytes(raw)

    _expect_storage_error(lambda: free_pages(pool, table_path))


def test_alloc_detects_free_head_out_of_range(pool, table_path):
    """free_head 指向文件里不存在的页 → alloc 抛 E_STORAGE。

    断言的改动：alloc 把越界 free_head 当 0 直接追加，掩盖损坏。
    """
    _make_pages(table_path, 2)
    raw = bytearray(table_path.read_bytes())
    raw[PAGE0_FREE_HEAD_OFFSET : PAGE0_FREE_HEAD_OFFSET + 4] = (99).to_bytes(
        4, "little"
    )
    table_path.write_bytes(raw)

    _expect_storage_error(lambda: alloc_page(pool, table_path))
