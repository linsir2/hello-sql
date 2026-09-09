"""BufferPool M3 测试：LRU、pin/dirty、缺页读盘、淘汰写回、flush/discard、统计。

命名规则沿用前几阶段：每个测试写清“会让它失败的生产改动”。
磁盘状态断言一律在显式 flush/discard 后读真实文件，不靠缓存自证。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from contracts.errors import E_STORAGE, SqlError
from storage.cache import BufferPool
from storage.constants import PAGE_SIZE
from storage.pager import alloc_page, create_table_file


@pytest.fixture
def pool() -> BufferPool:
    return BufferPool(capacity=8)


@pytest.fixture
def table_path(tmp_path) -> Path:
    return tmp_path / "users.table"


def _make_two_pages(table_path: Path) -> None:
    """建表文件并追加一页，供缓存按页读写。"""
    create_table_file(table_path)
    alloc_page(BufferPool(capacity=4), table_path)


def _expect_storage_error(call) -> None:
    with pytest.raises(SqlError) as exc:
        call()
    assert exc.value.code == E_STORAGE


# ---- 命中 / 未命中 / pin / 统计（slice 1）----


def test_get_page_miss_reads_disk_into_cached_frame(pool, table_path):
    """首次取页 = 未命中：从磁盘读整页进缓存并返回帧，统计 misses=1。

    断言的改动：get_page 返回全零新帧不读盘、或 miss 计数不对。
    """
    _make_two_pages(table_path)

    frame = pool.get_page(table_path, 0)
    try:
        assert bytes(frame) == table_path.read_bytes()[:PAGE_SIZE]
        assert pool.stats["misses"] == 1
        assert pool.stats["hits"] == 0
    finally:
        pool.unpin_page(table_path, 0)


def test_get_page_hit_returns_same_frame_and_counts_hit(pool, table_path):
    """同一页第二次取 = 命中：返回同一帧引用（缓存语义），hits/misses 各 1。

    断言的改动：每次 get 都新建帧副本（改了内存页下次取不到）。
    """
    _make_two_pages(table_path)
    first = pool.get_page(table_path, 1)
    pool.unpin_page(table_path, 1)

    second = pool.get_page(table_path, 1)
    try:
        assert second is first
        assert pool.stats["hits"] == 1
        assert pool.stats["misses"] == 1
    finally:
        pool.unpin_page(table_path, 1)


def test_memory_modification_survives_unpin_and_next_get(pool, table_path):
    """改内存帧后 unpin 再取同一页，应看到改动（帧是共享引用不是副本）。

    断言的改动：get/unpin 之间把帧数据复制走/丢弃，内存修改丢失。
    """
    _make_two_pages(table_path)
    frame = pool.get_page(table_path, 1)
    frame[0:4] = b"DIRT"
    pool.unpin_page(table_path, 1)

    again = pool.get_page(table_path, 1)
    try:
        assert bytes(again[0:4]) == b"DIRT"
    finally:
        pool.unpin_page(table_path, 1)


def test_unpin_without_pin_raises_storage(pool, table_path):
    """pin 计数为 0 时 unpin 是内部错误 → E_STORAGE（防 pin 泄漏掩盖 bug）。

    断言的改动：unpin 越界时静默把 pin 减成负数。
    """
    _make_two_pages(table_path)

    _expect_storage_error(lambda: pool.unpin_page(table_path, 1))


def test_stats_snapshot_and_reset_keep_frames(pool, table_path):
    """stats 只读快照含四项计数；reset_stats 清零但不清空已缓存帧。

    断言的改动：reset 连帧一起丢（下一次取页又 miss），或 stats 返回内部对象。
    """
    _make_two_pages(table_path)
    frame = pool.get_page(table_path, 0)
    pool.unpin_page(table_path, 0)

    snapshot = pool.stats
    pool.reset_stats()

    assert snapshot["hits"] == 0
    assert snapshot["misses"] == 1
    assert snapshot["evictions"] == 0
    assert snapshot["dirty_writes"] == 0
    assert pool.stats["misses"] == 0
    assert pool.stats["hit_rate"] == 0.0

    again = pool.get_page(table_path, 0)
    try:
        assert again is frame  # 帧仍在，reset 只动计数
    finally:
        pool.unpin_page(table_path, 0)


def test_get_page_missing_file_raises_storage(pool, table_path):
    """缓存读不存在的文件抛 E_STORAGE，不得静默建空帧。

    断言的改动：get_page 吞掉 OSError 返回零页。
    """
    _expect_storage_error(lambda: pool.get_page(table_path, 0))


def test_get_page_beyond_file_end_raises_storage(pool, table_path):
    """读超出文件长度的页 = 短读 → E_STORAGE（页数组不完整）。

    断言的改动：get_page 对短读返回部分/零页。
    """
    _make_two_pages(table_path)

    _expect_storage_error(lambda: pool.get_page(table_path, 99))


# ---- 淘汰 / 写回 / flush / discard（slice 2）----


@pytest.fixture
def small_pool() -> BufferPool:
    return BufferPool(capacity=2)


def _make_pages(table_path: Path, count: int) -> None:
    """建表文件并追加 count 个数据页（页 1..count）。"""
    create_table_file(table_path)
    tmp_pool = BufferPool(capacity=4)
    for _ in range(count):
        alloc_page(tmp_pool, table_path)


def _page_bytes(table_path: Path, page_no: int) -> bytes:
    raw = table_path.read_bytes()
    return raw[page_no * PAGE_SIZE : (page_no + 1) * PAGE_SIZE]


def test_mark_dirty_then_flush_writes_page_to_disk(small_pool, table_path):
    """标脏后 flush(表文件) 必须把内存页写回磁盘并置 clean。

    断言的改动：flush 漏写脏页、写回后仍算脏（下次 flush 又 dirty_writes++）。
    """
    _make_pages(table_path, 2)
    frame = small_pool.get_page(table_path, 1)
    frame[0:8] = b"MARKED!!"
    small_pool.mark_dirty(table_path, 1)
    small_pool.unpin_page(table_path, 1)

    small_pool.flush(table_path)

    assert _page_bytes(table_path, 1)[:8] == b"MARKED!!"
    assert small_pool.stats["dirty_writes"] == 1

    small_pool.flush(table_path)
    assert small_pool.stats["dirty_writes"] == 1  # clean 帧不重复写


def test_flush_without_path_writes_all_dirty_frames(small_pool, tmp_path):
    """flush() 不带参数 = 全池脏帧写回（供需要整池落盘的场景）。

    断言的改动：flush(None) 只写默认表或什么都不写。
    """
    first = tmp_path / "a.table"
    second = tmp_path / "b.table"
    _make_pages(first, 1)
    _make_pages(second, 1)
    frame_a = small_pool.get_page(first, 1)
    frame_a[0:4] = b"AAAA"
    small_pool.mark_dirty(first, 1)
    small_pool.unpin_page(first, 1)
    frame_b = small_pool.get_page(second, 1)
    frame_b[0:4] = b"BBBB"
    small_pool.mark_dirty(second, 1)
    small_pool.unpin_page(second, 1)

    small_pool.flush()

    assert _page_bytes(first, 1)[:4] == b"AAAA"
    assert _page_bytes(second, 1)[:4] == b"BBBB"
    assert small_pool.stats["dirty_writes"] == 2


def test_lru_eviction_victims_least_recently_used(small_pool, table_path):
    """容量满时按 LRU 淘汰：命中把页移到末尾，最久未用先被换出。

    断言的改动：淘汰 FIFO/随机页、或命中不更新访问序。
    """
    _make_pages(table_path, 3)
    small_pool.get_page(table_path, 1)
    small_pool.unpin_page(table_path, 1)
    small_pool.get_page(table_path, 2)
    small_pool.unpin_page(table_path, 2)
    small_pool.get_page(table_path, 1)  # 命中 1，访问序变为 2,1
    small_pool.unpin_page(table_path, 1)
    small_pool.get_page(table_path, 3)  # 满 → 应淘汰 2
    small_pool.unpin_page(table_path, 3)

    # 1 仍在缓存（命中），2 已被换出（再取是 miss，且会再淘汰 3）
    small_pool.get_page(table_path, 1)
    small_pool.unpin_page(table_path, 1)
    small_pool.get_page(table_path, 2)
    small_pool.unpin_page(table_path, 2)
    assert small_pool.stats["misses"] == 4
    assert small_pool.stats["hits"] == 2
    assert small_pool.stats["evictions"] == 2


def test_dirty_frame_is_written_back_when_evicted(small_pool, table_path):
    """脏帧被 LRU 淘汰前必须先写回磁盘（dirty_writes++），clean 帧不写。

    断言的改动：淘汰时丢脏帧不落盘（重启丢数据）。
    """
    _make_pages(table_path, 3)
    frame = small_pool.get_page(table_path, 1)
    frame[0:8] = b"DIRTY!!!"
    small_pool.mark_dirty(table_path, 1)
    small_pool.unpin_page(table_path, 1)
    small_pool.get_page(table_path, 2)
    small_pool.unpin_page(table_path, 2)

    small_pool.get_page(table_path, 3)  # 淘汰脏页 1
    small_pool.unpin_page(table_path, 3)

    assert small_pool.stats["evictions"] == 1
    assert small_pool.stats["dirty_writes"] == 1
    assert _page_bytes(table_path, 1)[:8] == b"DIRTY!!!"


def test_pinned_frame_blocks_eviction_until_unpinned(small_pool, table_path):
    """pin>0 的帧不可淘汰；全部帧都被 pin 时再取新页 → E_STORAGE。

    断言的改动：LRU 无视 pin 把正被使用的帧换出。
    """
    _make_pages(table_path, 3)
    small_pool.get_page(table_path, 1)
    small_pool.get_page(table_path, 2)

    _expect_storage_error(lambda: small_pool.get_page(table_path, 3))

    small_pool.unpin_page(table_path, 1)  # 放开一帧后就能腾位置
    frame = small_pool.get_page(table_path, 3)
    try:
        assert bytes(frame) == _page_bytes(table_path, 3)
        assert small_pool.stats["evictions"] == 1
    finally:
        small_pool.unpin_page(table_path, 2)
        small_pool.unpin_page(table_path, 3)


def test_discard_file_drops_dirty_frames_without_writeback(small_pool, table_path):
    """discard(表文件) 直接丢帧不写回；文件保持旧内容，flush 也不再写。

    断言的改动：discard 把脏帧先 flush（删文件后回写出错）或留帧不删。
    """
    _make_pages(table_path, 1)
    frame = small_pool.get_page(table_path, 1)
    frame[0:8] = b"LOST????"
    small_pool.mark_dirty(table_path, 1)
    small_pool.unpin_page(table_path, 1)

    small_pool.discard(table_path)
    small_pool.flush(table_path)

    assert _page_bytes(table_path, 1)[:8] != b"LOST????"
    assert small_pool.stats["dirty_writes"] == 0
    assert small_pool.stats["misses"] == 1

    refetched = small_pool.get_page(table_path, 1)
    try:
        assert bytes(refetched) == _page_bytes(table_path, 1)
    finally:
        small_pool.unpin_page(table_path, 1)
    assert small_pool.stats["misses"] == 2


def test_discard_directory_drops_all_frames_under_it(small_pool, tmp_path):
    """discard(库目录) 丢弃该目录下所有表的帧（drop_database 前调用）。

    断言的改动：discard 只认精确表文件，删库后脏帧仍在内存里。
    """
    first = tmp_path / "main" / "a.table"
    second = tmp_path / "main" / "b.table"
    first.parent.mkdir()
    _make_pages(first, 1)
    _make_pages(second, 1)
    for path in (first, second):
        frame = small_pool.get_page(path, 1)
        frame[0:4] = b"XXXX"
        small_pool.mark_dirty(path, 1)
        small_pool.unpin_page(path, 1)

    small_pool.discard(tmp_path / "main")

    assert small_pool.stats["dirty_writes"] == 0
    assert _page_bytes(first, 1)[:4] != b"XXXX"
    assert _page_bytes(second, 1)[:4] != b"XXXX"
