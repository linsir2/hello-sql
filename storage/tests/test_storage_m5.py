"""Storage M5 重启矩阵（T4）：溢出行在 insert/update/delete 每步后重开可读。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from contracts.ast import ColumnDef, SqlType
from storage import DatabaseServer
from storage.constants import TABLE_FILE_SUFFIX


def _columns() -> tuple[ColumnDef]:
    return (ColumnDef("body", SqlType.TEXT),)


@pytest.fixture
def data_dir(tmp_path) -> str:
    return str(tmp_path / "data")


def _path(data_dir: str) -> Path:
    return Path(data_dir) / "main" / f"notes{TABLE_FILE_SUFFIX}"


def _make_storage(data_dir: str):
    return DatabaseServer(data_dir).connect("main")


def test_overflow_crud_survives_restart_at_every_step(data_dir):
    """insert→update(overflow→inline→overflow)→delete 每步重开都完整可读。

    断言的改动：任何一步的溢出链没持久化，重开后 scan 丢数据/报损坏。
    """
    storage = _make_storage(data_dir)
    storage.create_table("notes", _columns())
    huge = "x" * 9000
    assert storage.insert("notes", (huge,)) == 1

    assert list(_make_storage(data_dir).scan("notes")) == [(1, (huge,))]

    _make_storage(data_dir).update_row("notes", 1, ("short",))
    assert list(_make_storage(data_dir).scan("notes")) == [(1, ("short",))]

    _make_storage(data_dir).update_row("notes", 1, (huge,))
    assert list(_make_storage(data_dir).scan("notes")) == [(1, (huge,))]

    _make_storage(data_dir).delete_row("notes", 1)
    assert list(_make_storage(data_dir).scan("notes")) == []


def test_overflow_delete_then_restart_reuses_pages_without_growth(data_dir):
    """删除溢出后重启：空闲链页持久化，再插同长行文件不增长、row_id 不复用。

    断言的改动：free list/页0 计数只存内存，重启后丢失。
    """
    storage = _make_storage(data_dir)
    storage.create_table("notes", _columns())
    huge = "y" * 9000
    storage.insert("notes", (huge,))
    storage.delete_row("notes", 1)
    size_before = _path(data_dir).stat().st_size

    fresh = _make_storage(data_dir)
    assert list(fresh.scan("notes")) == []
    assert fresh.insert("notes", (huge,)) == 2

    assert _path(data_dir).stat().st_size == size_before
    assert list(fresh.scan("notes")) == [(2, (huge,))]
