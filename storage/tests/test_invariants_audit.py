"""物理不变式审计 + 文件增长口径测试（D06/第③点口径）。"""

from __future__ import annotations

from pathlib import Path

import pytest

from contracts.ast import ColumnDef, SqlType
from storage import DatabaseServer
from storage.constants import TABLE_FILE_SUFFIX
from audit_util import audit_table_file


def _columns() -> tuple[ColumnDef, ColumnDef]:
    return (ColumnDef("id", SqlType.INT), ColumnDef("body", SqlType.TEXT))


@pytest.fixture
def data_dir(tmp_path) -> str:
    return str(tmp_path / "data")


def _path(data_dir: str, name: str = "users") -> Path:
    return Path(data_dir) / "main" / f"{name}{TABLE_FILE_SUFFIX}"


def _make_server_storage(data_dir: str):
    server = DatabaseServer(data_dir)
    storage = server.connect("main")
    storage.create_table("users", _columns())
    return storage


def test_audit_passes_after_mixed_inline_overflow_churn(data_dir):
    """inline/溢出/删除/更新混战后，物理审计仍全绿。"""
    storage = _make_server_storage(data_dir)
    ids = []
    for i in range(1, 151):
        ids.append(storage.insert("users", (i, "x" * (i % 30))))
    overflow_id = storage.insert("users", (200, "o" * 9000))  # 溢出行
    for rid in ids:
        if rid % 2 == 1:
            storage.delete_row("users", rid)
    storage.update_row("users", 4, (4, "s" * 7000))
    storage.update_row("users", overflow_id, (200, "tiny"))
    storage.delete_row("users", 6)
    storage.delete_row("users", 4)  # 清掉最后一个溢出行，链页应全部回收

    summary = audit_table_file(_path(data_dir), _columns())

    assert summary["rows"] > 0
    assert summary["pages"] >= 2
    assert summary["overflow_pages"] == 0  # 上一步把唯一溢出行缩回 inline


def test_file_never_shrinks_across_churn(data_dir):
    """D06：任意增删后文件大小单调不下降（收缩是缺陷）。"""
    storage = _make_server_storage(data_dir)
    last = _path(data_dir).stat().st_size
    for i in range(1, 101):
        storage.insert("users", (i, f"tag-{i}"))
    assert _path(data_dir).stat().st_size >= last
    last = _path(data_dir).stat().st_size
    for rid in list(range(1, 101)):
        storage.delete_row("users", rid)
    assert _path(data_dir).stat().st_size >= last


def test_identical_cycles_stay_at_stable_size(data_dir):
    """同构 fill→delete 循环：首轮定稳定尺寸，后续循环不得再增长。"""
    storage = _make_server_storage(data_dir)

    def run_cycle():
        ids = []
        for i in range(1, 81):
            ids.append(storage.insert("users", (i, "v" * 80)))
        for rid in ids:
            storage.delete_row("users", rid)
        return _path(data_dir).stat().st_size

    first = run_cycle()
    for _ in range(3):
        assert run_cycle() == first


def test_smaller_refill_never_exceeds_historical_peak(data_dir):
    """删光后再插更少数据，文件不得超过历史峰值（空闲页必须被复用）。"""
    storage = _make_server_storage(data_dir)
    for i in range(1, 121):
        storage.insert("users", (i, "p" * 100))
    peak = _path(data_dir).stat().st_size
    for rid in list(range(1, 121)):
        storage.delete_row("users", rid)

    for i in range(1, 61):
        storage.insert("users", (i, "p" * 100))

    assert _path(data_dir).stat().st_size <= peak
