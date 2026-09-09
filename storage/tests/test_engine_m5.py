"""TableEngine M5 测试：溢出页链（D14）——超长行跨页存储、scan 还原、
update/delete 沿链回收。M2 的“超长行报 E_STORAGE”占位将被这些用例替换。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from contracts.ast import ColumnDef, SqlType
from storage.cache import BufferPool
from storage.engine import TableEngine
from storage.pager import create_table_file, free_pages, page_count


def _text_columns() -> tuple[ColumnDef]:
    return (ColumnDef("body", SqlType.TEXT),)


@pytest.fixture
def pool() -> BufferPool:
    return BufferPool(capacity=32)


@pytest.fixture
def table_path(tmp_path) -> Path:
    return tmp_path / "notes.table"


@pytest.fixture
def engine(table_path, pool) -> TableEngine:
    create_table_file(table_path)
    return TableEngine(table_path, _text_columns(), pool)


def test_overflow_insert_and_scan_roundtrip(engine):
    """编码超 4080B 的行必须成功落库，scan 还原完整内容（替代 M2 的报错占位）。

    断言的改动：insert 仍抛“overflow in M5”、或 scan 丢尾部字节。
    """
    body = "x" * 5000

    assert engine.insert((body,)) == 1
    assert list(engine.scan()) == [(1, (body,))]


def test_overflow_row_spans_chain_pages_and_scan_still_works(
    engine, table_path, pool
):
    """5000B 文本需要 2 个溢出页，scan 必须跳过链页并拼回完整记录。

    断言的改动：把所有页当数据页扫（读到 OVFL 头就 E_STORAGE）。
    """
    body = "y" * 9000

    assert engine.insert((body,)) == 1

    total_pages = page_count(pool, table_path)
    assert total_pages >= 4  # 页0 + 锚点数据页 + ≥2 个溢出页
    assert free_pages(pool, table_path) == []
    assert list(engine.scan()) == [(1, (body,))]


def test_multiple_overflow_rows_scan_all(engine):
    """多条超长行各建各的链，scan 必须互不串行地全部还原。

    断言的改动：锚点/链串行（读 A 行拼到 B 行 payload）。
    """
    body_a = "a" * 5000
    body_b = "b" * 9000

    assert engine.insert((body_a,)) == 1
    assert engine.insert((body_b,)) == 2

    rows = sorted(engine.scan(), key=lambda row: row[0])
    assert rows == [(1, (body_a,)), (2, (body_b,))]


def test_update_overflow_to_longer_overflow_frees_old_chain(engine, table_path, pool):
    """溢出行整行更新为更长溢出行：row_id 不变、旧链被回收、scan 只有新值。

    断言的改动：update 后旧链页残留（被 scan 当数据页/占空间不释放）。
    """
    body_old = "a" * 5000
    body_new = "b" * 12000
    engine.insert((body_old,))
    pages_before = page_count(pool, table_path)

    engine.update(1, (body_new,))

    assert list(engine.scan()) == [(1, (body_new,))]
    # 旧链回收后立刻被新链复用：文件只比原来多 1 页（新链多一页）
    assert page_count(pool, table_path) == pages_before + 1


def test_update_overflow_to_inline_frees_chain(engine, table_path, pool):
    """溢出 → inline 更新：旧链全部回收，scan 返回短值。

    断言的改动：update 缩行后链页仍留在活动页（scan 报损坏）。
    """
    engine.insert(("a" * 9000,))

    engine.update(1, ("short",))

    assert list(engine.scan()) == [(1, ("short",))]
    assert free_pages(pool, table_path)


def test_update_inline_to_overflow_creates_chain(engine):
    """inline → 溢出更新：短行长大成超长行后仍可完整 scan。

    断言的改动：update 长大时按 inline 硬塞/截断。
    """
    engine.insert(("short",))

    body = "c" * 7000
    engine.update(1, (body,))

    assert list(engine.scan()) == [(1, (body,))]


def test_delete_overflow_frees_chain_and_pages_reusable(
    engine, table_path, pool
):
    """删除溢出行：数据页与全部链页进 free list；再插同长行文件不再增长。

    断言的改动：delete 漏 free 链页，文件持续增长。
    """
    body = "d" * 9000
    engine.insert((body,))
    pages_after_insert = page_count(pool, table_path)

    engine.delete(1)

    assert list(engine.scan()) == []
    assert free_pages(pool, table_path)

    assert engine.insert((body,)) == 2
    assert page_count(pool, table_path) == pages_after_insert
    assert list(engine.scan()) == [(2, (body,))]
