"""第二轮独立对抗测试（与上一轮用例互补、互不依赖上下文）。

期望来源：
- 契约（docs/contract-v1.md V1.1）：类型规则 / row_id 单调不复用 /
  损坏路径 E_STORAGE / 失败时数据可读；
- B 内部拍板语义（storage_prd.md）：REAL 只收有限值（E_TYPE_MISMATCH）、
  row_id 全表唯一（D08）、单行上限 MAX_ROW_BYTES、溢出链 D14、
  立即紧凑 D15、文件只增不减 D06；
- 物理不变式：audit_util 的页 0 / free list / OVFL 引用 / rid 唯一审计。

原则：先用红测试证明“产品行为偏离上述期望”，再修产品；不得为过测试改期望。
所有用例自建临时 data_dir，不依赖用例执行顺序或共享状态。
"""

from __future__ import annotations

import struct
from pathlib import Path

import pytest

from contracts.ast import ColumnDef, SqlType
from contracts.errors import E_ROW_NOT_FOUND, E_STORAGE, E_TYPE_MISMATCH, SqlError
from storage import DatabaseServer
from storage.constants import (
    MAX_ROW_BYTES,
    PAGE_SIZE,
    SLOT_OVERFLOW_FLAG,
    TABLE_FILE_SUFFIX,
)
from audit_util import audit_table_file


def _expect_code(call, code: str) -> None:
    with pytest.raises(SqlError) as exc:
        call()
    assert exc.value.code == code


@pytest.fixture
def data_dir(tmp_path) -> str:
    return str(tmp_path / "data")


def _table_path(data_dir: str, name: str = "t") -> Path:
    return Path(data_dir) / "main" / f"{name}{TABLE_FILE_SUFFIX}"


def _new_server(data_dir: str):
    """重开 DatabaseServer（模拟重启，绕开旧缓存帧读磁盘真相）。"""
    return DatabaseServer(data_dir)


