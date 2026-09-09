"""TableEngine M4 测试：空闲页回收（D05/D06）与 engine 对空闲页的感知。

删空页必须进 free list；scan/找页/定位必须跳过空闲页（其前 4 B 是 next，
不再是合法 slotted 头）。测试直接驱动单表引擎 + 真实 BufferPool。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from contracts.ast import ColumnDef, SqlType
from storage.cache import BufferPool
from storage.engine import TableEngine
from storage.pager import create_table_file, free_pages, page_count


def _columns() -> tuple[ColumnDef, ColumnDef]:
    return (ColumnDef("id", SqlType.INT), ColumnDef("tag", SqlType.TEXT))


@pytest.fixture
def pool() -> BufferPool:
    return BufferPool(capacity=16)


@pytest.fixture
def table_path(tmp_path) -> Path:
    return tmp_path / "users.table"


def _make_engine(table_path: Path, pool: BufferPool) -> TableEngine:
    return TableEngine(table_path, _columns(), pool)


def _scan_sorted(engine: TableEngine):
    return sorted(engine.scan(), key=lambda row: row[0])


def _insert_many(engine: TableEngine, count: int) -> None:
    for i in range(1, count + 1):
        engine.insert((i, f"tag-{i}"))


def test_delete_last_row_on_single_page_frees_the_page(
    engine_factory, table_path, pool
):
    """删掉页内唯一行后，该页必须进入 free list（不再留在活动页里）。

    断言的改动：delete 清空页后不调 free_page（还当普通空页遍历）。
    """
    engine = engine_factory()

    engine.insert((1, "a"))
    engine.delete(1)

    assert free_pages(pool, table_path) == [1]
    assert _scan_sorted(engine) == []


def test_insert_reuses_freed_page_without_growing_file(
    engine_factory, table_path, pool
):
    """free list 有页时 insert 先弹回该页，文件长度不变（D05/D06）。

    断言的改动：insert 无视 free list 又 alloc 追加一页。
    """
    engine = engine_factory()
    engine.insert((1, "a"))
    engine.delete(1)
    pages_before = page_count(pool, table_path)

    assert engine.insert((2, "b")) == 2

    assert page_count(pool, table_path) == pages_before
    assert free_pages(pool, table_path) == []
    assert _scan_sorted(engine) == [(2, (2, "b"))]


def test_scan_skips_free_pages_with_next_pointer_bytes(
    engine_factory, table_path, pool
):
    """部分页在 free list 时 scan 必须跳过它们，不得把 next 当页头解析。

    断言的改动：scan 仍遍历 1..N-1 全部页，读到空闲页就 E_STORAGE。
    """
    engine = engine_factory()
    _insert_many(engine, 200)
    rows = _scan_sorted(engine)
    for row_id, _values in rows:
        engine.delete(row_id)
    # 全部页已 free；再插一条 → 弹出最新空闲页，其余空闲页仍在链上
    assert free_pages(pool, table_path)
    pages_before = page_count(pool, table_path)

    new_id = engine.insert((201, "fresh"))

    assert page_count(pool, table_path) == pages_before
    assert _scan_sorted(engine) == [(201, (201, "fresh"))]
    assert free_pages(pool, table_path)  # 仍有别的空闲页待复用
    assert new_id == 201


def test_free_list_and_counter_persist_across_engine_restart(
    engine_factory, table_path, pool
):
    """重启后 free list 与 next_row_id 都从磁盘恢复：复用旧页、不发重复 id。

    断言的改动：free list 只存内存、重启后丢失（scan 读空闲页报损坏）。
    """
    engine = engine_factory()
    _insert_many(engine, 200)
    for row_id, _values in _scan_sorted(engine):
        engine.delete(row_id)
    pool.flush(table_path)

    fresh = _make_engine(table_path, BufferPool(capacity=8))
    assert _scan_sorted(fresh) == []
    pages_before = page_count(pool, table_path)

    assert fresh.insert((201, "new")) == 201

    assert page_count(pool, table_path) == pages_before
    assert _scan_sorted(fresh) == [(201, (201, "new"))]


# ---- 测试工具：从引擎取内部池/路径，保持用例简短 ----


@pytest.fixture
def engine_factory(table_path, pool):
    create_table_file(table_path)

    def build() -> TableEngine:
        return _make_engine(table_path, pool)

    return build
