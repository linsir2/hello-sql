"""值边界测试：INT/REAL/TEXT 的类型与大小边界、记录 inline/overflow 临界。

已拍板语义：REAL 拒绝 NaN/±Inf（E_TYPE_MISMATCH）。
"""

from __future__ import annotations

import math

import pytest

from contracts.ast import ColumnDef, SqlType
from contracts.errors import E_TYPE_MISMATCH, SqlError
from storage import DatabaseServer


def _expect_code(call, code: str) -> None:
    with pytest.raises(SqlError) as exc:
        call()
    assert exc.value.code == code


@pytest.fixture
def storage(tmp_path):
    return DatabaseServer(str(tmp_path / "data")).connect("main")


def _one_col_storage(storage, sql_type: SqlType, name: str = "t"):
    storage.create_table(name, (ColumnDef("v", sql_type),))
    return name


# ---- INT 边界 ----


def test_int_boundaries_roundtrip(storage):
    """INT 最小/最大/0/负数都正确落库读回。"""
    table = _one_col_storage(storage, SqlType.INT)
    values = [0, -1, 1, -(2**63), 2**63 - 1]

    ids = [storage.insert(table, (v,)) for v in values]
    rows = dict((row[0], row[1][0]) for row in storage.scan(table))

    assert rows == dict(zip(ids, values))


def test_int_rejects_bool_everywhere(storage):
    """bool 混进 INT 的 insert/update 都 E_TYPE_MISMATCH。"""
    table = _one_col_storage(storage, SqlType.INT)
    storage.insert(table, (1,))
    _expect_code(lambda: storage.insert(table, (True,)), E_TYPE_MISMATCH)
    _expect_code(lambda: storage.update_row(table, 1, (False,)), E_TYPE_MISMATCH)


# ---- REAL 边界（含 NaN/Inf 拒绝）----


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
def test_real_rejects_non_finite_values(storage, value):
    """REAL 拒绝 NaN/±Inf → E_TYPE_MISMATCH（拍板语义）。

    断言的改动：_normalize_values 放行非有限浮点。
    """
    table = _one_col_storage(storage, SqlType.REAL)

    _expect_code(lambda: storage.insert(table, (value,)), E_TYPE_MISMATCH)
    assert list(storage.scan(table)) == []


def test_real_finite_boundaries_roundtrip(storage):
    """REAL 有限极值、0、负数、int 归一化全部可往返。"""
    table = _one_col_storage(storage, SqlType.REAL)
    values = [0, -0.0, 1.5, -1e308, 1e308, 7]

    ids = [storage.insert(table, (v,)) for v in values]
    rows = dict((row[0], row[1][0]) for row in storage.scan(table))

    assert rows == dict(zip(ids, [float(v) for v in values]))


@pytest.mark.parametrize("value", [True, "1", None])
def test_real_rejects_wrong_python_types(storage, value):
    """REAL 只收 int/float：bool/str/None → E_TYPE_MISMATCH。"""
    table = _one_col_storage(storage, SqlType.REAL)
    _expect_code(lambda: storage.insert(table, (value,)), E_TYPE_MISMATCH)


# ---- TEXT 边界 ----


def test_text_empty_and_unicode_roundtrip(storage):
    """空串与多字节 UTF-8（emoji）正常往返，不按字符数算长度。"""
    table = _one_col_storage(storage, SqlType.TEXT)
    values = ["", "a", "中文", "😀" * 1000]

    ids = [storage.insert(table, (v,)) for v in values]
    rows = dict((row[0], row[1][0]) for row in storage.scan(table))

    assert rows == dict(zip(ids, values))


def test_text_at_inline_limit_roundtrips(storage):
    """编码恰好 4080B（TEXT-only: 12B 头 + 4068B 内容）走 inline 且完整。"""
    table = _one_col_storage(storage, SqlType.TEXT)
    body = "x" * 4068  # 记录 = 8(rid)+4(len)+4068 = 4080

    storage.insert(table, (body,))

    assert list(storage.scan(table)) == [(1, (body,))]


def test_text_one_byte_over_inline_limit_becomes_overflow(storage):
    """4069B 内容 → 记录 4081B，刚好跨进溢出链，仍完整往返。"""
    table = _one_col_storage(storage, SqlType.TEXT)
    body = "y" * 4069

    storage.insert(table, (body,))

    assert list(storage.scan(table)) == [(1, (body,))]


def test_text_unicode_hits_byte_limit_exactly(storage):
    """中文按 UTF-8 字节算：1356 字 = 4068B 恰好 inline；+1 字走溢出。"""
    table = _one_col_storage(storage, SqlType.TEXT)
    body_inline = "中" * 1356
    body_overflow = "中" * 1357

    storage.insert(table, (body_inline,))
    storage.insert(table, (body_overflow,))

    rows = dict((row[0], row[1][0]) for row in storage.scan(table))
    assert rows == {1: body_inline, 2: body_overflow}


def test_text_with_lone_surrogate_raises_type_mismatch(storage):
    """TEXT 含无法编码成 UTF-8 的孤立代理项 → E_TYPE_MISMATCH（不是裸异常）。

    断言的改动：encode 把 UnicodeEncodeError 原样抛给调用方。
    """
    table = _one_col_storage(storage, SqlType.TEXT)

    _expect_code(lambda: storage.insert(table, ("\ud800",)), E_TYPE_MISMATCH)
    assert list(storage.scan(table)) == []
