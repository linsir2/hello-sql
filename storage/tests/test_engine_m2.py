"""engine M2 测试：记录编解码（PRD §8.1）与 slotted 页内布局（§8.2/D15）。

按数据流自底向上推进：先编解码字节，再页内增删/紧凑，最后才是公开方法。
命名规则沿用 M0/M1：每个测试写清“会让它失败的生产改动”。
期望字节全部手拼字面量；解码坏数据必须 E_STORAGE（§8.1）。
"""

from __future__ import annotations

import pytest

from contracts.ast import ColumnDef, SqlType
from contracts.errors import E_STORAGE, SqlError
from storage.constants import PAGE_SIZE
from storage.engine import (
    append_record,
    decode_record,
    encode_record,
    new_data_page,
    page_rows,
    remove_slot,
    replace_slot,
)


def _int_text_columns() -> tuple[ColumnDef, ColumnDef]:
    return (ColumnDef("id", SqlType.INT), ColumnDef("tag", SqlType.TEXT))


# ---- encode_record ----


def test_encode_record_writes_rid_then_int_len_prefixed_text():
    """一行 (row_id=3, id=7, tag="hi") 的字节必须精确等于手拼布局：
    u64 rid + q int + u32 len + utf8 文本。

    断言的改动：字段顺序、宽度、大小端、文本长度前缀任一项出错都会失败。
    """
    record = encode_record(3, _int_text_columns(), (7, "hi"))

    expected = (
        (3).to_bytes(8, "little")
        + (7).to_bytes(8, "little", signed=True)
        + (2).to_bytes(4, "little")
        + b"hi"
    )
    assert record == expected


def test_encode_record_text_uses_utf8_byte_length():
    """TEXT 的长度前缀必须是 UTF-8 字节数而非字符数（中文一字三字节）。

    断言的改动：用 len(str) 当字节长、或编码后不写回 UTF-8 字节。
    """
    columns = (ColumnDef("t", SqlType.TEXT),)

    record = encode_record(1, columns, ("中文",))

    assert record[8:12] == (6).to_bytes(4, "little")
    assert record[12:] == "中文".encode("utf-8")


def test_encode_decode_roundtrip_for_all_three_types():
    """INT/REAL/TEXT 混合列 encode 后 decode 必须还原为 (rid, 值元组)。

    断言的改动：任一类型编码/解码不对称（REAL 精度、INT 符号、TEXT 边界）。
    """
    columns = (
        ColumnDef("id", SqlType.INT),
        ColumnDef("name", SqlType.TEXT),
        ColumnDef("score", SqlType.REAL),
    )

    row = decode_record(encode_record(9, columns, (-5, "中文", 2.5)), columns)

    assert row == (9, (-5, "中文", 2.5))


# ---- decode_record ----


def test_decode_record_returns_int_and_float_native_types():
    """REAL 解码回来必须是 float（内部统一存 float，§2.2），INT 是 int。

    断言的改动：REAL 解成 int、或丢小数位。
    """
    columns = (
        ColumnDef("a", SqlType.INT),
        ColumnDef("b", SqlType.REAL),
    )

    row = decode_record(encode_record(2, columns, (1, 1.5)), columns)

    assert row == (2, (1, 1.5))
    assert type(row[1][0]) is int
    assert type(row[1][1]) is float


def test_decode_record_truncated_payload_raises_storage():
    """记录字节被截断（少一个 INT/长度前缀越界）必须 E_STORAGE。

    断言的改动：decode 静默返回半截值或抛 struct.error 裸异常。
    """
    good = encode_record(1, _int_text_columns(), (7, "hi"))

    with pytest.raises(SqlError) as exc:
        decode_record(good[:-1], _int_text_columns())

    assert exc.value.code == E_STORAGE


def test_decode_record_rejects_trailing_garbage():
    """记录后面多出不在列定义里的字节必须 E_STORAGE（长度账本不一致）。

    断言的改动：decode 忽略尾部多余字节。
    """
    good = encode_record(1, _int_text_columns(), (7, "hi"))

    with pytest.raises(SqlError) as exc:
        decode_record(good + b"junk", _int_text_columns())

    assert exc.value.code == E_STORAGE


def test_decode_record_rejects_invalid_utf8():
    """TEXT 字节不是合法 UTF-8 时必须 E_STORAGE。

    断言的改动：decode 用 errors="replace" 静默洗掉坏字节。
    """
    columns = (ColumnDef("t", SqlType.TEXT),)
    bad = (
        (1).to_bytes(8, "little")
        + (1).to_bytes(4, "little")
        + b"\xff"
    )

    with pytest.raises(SqlError) as exc:
        decode_record(bad, columns)

    assert exc.value.code == E_STORAGE


# ---- slotted page：页头 / 槽 / 追加 / 紧凑（§8.2、D15）----


def _int_column() -> tuple[ColumnDef]:
    return (ColumnDef("id", SqlType.INT),)


def test_new_data_page_starts_with_empty_header_and_zero_free_area():
    """新数据页页头 = slot_count 0 + flags 0 + free_ptr 8，其余字节全 0。

    断言的改动：页头初值不对（free_ptr 不是 8）、残留旧字节。
    """
    page = new_data_page()

    assert len(page) == PAGE_SIZE
    assert page[:2] == (0).to_bytes(2, "little")
    assert page[2:4] == (0).to_bytes(2, "little")
    assert page[4:8] == (8).to_bytes(4, "little")
    assert page[8:] == bytes(PAGE_SIZE - 8)


