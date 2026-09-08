"""损坏矩阵 T7（溢出部分）：改坏链页/锚点后 scan 必须 E_STORAGE。

改坏后一律新建 DatabaseServer 重连（绕过旧缓存帧），从磁盘读损坏数据。
"""

from __future__ import annotations

import struct
from pathlib import Path

import pytest

from contracts.ast import ColumnDef, SqlType
from contracts.errors import E_STORAGE, SqlError
from storage import DatabaseServer
from storage.constants import (
    OVERFLOW_ANCHOR_TOTAL_LEN_OFFSET,
    OVERFLOW_MAGIC,
    PAGE_SIZE,
    SLOT_OVERFLOW_FLAG,
    TABLE_FILE_SUFFIX,
)


def _columns() -> tuple[ColumnDef]:
    return (ColumnDef("body", SqlType.TEXT),)


@pytest.fixture
def data_dir(tmp_path) -> str:
    return str(tmp_path / "data")


def _path(data_dir: str) -> Path:
    return Path(data_dir) / "main" / f"notes{TABLE_FILE_SUFFIX}"


def _prepare_overflow_row(data_dir: str) -> None:
    """建表并插入一条 9000B 溢出行，方法返回前已 flush。"""
    storage = DatabaseServer(data_dir).connect("main")
    storage.create_table("notes", _columns())
    storage.insert("notes", ("z" * 9000,))


def _chain_page_no(path: Path) -> int:
    raw = path.read_bytes()
    page_count = len(raw) // PAGE_SIZE
    for page_no in range(1, page_count):
        start = page_no * PAGE_SIZE
        if raw[start : start + 4] == OVERFLOW_MAGIC:
            return page_no
    raise AssertionError("no overflow chain page found")


def _anchor_page_and_offset(path: Path) -> tuple[int, int]:
    """找到溢出锚点所在数据页与记录偏移（用于改坏 total_len）。"""
    raw = path.read_bytes()
    page_count = len(raw) // PAGE_SIZE
    for page_no in range(1, page_count):
        start = page_no * PAGE_SIZE
        page = raw[start : start + PAGE_SIZE]
        if page[:4] == OVERFLOW_MAGIC:
            continue
        slot_count = struct.unpack_from("<H", page, 0)[0]
        free_ptr = struct.unpack_from("<I", page, 4)[0]
        for i in range(slot_count):
            slot_pos = PAGE_SIZE - 8 * (i + 1)
            record_offset, raw_length = struct.unpack_from("<II", page, slot_pos)
            if raw_length & SLOT_OVERFLOW_FLAG:
                length = raw_length & ~SLOT_OVERFLOW_FLAG
                if record_offset + length > free_ptr:
                    raise AssertionError("anchor slot out of range")
                return page_no, record_offset
    raise AssertionError("no overflow anchor found")


def _corrupt_bytes(path: Path, page_no: int, offset: int, new: bytes) -> None:
    raw = bytearray(path.read_bytes())
    pos = page_no * PAGE_SIZE + offset
    raw[pos : pos + len(new)] = new
    path.write_bytes(raw)


def _expect_scan_storage_error(data_dir: str) -> None:
    storage = DatabaseServer(data_dir).connect("main")
    with pytest.raises(SqlError) as exc:
        list(storage.scan("notes"))
    assert exc.value.code == E_STORAGE


def test_corrupt_chain_magic_raises_storage(data_dir):
    """溢出页 magic 被改坏 → scan 沿链校验失败 E_STORAGE。

    断言的改动：scan 不校验链页 magic，把垃圾当 payload 拼出来。
    """
    _prepare_overflow_row(data_dir)
    path = _path(data_dir)
    page_no = _chain_page_no(path)
    _corrupt_bytes(path, page_no, 0, b"XXXX")

    _expect_scan_storage_error(data_dir)


def test_corrupt_chain_total_len_raises_storage(data_dir):
    """链页 total_len 与锚点不符 → E_STORAGE（防拼错行）。

    断言的改动：collect 忽略 total 一致性，拿不完整行解码。
    """
    _prepare_overflow_row(data_dir)
    path = _path(data_dir)
    page_no = _chain_page_no(path)
    _corrupt_bytes(path, page_no, 8, (100).to_bytes(8, "little"))

    _expect_scan_storage_error(data_dir)


def test_corrupt_chain_next_self_loop_raises_storage(data_dir):
    """链页 next 指向自己 → scan E_STORAGE（防无限循环）。

    断言的改动：collect 无环检测，scan 死循环/超时。
    """
    _prepare_overflow_row(data_dir)
    path = _path(data_dir)
    page_no = _chain_page_no(path)
    _corrupt_bytes(path, page_no, 4, page_no.to_bytes(4, "little"))

    _expect_scan_storage_error(data_dir)


def test_corrupt_anchor_total_len_raises_storage(data_dir):
    """锚点 total_len 改坏（≤inline 上限）→ scan E_STORAGE。

    断言的改动：锚点字段不校验，按 4080 内小记录硬解。
    """
    _prepare_overflow_row(data_dir)
    path = _path(data_dir)
    page_no, record_offset = _anchor_page_and_offset(path)
    _corrupt_bytes(
        path,
        page_no,
        record_offset + OVERFLOW_ANCHOR_TOTAL_LEN_OFFSET,
        (1).to_bytes(4, "little"),
    )

    _expect_scan_storage_error(data_dir)
