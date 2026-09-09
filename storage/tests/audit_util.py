"""测试专用物理审计工具（不进生产代码）。

audit_table_file() 对单个 .table 文件做整体体检：
页0 合法、长度整页、free list 无环、无未引用的 OVFL 页、活动页可完整解码、
row_id 唯一、锚点链完整。任何违规抛 AssertionError（带原因）。
"""

from __future__ import annotations

import struct
from pathlib import Path
from typing import Sequence

from contracts.ast import ColumnDef
from storage.constants import (
    INLINE_RECORD_LIMIT,
    OVERFLOW_HEADER_SIZE,
    OVERFLOW_MAGIC,
    PAGE0_FREE_HEAD_OFFSET,
    PAGE_SIZE,
    SLOT_OVERFLOW_FLAG,
)
from storage.engine import decode_record, _page_slot_entries, _parse_page_header


_ANCHOR = struct.Struct("<QII")
_CHAIN_HEADER = struct.Struct("<4sIQ")


def _fail(reason: str) -> None:
    raise AssertionError(f"table audit failed: {reason}")


def audit_table_file(
    path: Path, columns: Sequence[ColumnDef]
) -> dict[str, int | list[int]]:
    """体检表文件；返回 {pages, rows, free_pages} 摘要。"""
    raw = path.read_bytes()
    if len(raw) < PAGE_SIZE or len(raw) % PAGE_SIZE != 0:
        _fail(f"{path}: length {len(raw)} not page-aligned")
    total_pages = len(raw) // PAGE_SIZE
    if raw[0:4] != b"HSQL":
        _fail(f"{path}: bad magic")
    if int.from_bytes(raw[4:6], "little") != 1:
        _fail(f"{path}: bad version")

    def page_bytes(page_no: int) -> bytes:
        start = page_no * PAGE_SIZE
        return raw[start : start + PAGE_SIZE]

    # free list
    free_set: set[int] = set()
    free_order: list[int] = []
    head = int.from_bytes(raw[PAGE0_FREE_HEAD_OFFSET : PAGE0_FREE_HEAD_OFFSET + 4], "little")
    while head != 0:
        if head in free_set or not 0 < head < total_pages:
            _fail(f"{path}: corrupt free list at {head}")
        free_set.add(head)
        free_order.append(head)
        head = int.from_bytes(page_bytes(head)[0:4], "little")

    overflow_pages: set[int] = set()
    for page_no in range(1, total_pages):
        if page_bytes(page_no)[:4] == OVERFLOW_MAGIC:
            overflow_pages.add(page_no)

    referenced_overflow: set[int] = set()
    seen_rids: set[int] = set()
    rows = 0
    active_data_pages = 0

    for page_no in range(1, total_pages):
        if page_no in free_set or page_no in overflow_pages:
            continue
        page = bytearray(page_bytes(page_no))
        _parse_page_header(page)  # 非法 slotted 头直接抛
        active_data_pages += 1
        for record_offset, record_length, is_overflow in _page_slot_entries(page):
            if is_overflow:
                anchor = bytes(page[record_offset : record_offset + record_length])
                if len(anchor) != _ANCHOR.size:
                    _fail(f"{path}: bad anchor length on page {page_no}")
                row_id, first_page, total_len = _ANCHOR.unpack(anchor)
                if total_len <= INLINE_RECORD_LIMIT or first_page <= 0:
                    _fail(f"{path}: bad anchor fields on page {page_no}")
                chain: list[int] = []
                chain_set: set[int] = set()
                cur = first_page
                while cur != 0:
                    if cur in chain_set or not 0 < cur < total_pages:
                        _fail(f"{path}: overflow chain cycle/out-of-range")
                    if cur in referenced_overflow:
                        _fail(f"{path}: overflow page shared by two anchors")
                    chain_set.add(cur)
                    chain.append(cur)
                    p = page_bytes(cur)
                    magic, nxt, chain_total = _CHAIN_HEADER.unpack_from(p, 0)
                    if magic != OVERFLOW_MAGIC or chain_total != total_len:
                        _fail(f"{path}: overflow page header mismatch")
                    cur = nxt
                if len(chain) * (PAGE_SIZE - OVERFLOW_HEADER_SIZE) < total_len:
                    _fail(f"{path}: overflow chain too short")
                chunks = []
                for i, cp in enumerate(chain):
                    p = page_bytes(cp)
                    take = min(
                        PAGE_SIZE - OVERFLOW_HEADER_SIZE,
                        total_len - i * (PAGE_SIZE - OVERFLOW_HEADER_SIZE),
                    )
                    chunks.append(p[OVERFLOW_HEADER_SIZE : OVERFLOW_HEADER_SIZE + take])
                decoded = decode_record(b"".join(chunks), columns)
                if decoded[0] != row_id:
                    _fail(f"{path}: anchor row_id mismatch on page {page_no}")
                referenced_overflow.update(chain)
            else:
                record = bytes(page[record_offset : record_offset + record_length])
                decoded = decode_record(record, columns)
            if decoded[0] in seen_rids:
                _fail(f"{path}: duplicate row_id {decoded[0]}")
            seen_rids.add(decoded[0])
            rows += 1

    orphan_overflow = overflow_pages - referenced_overflow
    if orphan_overflow:
        _fail(f"{path}: unreferenced overflow pages {sorted(orphan_overflow)}")
    return {
        "pages": total_pages,
        "rows": rows,
        "free_pages": free_order,
        "active_data_pages": active_data_pages,
        "overflow_pages": len(overflow_pages),
    }