def _inline_record_locations(path: Path) -> list[tuple[int, int, int]]:
    """返回 (页号, 记录偏移, row_id)，只收 inline 记录（不解析溢出锚点）。"""
    raw = path.read_bytes()
    assert len(raw) >= PAGE_SIZE and len(raw) % PAGE_SIZE == 0
    found: list[tuple[int, int, int]] = []
    for page_no in range(1, len(raw) // PAGE_SIZE):
        page = raw[page_no * PAGE_SIZE : (page_no + 1) * PAGE_SIZE]
        (slot_count,) = struct.unpack_from("<H", page, 0)
        assert slot_count <= (PAGE_SIZE - 8) // 8
        for i in range(slot_count):
            record_offset, raw_length = struct.unpack_from(
                "<II", page, PAGE_SIZE - 8 * (i + 1)
            )
            if raw_length & SLOT_OVERFLOW_FLAG:
                continue
            (row_id,) = struct.unpack_from("<Q", page, record_offset)
            found.append((page_no, record_offset, row_id))
    return found


# ---------------------------------------------------------------- REAL 边界


@pytest.mark.parametrize(
    ("value",),
    [
        pytest.param(2**1024, id="2^1024"),
        pytest.param(-(2**1024), id="-2^1024"),
        pytest.param(10**400, id="10^400"),
        pytest.param(-(10**400), id="-10^400"),
        pytest.param(10**10000, id="10^10000"),
    ],
)
def test_real_int_too_large_for_double_is_type_mismatch(data_dir, value):
    """超出 double 可表示范围的 int：REAL 必须 E_TYPE_MISMATCH，不许泄漏
    裸 OverflowError（REAL 只存有限值；表示不了的值按类型/范围不符拒绝）。"""
    storage = _new_server(data_dir).connect("main")
    storage.create_table("t", (ColumnDef("r", SqlType.REAL),))

    _expect_code(lambda: storage.insert("t", (value,)), E_TYPE_MISMATCH)
    assert list(storage.scan("t")) == []

    ok_rid = storage.insert("t", (1.5,))
    _expect_code(
        lambda: storage.update_row("t", ok_rid, (value,)), E_TYPE_MISMATCH
    )
    rows = list(storage.scan("t"))
    assert rows == [(ok_rid, (1.5,))]


def test_real_largest_representable_int_accepted(data_dir):
    """刚好能表示成有限 double 的 int 仍要能落库（int→float 归一化）。"""
    storage = _new_server(data_dir).connect("main")
    storage.create_table("t", (ColumnDef("r", SqlType.REAL),))
    value = 2**1023  # float(value) 有限
    rid = storage.insert("t", (value,))
    rows = list(storage.scan("t"))
    assert rows == [(rid, (float(value),))]


def test_real_int_loses_precision_like_float(data_dir):
    """REAL 内部存 float：>2^53 的 int 精度丢失是既定语义，不是 bug。"""
    storage = _new_server(data_dir).connect("main")
    storage.create_table("t", (ColumnDef("r", SqlType.REAL),))
    value = 2**53 + 1
    rid = storage.insert("t", (value,))
    assert list(storage.scan("t")) == [(rid, (float(value),))]


@pytest.mark.parametrize(
    ("value",),
    [
        pytest.param(2**63, id="2^63"),
        pytest.param(-(2**63) - 1, id="-2^63-1"),
        pytest.param(10**10000, id="10^10000"),
    ],
)
def test_int_out_of_64bit_is_type_mismatch_not_value_error(data_dir, value):
    """INT 超 64 位：必须 E_TYPE_MISMATCH；极长 int 的报错消息格式化也不得
    触发 Python int-str 位数保护（不允许泄漏裸 ValueError）。"""
    storage = _new_server(data_dir).connect("main")
    storage.create_table("t", (ColumnDef("i", SqlType.INT),))

    _expect_code(lambda: storage.insert("t", (value,)), E_TYPE_MISMATCH)
    assert list(storage.scan("t")) == []

    ok_rid = storage.insert("t", (1,))
    _expect_code(
        lambda: storage.update_row("t", ok_rid, (value,)), E_TYPE_MISMATCH
    )
    assert list(storage.scan("t")) == [(ok_rid, (1,))]


# ------------------------------------------------------- 失败原子性/上限


def test_oversize_insert_raises_and_leaves_file_bytes_unchanged(data_dir):
    """编码后超过 MAX_ROW_BYTES 的 insert 抛 E_STORAGE，且磁盘文件
    一个字节都不许变（页 0 计数也不许被提前落盘）。"""
    storage = _new_server(data_dir).connect("main")
    storage.create_table("t", (ColumnDef("s", SqlType.TEXT),))
    storage.insert("t", ("seed",))
    path = _table_path(data_dir)
    before = path.read_bytes()

    _expect_code(
        lambda: storage.insert("t", ("x" * MAX_ROW_BYTES,)), E_STORAGE
    )
    assert path.read_bytes() == before
    assert list(storage.scan("t"))[0][1] == ("seed",)
    audit_table_file(path, storage.describe("t").columns)


def test_oversize_update_keeps_row_and_other_rows_intact(data_dir):
    """把现有行更新成超上限长度：必须抛 E_STORAGE，原行与邻行原样保留。"""
    storage = _new_server(data_dir).connect("main")
    storage.create_table("t", (ColumnDef("id", SqlType.INT), ColumnDef("s", SqlType.TEXT)))
    rid_a = storage.insert("t", (1, "keep-a"))
    rid_b = storage.insert("t", (2, "keep-b"))
    path = _table_path(data_dir)
    before = path.read_bytes()

    _expect_code(
        lambda: storage.update_row("t", rid_a, (1, "x" * MAX_ROW_BYTES)),
        E_STORAGE,
    )
    assert path.read_bytes() == before
    rows = sorted(storage.scan("t"))
    assert rows == [(rid_a, (1, "keep-a")), (rid_b, (2, "keep-b"))]
    audit_table_file(path, storage.describe("t").columns)


def test_row_id_stays_monotonic_after_failed_insert(data_dir):
    """失败 insert 不许留下半行，也不许复用 row_id（D08：单调不复用）。"""
    storage = _new_server(data_dir).connect("main")
    storage.create_table("t", (ColumnDef("s", SqlType.TEXT),))
    first = storage.insert("t", ("a",))
    _expect_code(lambda: storage.insert("t", ("x" * MAX_ROW_BYTES,)), E_STORAGE)
    after_failure = storage.insert("t", ("b",))
    assert after_failure > first
    rows = list(storage.scan("t"))
    assert rows == [(first, ("a",)), (after_failure, ("b",))]


def test_max_row_exact_boundary_roundtrips_and_survives_restart(data_dir):
    """编码总长恰为 MAX_ROW_BYTES 的行可以落库、跨重启完整读回，
    物理审计通过（链页数量足以触发默认 LRU 淘汰写回）。"""
    storage = _new_server(data_dir).connect("main")
    storage.create_table("t", (ColumnDef("s", SqlType.TEXT),))
    text = "a" * (MAX_ROW_BYTES - 12)  # 记录 = 8B rid + 4B 长度 + 文本
    rid = storage.insert("t", (text,))

    reopened = _new_server(data_dir).connect("main")
    rows = list(reopened.scan("t"))
    assert len(rows) == 1
    assert rows[0][0] == rid
    assert rows[0][1] == (text,)
    audit_table_file(_table_path(data_dir), reopened.describe("t").columns)


# ------------------------------------------------------- 溢出链生命周期


def test_mixed_overflow_delete_shrink_grow_audits_every_step(data_dir):
    """同页混合 inline/溢出：删 A → B 缩成 inline → C 长成溢出，
    每步 scan 对照模型并做物理审计；重启后仍一致。"""
    storage = _new_server(data_dir).connect("main")
    storage.create_table("t", (ColumnDef("id", SqlType.INT), ColumnDef("s", SqlType.TEXT)))
    big = "x" * 9000
    rid_a = storage.insert("t", (1, big))
    rid_b = storage.insert("t", (2, big))
    rid_c = storage.insert("t", (3, "small"))

    def check(expected: dict[int, tuple]) -> None:
        actual = dict((row[0], row[1]) for row in storage.scan("t"))
        assert actual == expected
        audit_table_file(_table_path(data_dir), storage.describe("t").columns)

    model = {rid_a: (1, big), rid_b: (2, big), rid_c: (3, "small")}
    check(model)

    storage.delete_row("t", rid_a)
    del model[rid_a]
    check(model)

    storage.update_row("t", rid_b, (2, "now-inline"))
    model[rid_b] = (2, "now-inline")
    check(model)

    storage.update_row("t", rid_c, (3, big))
    model[rid_c] = (3, big)
    check(model)

    reopened = _new_server(data_dir).connect("main")
    assert dict((row[0], row[1]) for row in reopened.scan("t")) == model
    audit_table_file(_table_path(data_dir), reopened.describe("t").columns)


def test_multibyte_char_split_across_overflow_payload_boundary(data_dir):
    """文本的 4 字节字符恰好跨 OVFL payload(4080B) 边界时，整行仍等值读回。"""
    storage = _new_server(data_dir).connect("main")
    storage.create_table("t", (ColumnDef("s", SqlType.TEXT),))
    # 记录字节 = 8(rid) + 4(len) + 文本；让字符首字节落在 4080 边界之后。
    head = "a" * 4066
    text = head + "𝄞" * 8 + "tail"
    rid = storage.insert("t", (text,))
    rows = list(storage.scan("t"))
    assert rows == [(rid, (text,))]
    assert len(rows[0][1][0].encode("utf-8")) > 4080  # 确实走了溢出链
    audit_table_file(_table_path(data_dir), storage.describe("t").columns)


# ------------------------------------------------------- 损坏路径补强


def _corrupt_second_row_id_to_first(path: Path) -> None:
    """把另一页上某条 inline 记录的 row_id 改成第一页首条的 row_id。"""
    records = _inline_record_locations(path)
    assert len({p for p, _o, _r in records}) >= 2
    (page_a, _o_a, rid_a) = records[0]
    (page_b, offset_b, _r_b) = next(
        (p, o, r) for p, o, r in records if p != page_a
    )
    assert rid_a != 0
    raw = bytearray(path.read_bytes())
    struct.pack_into("<Q", raw, page_b * PAGE_SIZE + offset_b, rid_a)
    path.write_bytes(raw)


def test_duplicate_row_id_across_pages_is_storage_error(data_dir):
    """物理损坏：两个 inline 记录同 rid（跨页）违反 D08 唯一不变式。
    scan 必须抛 E_STORAGE，而不是静默返回重复行 / 让 update 二义。"""
    storage = _new_server(data_dir).connect("main")
    storage.create_table("t", (ColumnDef("a", SqlType.INT),))
    for i in range(200):  # 保证跨页
        storage.insert("t", (i,))

    _corrupt_second_row_id_to_first(_table_path(data_dir))
    reopened = _new_server(data_dir).connect("main")
    _expect_code(lambda: list(reopened.scan("t")), E_STORAGE)


def test_duplicate_row_id_can_be_dropped_and_name_reused(data_dir):
    """重复 rid 的损坏表：scan 抛 E_STORAGE，但库不被“砖”——
    可以 drop 掉损坏表并用同名重建，重建后一切正常。"""
    storage = _new_server(data_dir).connect("main")
    storage.create_table("t", (ColumnDef("a", SqlType.INT),))
    for i in range(200):
        storage.insert("t", (i,))

    _corrupt_second_row_id_to_first(_table_path(data_dir))
    reopened = _new_server(data_dir).connect("main")
    _expect_code(lambda: list(reopened.scan("t")), E_STORAGE)
    assert "t" in reopened.list_tables()

    reopened.drop_table("t")
    assert "t" not in reopened.list_tables()
    assert not _table_path(data_dir).exists()

    storage2 = _new_server(data_dir).connect("main")
    storage2.create_table("t", (ColumnDef("a", SqlType.INT),))
    rid = storage2.insert("t", (1,))
    assert list(storage2.scan("t")) == [(rid, (1,))]


# ------------------------------------------------------- 行为防回归


def test_empty_table_scan_restart_and_drop(data_dir):
    """空表（只有页 0）：scan 空、重启后仍空、drop 干净。"""
    storage = _new_server(data_dir).connect("main")
    storage.create_table("t", (ColumnDef("a", SqlType.INT),))
    assert list(storage.scan("t")) == []
    reopened = _new_server(data_dir).connect("main")
    assert list(reopened.scan("t")) == []
    assert reopened.describe("t").columns == (ColumnDef("a", SqlType.INT),)
    audit_table_file(_table_path(data_dir), reopened.describe("t").columns)
    reopened.drop_table("t")
    assert not _table_path(data_dir).exists()


def test_deleted_row_id_cannot_hit_replacement_row(data_dir):
    """删除过的 row_id 之后无论是否被文件复用，update/delete 都只能
    E_ROW_NOT_FOUND，绝不许命中后来落在同页的新行。"""
    storage = _new_server(data_dir).connect("main")
    storage.create_table("t", (ColumnDef("a", SqlType.INT),))
    rid_gone = storage.insert("t", (1,))
    storage.delete_row("t", rid_gone)
    rid_new = storage.insert("t", (2,))  # 复用空闲页的最常见路径
    _expect_code(lambda: storage.update_row("t", rid_gone, (9,)), E_ROW_NOT_FOUND)
    _expect_code(lambda: storage.delete_row("t", rid_gone), E_ROW_NOT_FOUND)
    assert list(storage.scan("t")) == [(rid_new, (2,))]
    audit_table_file(_table_path(data_dir), storage.describe("t").columns)