def test_append_record_writes_header_slot_and_contiguous_record():
    """追加一条记录：slot_count=1、free_ptr=8+len、槽(offset=8,len) 在页尾。

    断言的改动：记录没写进记录区、槽目录位置/内容错、free_ptr 不前进。
    """
    page = new_data_page()
    record = encode_record(1, _int_column(), (7,))

    assert append_record(page, record) is True

    assert page[:2] == (1).to_bytes(2, "little")
    assert page[4:8] == (8 + len(record)).to_bytes(4, "little")
    assert page[PAGE_SIZE - 8 : PAGE_SIZE - 4] == (8).to_bytes(4, "little")
    assert page[PAGE_SIZE - 4 :] == len(record).to_bytes(4, "little")
    assert page[8 : 8 + len(record)] == record
    assert page_rows(page, _int_column()) == [(1, (7,))]


def test_append_returns_false_when_no_room_and_leaves_page_unchanged():
    """页内空间不足时 append 返回 False，且页内容必须原样不动。

    断言的改动：append 失败时仍写半个记录/改页头（破坏单块连续空闲区不变式）。
    """
    page = new_data_page()
    record = encode_record(1, _int_column(), (7,))
    for _ in range(300):
        if not append_record(page, record):
            break

    snapshot = bytes(page)
    assert append_record(page, record) is False
    assert bytes(page) == snapshot


def test_remove_slot_compacts_records_and_keeps_single_free_block():
    """删中间槽后记录必须立即紧凑成单块连续区，不能留洞。

    断言的改动：remove 后不紧凑（中间留空洞）或 free_ptr 算错。
    """
    page = new_data_page()
    rec1 = encode_record(1, _int_column(), (1,))
    rec2 = encode_record(2, _int_column(), (2,))
    rec3 = encode_record(3, _int_column(), (3,))
    append_record(page, rec1)
    append_record(page, rec2)
    append_record(page, rec3)

    remove_slot(page, 1)

    rows = page_rows(page, _int_column())
    assert [row[0] for row in rows] == [1, 3]
    expected_free_ptr = 8 + len(rec1) + len(rec3)
    assert page[:2] == (2).to_bytes(2, "little")
    assert page[4:8] == expected_free_ptr.to_bytes(4, "little")
    # free_ptr 到新槽目录起点之间应全是 0（单块连续空闲区）
    assert page[expected_free_ptr : PAGE_SIZE - 16] == bytes(
        PAGE_SIZE - 16 - expected_free_ptr
    )


def test_remove_last_record_leaves_empty_reusable_page():
    """删掉页内唯一记录后：slot_count=0、free_ptr 回到 8，页可再次插入。

    断言的改动：删除末行后页头不重置，或残留已删记录字节。
    """
    page = new_data_page()
    record = encode_record(1, _int_column(), (7,))
    append_record(page, record)

    remove_slot(page, 0)

    assert page_rows(page, _int_column()) == []
    assert page[:2] == (0).to_bytes(2, "little")
    assert page[4:8] == (8).to_bytes(4, "little")
    assert page[8:] == bytes(PAGE_SIZE - 8)
    assert append_record(page, record) is True
    assert page_rows(page, _int_column()) == [(1, (7,))]


def test_replace_slot_updates_row_in_place_with_compaction():
    """替换槽 = 整行更新：目标槽换成新记录，其余记录保留且布局仍连续。

    断言的改动：replace 后出现旧记录残留、槽数变化或丢别的行。
    """
    page = new_data_page()
    rec1 = encode_record(1, _int_column(), (1,))
    rec2 = encode_record(2, _int_column(), (2,))
    rec3 = encode_record(3, _int_column(), (3,))
    append_record(page, rec1)
    append_record(page, rec2)
    append_record(page, rec3)
    longer = encode_record(2, _int_column(), (99,))

    assert replace_slot(page, 1, longer) is True

    assert page_rows(page, _int_column()) == [(1, (1,)), (2, (99,)), (3, (3,))]
    assert page[:2] == (3).to_bytes(2, "little")


def test_replace_slot_too_large_returns_false_and_leaves_page_unchanged():
    """新记录把当前页撑爆时 replace 返回 False，页面字节不能动。

    断言的改动：replace 失败仍先写记录/改页头（半更新状态）。
    """
    page = new_data_page()
    big1 = encode_record(1, (ColumnDef("t", SqlType.TEXT),), ("x" * 2980,))
    big2 = encode_record(2, (ColumnDef("t", SqlType.TEXT),), ("y" * 980,))
    assert append_record(page, big1) is True
    assert append_record(page, big2) is True
    snapshot = bytes(page)
    too_long = encode_record(2, (ColumnDef("t", SqlType.TEXT),), ("z" * 4060,))

    assert replace_slot(page, 0, too_long) is False
    assert bytes(page) == snapshot


def test_page_rows_rejects_corrupt_free_ptr_overlapping_slots():
    """free_ptr 越过槽目录起点（记录与槽重叠）视为损坏 → E_STORAGE。

    断言的改动：page_rows 不校验布局，重叠后照样解码出假数据。
    """
    page = new_data_page()
    append_record(page, encode_record(1, _int_column(), (7,)))
    page[4:8] = PAGE_SIZE.to_bytes(4, "little")  # free_ptr 撑到页尾

    with pytest.raises(SqlError) as exc:
        page_rows(page, _int_column())

    assert exc.value.code == E_STORAGE
