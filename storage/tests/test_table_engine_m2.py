"""TableEngine M2 测试：行级 insert/scan/update/delete、row_id、rid→页映射。

测试直接操作单表引擎（不经 Storage 门面），构造真实表文件 + BufferPool；
公开方法的边界/类型校验在 test_storage_m2.py 里测。
命名规则沿用前几阶段：写清“会让它失败的生产改动”。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from contracts.ast import ColumnDef, SqlType
from contracts.errors import E_ROW_NOT_FOUND, SqlError
from storage.cache import BufferPool
from storage.engine import TableEngine
from storage.pager import create_table_file, page_count


def _columns() -> tuple[ColumnDef, ColumnDef]:
    return (ColumnDef("id", SqlType.INT), ColumnDef("tag", SqlType.TEXT))


@pytest.fixture
def pool() -> BufferPool:
    return BufferPool(capacity=8)


@pytest.fixture
def table_path(tmp_path) -> Path:
    return tmp_path / "users.table"


def _make_engine(table_path: Path, pool: BufferPool) -> TableEngine:
    return TableEngine(table_path, _columns(), pool)


@pytest.fixture
def engine(table_path, pool) -> TableEngine:
    create_table_file(table_path)
    return _make_engine(table_path, pool)


def _scan_sorted(engine: TableEngine):
    return sorted(engine.scan(), key=lambda row: row[0])


def _page0_next_row_id(table_path: Path) -> int:
    return int.from_bytes(table_path.read_bytes()[8:16], "little")


# ---- insert 与 row_id ----


def test_insert_returns_increasing_ids_and_persists_counter(engine, table_path):
    """连续 insert 返回 1、2、3，且页 0 next_row_id 持久化为下一个可用号。

    断言的改动：row_id 从 0 开始/复用、或页 0 计数没跟着 +1。
    """
    assert engine.insert((10, "a")) == 1
    assert engine.insert((20, "b")) == 2
    assert engine.insert((30, "c")) == 3

    assert _page0_next_row_id(table_path) == 4
    assert _scan_sorted(engine) == [
        (1, (10, "a")),
        (2, (20, "b")),
        (3, (30, "c")),
    ]


def test_delete_does_not_reuse_row_id(engine):
    """删除后 row_id 不复用：下一次 insert 必须拿到更大的号（D08）。

    断言的改动：engine 把空页/空闲槽的 row_id 拿回来重新发。
    """
    assert engine.insert((1, "a")) == 1
    engine.delete(1)

    assert engine.insert((2, "b")) == 2
    assert _scan_sorted(engine) == [(2, (2, "b"))]


def test_insert_many_rows_spans_pages_and_scan_returns_all(
    engine, table_path, pool
):
    """行数超过单页容量时必须自动分配新页，scan 仍返回全部行。

    断言的改动：页满时不扩页丢数据、或 scan 漏页。
    """
    for i in range(1, 201):
        engine.insert((i, f"tag-{i}"))

    assert page_count(pool, table_path) > 1
    rows = _scan_sorted(engine)
    assert [row[0] for row in rows] == list(range(1, 201))


# ---- 跨“连接”定位（D08：映射随 Storage 实例存在，重启后靠全表找）----


def test_new_engine_can_delete_and_update_rows_from_previous_run(
    engine, table_path, pool
):
    """换一个全新 TableEngine（模拟重启，映射为空）仍能按 row_id 定位。

    断言的改动：定位只信内存映射、不退化全表找，重启后 update/delete 全断。
    """
    engine.insert((1, "a"))
    engine.insert((2, "b"))
    engine.insert((3, "c"))

    fresh = _make_engine(table_path, pool)
    fresh.update(2, (20, "B"))
    fresh.delete(3)

    rows = _scan_sorted(fresh)
    assert rows == [(1, (1, "a")), (2, (20, "B"))]


def test_scan_rebuilds_mapping_so_update_works_without_full_scan_twice(engine):
    """scan 必须重建 rid→页 映射（§8.3），之后 update 走映射命中。

    断言的改动：scan 只 yield 不建映射，update 每次都退化全表扫。
    """
    engine.insert((1, "a"))
    list(engine.scan())

    engine.update(1, (10, "A"))

    assert _scan_sorted(engine) == [(1, (10, "A"))]


def test_update_and_delete_missing_row_id_raise_row_not_found(engine):
    """不存在的 row_id（含从未发过的号）update/delete 抛 E_ROW_NOT_FOUND。

    断言的改动：找不到行时静默成功或抛别的码。
    """
    with pytest.raises(SqlError) as exc:
        engine.update(99, (1, "x"))
    assert exc.value.code == E_ROW_NOT_FOUND

    with pytest.raises(SqlError) as exc:
        engine.delete(99)
    assert exc.value.code == E_ROW_NOT_FOUND


# ---- 页内空间行为 ----


def test_delete_all_rows_leaves_reusable_page_without_growing_file(
    engine, table_path, pool
):
    """删空整页后页面保留且可再次插入，文件不继续增长（M2 空页复用）。

    断言的改动：delete 后引擎忘了空页又 alloc 新页，或 M2 就调 free_page。
    """
    engine.insert((1, "a"))
    before = page_count(pool, table_path)

    engine.delete(1)
    assert engine.insert((2, "b")) == 2

    assert page_count(pool, table_path) == before
    assert _scan_sorted(engine) == [(2, (2, "b"))]


def test_update_that_does_not_fit_current_page_moves_to_new_page(
    engine, table_path, pool
):
    """新记录塞不进原页时：删旧行 → 分配新页放入，行身份不变。

    断言的改动：update 放不下时丢行、报错，或行被复制成两份。
    """
    engine.insert((1, "a"))
    engine.insert((2, "b"))
    pages_before = page_count(pool, table_path)

    huge = "x" * 4060
    engine.update(1, (1, huge))

    rows = _scan_sorted(engine)
    row1 = [row for row in rows if row[0] == 1][0]
    assert row1 == (1, (1, huge))
    assert [row[0] for row in rows] == [1, 2]
    # 原页（含另一行）放不下 4080 B 新记录 → 必须新增一页搬走
    assert page_count(pool, table_path) == pages_before + 1
