"""BOOLEAN 存储层 M1 测试：编码、校验、更新、重启与损坏路径。

命名规则沿用前几阶段：写清“会让它失败的生产改动”。
本阶段 BOOLEAN 仍经 V1 catalog.json 持久化（页式 Catalog 切换在 M2）。
"""

from __future__ import annotations

import struct
from pathlib import Path

import pytest

from contracts.ast import ColumnDef, SqlType
from contracts.errors import E_STORAGE, E_TYPE_MISMATCH, SqlError
from storage import DatabaseServer, Storage
from storage.constants import PAGE_SIZE, TABLE_FILE_SUFFIX
from storage.engine import decode_record


def _bool_columns() -> tuple[ColumnDef, ...]:
    return (
        ColumnDef("id", SqlType.INT),
        ColumnDef("flag", SqlType.BOOLEAN),
        ColumnDef("note", SqlType.TEXT),
    )


def _typed_columns() -> tuple[ColumnDef, ...]:
    return (
        ColumnDef("int_col", SqlType.INT),
        ColumnDef("real_col", SqlType.REAL),
        ColumnDef("text_col", SqlType.TEXT),
    )


@pytest.fixture
def data_dir(tmp_path) -> str:
    return str(tmp_path / "data")


@pytest.fixture
def storage(data_dir) -> Storage:
    return DatabaseServer(data_dir).connect("main")


def _reopen(data_dir: str) -> Storage:
    return DatabaseServer(data_dir).connect("main")


def _table_path(data_dir: str, name: str) -> Path:
    return Path(data_dir) / "main" / f"{name}{TABLE_FILE_SUFFIX}"


def _expect_code(call, code: str) -> None:
    with pytest.raises(SqlError) as exc:
        call()
    assert exc.value.code == code


# ---- 建表与 Schema 持久化 ----


def test_create_table_persists_boolean_column_and_survives_restart(storage, data_dir):
    """BOOLEAN 列定义必须进 catalog 并在重启后按原顺序还原。

    断言的改动：ColumnDef 丢失 BOOLEAN、列序错、catalog 类型串写错。
    """
    storage.create_table("t", _bool_columns())

    info = storage.describe("t")
    assert info.columns == _bool_columns()
    assert _reopen(data_dir).describe("t").columns[1].type is SqlType.BOOLEAN


# ---- 正常值往返 ----


@pytest.mark.parametrize("value", [True, False])
def test_insert_scan_roundtrip_preserves_python_bool(storage, value):
    """insert 的 True/False 必须原样以 bool 读出，不能被读成 1/0。

    断言的改动：编码丢了布尔语义（按 INT 存）、解码返回 int。
    """
    storage.create_table("t", _bool_columns())

    row_id = storage.insert("t", (1, value, "x"))

    rows = list(storage.scan("t"))
    assert rows == [(row_id, (1, value, "x"))]
    assert type(rows[0][1][1]) is bool


def test_update_toggles_boolean_and_survives_restart(storage, data_dir):
    """BOOLEAN 整行更新必须写回新值，并在重启后仍可读。

    断言的改动：update 跳过 BOOLEAN 列、落盘前值未变更。
    """
    storage.create_table("t", _bool_columns())
    row_id = storage.insert("t", (1, True, "x"))

    storage.update_row("t", row_id, (1, False, "x"))

    assert list(storage.scan("t")) == [(row_id, (1, False, "x"))]
    assert list(_reopen(data_dir).scan("t")) == [(row_id, (1, False, "x"))]


def test_delete_boolean_row_leaves_other_rows(storage):
    """删除一行 BOOLEAN 数据不应影响其他行或破坏解码。

    断言的改动：delete 后行仍可被 scan 到、跨行槽位错乱。
    """
    storage.create_table("t", _bool_columns())
    first = storage.insert("t", (1, True, "a"))
    second = storage.insert("t", (2, False, "b"))

    storage.delete_row("t", first)

    assert list(storage.scan("t")) == [(second, (2, False, "b"))]


# ---- 类型边界 ----


@pytest.mark.parametrize("bad", [1, 0, "true", "", 1.0, None, [], {}])
def test_boolean_column_rejects_non_bool_values(storage, bad):
    """BOOLEAN 列只接受 bool；int/str/float/None 等必须 E_TYPE_MISMATCH。

    断言的改动：按 truthiness 接收、或把 1/0 当作 True/False。
    """
    storage.create_table("t", _bool_columns())

    _expect_code(lambda: storage.insert("t", (1, bad, "x")), E_TYPE_MISMATCH)


@pytest.mark.parametrize(
    ("column_index", "value"),
    [(0, True), (1, False), (2, True)],
)
def test_existing_typed_columns_still_reject_bool(storage, column_index, value):
    """INT/REAL/TEXT 列必须继续拒绝 bool（Python bool 是 int 子类）。

    断言的改动：BOOLEAN 分支误伤既有类型，或既有类型放宽了 bool。
    """
    storage.create_table("t", _typed_columns())
    values = [1, 1.5, "x"]
    values[column_index] = value

    _expect_code(lambda: storage.insert("t", tuple(values)), E_TYPE_MISMATCH)


# ---- 溢出与损坏 ----


def test_boolean_survives_overflow_row(storage, data_dir):
    """BOOLEAN 与超长 TEXT 同行走溢出页链时仍能正确编解码。

    断言的改动：溢出路径只重建 TEXT、丢弃/错位 BOOLEAN 字节。
    """
    columns = (ColumnDef("flag", SqlType.BOOLEAN), ColumnDef("body", SqlType.TEXT))
    storage.create_table("t", columns)
    body = "h" * 9000

    row_id = storage.insert("t", (True, body))

    assert list(storage.scan("t")) == [(row_id, (True, body))]
    assert list(_reopen(data_dir).scan("t")) == [(row_id, (True, body))]


def test_decode_rejects_invalid_boolean_byte():
    """磁盘上的 BOOLEAN 字节只能是 0x00/0x01；其他值必须报 E_STORAGE。

    断言的改动：解码把任意非零字节都当 True。
    """
    columns = (ColumnDef("flag", SqlType.BOOLEAN),)
    record = struct.pack("<Q", 1) + b"\x02"

    _expect_code(lambda: decode_record(record, columns), E_STORAGE)


def test_decode_rejects_truncated_boolean_record():
    """BOOLEAN 字节缺失（记录被截断）必须报 E_STORAGE，不能泄漏 IndexError。

    断言的改动：直接下标取值而非按 BOOL_SIZE 切片校验长度。
    """
    columns = (ColumnDef("flag", SqlType.BOOLEAN),)
    record = struct.pack("<Q", 1)  # 只有 row_id，没有布尔字节

    _expect_code(lambda: decode_record(record, columns), E_STORAGE)


def test_scan_rejects_corrupted_boolean_byte(storage, data_dir):
    """落盘后改坏 BOOLEAN 字节，重开扫描必须报 E_STORAGE。

    断言的改动：scan 把损坏的布尔字节静默解释成 True/False。
    """
    storage.create_table("t", (ColumnDef("flag", SqlType.BOOLEAN),))
    storage.insert("t", (True,))
    path = _table_path(data_dir, "t")
    raw = bytearray(path.read_bytes())
    # 页 1 的第一条记录紧跟页头；记录 = u64 row_id + 1B BOOLEAN。
    raw[PAGE_SIZE + 8 + 8] = 0x02
    path.write_bytes(raw)

    _expect_code(lambda: list(_reopen(data_dir).scan("t")), E_STORAGE)
