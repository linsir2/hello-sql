"""Storage 表级方法 M2 测试：8 个公开方法端到端（契约 §3.0 + PRD §10）。

经过 DatabaseServer.connect 拿真实 Storage；重启 = 同一 data_dir 上
新建 DatabaseServer 再 connect。错误码断言全部字面量。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from contracts.ast import ColumnDef, SqlType
from contracts.errors import (
    E_BAD_ARG,
    E_DUP_COLUMN,
    E_ROW_NOT_FOUND,
    E_STORAGE,
    E_TABLE_EXISTS,
    E_TABLE_NOT_FOUND,
    E_TYPE_MISMATCH,
    E_VALUE_COUNT,
    SqlError,
)
from contracts.storage import TableInfo
from storage import DatabaseServer, Storage
from storage.constants import TABLE_FILE_SUFFIX


def _columns() -> tuple[ColumnDef, ColumnDef, ColumnDef]:
    return (
        ColumnDef("id", SqlType.INT),
        ColumnDef("name", SqlType.TEXT),
        ColumnDef("score", SqlType.REAL),
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


def _insert_three(storage: Storage) -> None:
    storage.create_table("users", _columns())
    storage.insert("users", (1, "alice", 1.5))
    storage.insert("users", (2, "bob", 2.5))
    storage.insert("users", (3, "carol", 3.5))


def _expect_code(call, code: str) -> None:
    with pytest.raises(SqlError) as exc:
        call()
    assert exc.value.code == code


# ---- create_table / drop_table / list / describe ----


def test_create_table_persists_columns_and_survives_restart(storage, data_dir):
    """建表后：表文件存在、describe 还原列序、重启后 catalog 仍可读。

    断言的改动：建表不写文件、丢列/乱序、save/load 没落盘。
    """
    storage.create_table("users", _columns())

    assert _table_path(data_dir, "users").is_file()
    assert storage.list_tables() == ["users"]
    assert storage.describe("users") == TableInfo("users", _columns())
    assert _reopen(data_dir).describe("users") == TableInfo("users", _columns())


def test_create_table_existing_does_not_overwrite_data(storage):
    """已存在的表再次 create 必须 E_TABLE_EXISTS，且原数据不能被破坏。

    断言的改动：存在性检查放在建文件之后（先把文件覆盖了再报错）。
    """
    storage.create_table("users", _columns())
    storage.insert("users", (1, "alice", 1.0))

    _expect_code(
        lambda: storage.create_table("users", _columns()), E_TABLE_EXISTS
    )

    assert list(storage.scan("users")) == [(1, (1, "alice", 1.0))]


@pytest.mark.parametrize("columns", [(), ((ColumnDef("id", SqlType.INT),) * 2)])
def test_create_table_empty_or_duplicate_columns_raises_dup(storage, columns):
    """空列/重复列 → E_DUP_COLUMN，目录里不得出现这张表。

    断言的改动：register 漏查重复列/空列，或建表后把表留进 catalog。
    """
    _expect_code(lambda: storage.create_table("users", columns), E_DUP_COLUMN)

    assert storage.list_tables() == []


def test_create_after_dup_failure_overwrites_orphan_file(storage, data_dir):
    """DUP 失败可能留下孤儿表文件（§9.4 容忍），再次合法建表应覆盖成功。

    断言的改动：孤儿文件挡住后续 create_table（E_STORAGE 或 EXISTS）。
    """
    _expect_code(
        lambda: storage.create_table("users", ()), E_DUP_COLUMN
    )
    assert _table_path(data_dir, "users").is_file()  # 孤儿文件被容忍

    storage.create_table("users", _columns())

    assert storage.describe("users") == TableInfo("users", _columns())


def test_drop_table_removes_registry_and_file_then_name_reusable(storage, data_dir):
    """drop 后 catalog 无此表、表文件被删；同名可重建且 row_id 从头开始。

    断言的改动：drop 只摘 catalog 不删文件 / 摘牌顺序反了 / 映射没清。
    """
    storage.create_table("users", _columns())
    storage.insert("users", (1, "alice", 1.0))

    storage.drop_table("users")

    assert storage.list_tables() == []
    assert not _table_path(data_dir, "users").exists()
    _expect_code(lambda: storage.describe("users"), E_TABLE_NOT_FOUND)

    storage.create_table("users", _columns())
    assert storage.insert("users", (1, "alice", 1.0)) == 1


@pytest.mark.parametrize(
    "name", ["", "Shop", "1abc", "a-b", "a b"]
)
def test_all_name_methods_reject_invalid_names_first(storage, name):
    """所有带表名的方法先抛 E_BAD_ARG，不得落库/留副作用。

    断言的改动：非法名被当成“表不存在”或直接去查 catalog。
    """
    columns = _columns()
    _expect_code(lambda: storage.create_table(name, columns), E_BAD_ARG)
    _expect_code(lambda: storage.drop_table(name), E_BAD_ARG)
    _expect_code(lambda: storage.describe(name), E_BAD_ARG)
    _expect_code(lambda: storage.insert(name, (1, "a", 1.0)), E_BAD_ARG)
    _expect_code(lambda: storage.scan(name), E_BAD_ARG)
    _expect_code(lambda: storage.update_row(name, 1, (1, "a", 1.0)), E_BAD_ARG)
    _expect_code(lambda: storage.delete_row(name, 1), E_BAD_ARG)


def test_drop_table_and_describe_missing_raise_table_not_found(storage):
    """不存在的表 drop/describe 抛 E_TABLE_NOT_FOUND。

    断言的改动：drop/describe 对缺表静默成功。
    """
    _expect_code(lambda: storage.drop_table("ghost"), E_TABLE_NOT_FOUND)
    _expect_code(lambda: storage.describe("ghost"), E_TABLE_NOT_FOUND)


def test_list_tables_sorted_stable(storage):
    """list_tables 返回稳定顺序（排序实现），先建 z 后建 a 也按名排。

    断言的改动：list_tables 按建表顺序输出导致跨实例不稳定。
    """
    storage.create_table("zebra", (ColumnDef("id", SqlType.INT),))
    storage.create_table("alpha", (ColumnDef("id", SqlType.INT),))

    assert storage.list_tables() == ["alpha", "zebra"]


# ---- insert / scan ----


def test_insert_returns_ids_and_scan_normalizes_real(storage):
    """insert 返回 1、2…；REAL 以 int 写入后 scan 回来必须是 float（§2.2）。

    断言的改动：REAL 不归一化、insert 返回值错、scan 漏行。
    """
    storage.create_table("users", _columns())

    assert storage.insert("users", (1, "alice", 1)) == 1
    assert storage.insert("users", (2, "bob", 2.5)) == 2

    rows = sorted(storage.scan("users"), key=lambda row: row[0])
    assert rows == [
        (1, (1, "alice", 1.0)),
        (2, (2, "bob", 2.5)),
    ]
    assert type(rows[0][1][2]) is float


def test_insert_wrong_count_or_types_raise_before_write(storage):
    """values 个数/类型错必须抛 E_VALUE_COUNT/E_TYPE_MISMATCH 且不产生行。

    断言的改动：B 依赖 C 预检不设防、bool 混进 INT/REAL、或写了一半再报错。
    """
    storage.create_table("users", _columns())

    _expect_code(lambda: storage.insert("users", (1, "a")), E_VALUE_COUNT)
    _expect_code(lambda: storage.insert("users", (1, "a", "x")), E_TYPE_MISMATCH)
    _expect_code(lambda: storage.insert("users", (True, "a", 1.0)), E_TYPE_MISMATCH)
    _expect_code(lambda: storage.insert("users", (1, "a", True)), E_TYPE_MISMATCH)
    _expect_code(lambda: storage.insert("users", (1, 5, 1.0)), E_TYPE_MISMATCH)

    assert list(storage.scan("users")) == []


def test_insert_int_out_of_64bit_range_raises_type_mismatch(storage):
    """INT 超出 64 位有符号范围 → E_TYPE_MISMATCH（契约无独立范围码）。

    断言的改动：让 struct.error 裸抛出来（不属于 13 码）。
    """
    storage.create_table("nums", (ColumnDef("n", SqlType.INT),))

    _expect_code(
        lambda: storage.insert("nums", (2**63,)), E_TYPE_MISMATCH
    )
    _expect_code(
        lambda: storage.insert("nums", (-(2**63) - 1,)), E_TYPE_MISMATCH
    )
    assert list(storage.scan("nums")) == []


def test_scan_missing_table_raises_immediately(storage):
    """scan 缺表必须在调用时立刻抛 E_TABLE_NOT_FOUND，不是等迭代才抛。

    断言的改动：scan 校验放进了生成器体里（惰性到 next 才报）。
    """
    _expect_code(lambda: storage.scan("ghost"), E_TABLE_NOT_FOUND)


def test_row_too_large_raises_storage_with_m5_notice(storage):
    """编码后超 INLINE_RECORD_LIMIT 的行 M2 明确报 E_STORAGE（D14）。

    断言的改动：M2 静默截断/写坏页，或不带“M5 前不支持”的说明。
    """
    storage.create_table("notes", (ColumnDef("body", SqlType.TEXT),))
    huge = "x" * 5000

    _expect_code(lambda: storage.insert("notes", (huge,)), E_STORAGE)
    assert list(storage.scan("notes")) == []


# ---- update_row / delete_row ----


def test_update_and_delete_crud_with_restart_persistence(storage, data_dir):
    """整轮 CRUD 后重开 DatabaseServer 数据仍完整可读（契约 2.2/§11 T4）。

    断言的改动：update/delete 不落盘、重启后行丢失、row_id 被复用。
    """
    _insert_three(storage)

    reopened = _reopen(data_dir)
    reopened.update_row("users", 1, (10, "ALICE", 10.0))
    reopened.delete_row("users", 2)

    again = _reopen(data_dir)
    rows = sorted(again.scan("users"), key=lambda row: row[0])
    assert rows == [
        (1, (10, "ALICE", 10.0)),
        (3, (3, "carol", 3.5)),
    ]
    assert again.insert("users", (4, "dave", 4.5)) == 4


def test_update_validates_values_and_row_id(storage):
    """update 值错走 E_VALUE_COUNT/E_TYPE_MISMATCH；行不存在 E_ROW_NOT_FOUND。

    断言的改动：update 跳过边界直接写、或把 row_id 错当成类型错。
    """
    storage.create_table("users", _columns())
    storage.insert("users", (1, "alice", 1.0))

    _expect_code(
        lambda: storage.update_row("users", 1, (1, "a")), E_VALUE_COUNT
    )
    _expect_code(
        lambda: storage.update_row("users", 1, (True, "a", 1.0)),
        E_TYPE_MISMATCH,
    )
    _expect_code(
        lambda: storage.update_row("users", 99, (1, "a", 1.0)),
        E_ROW_NOT_FOUND,
    )
    _expect_code(
        lambda: storage.update_row("users", True, (1, "a", 1.0)),
        E_ROW_NOT_FOUND,
    )
    assert list(storage.scan("users")) == [(1, (1, "alice", 1.0))]


def test_delete_missing_row_raises_and_remaining_rows_survive(storage):
    """delete 不存在 row_id → E_ROW_NOT_FOUND；成功删除后其余行完好。

    断言的改动：delete 缺行静默成功/误删其他行。
    """
    _insert_three(storage)

    _expect_code(lambda: storage.delete_row("users", 99), E_ROW_NOT_FOUND)
    _expect_code(lambda: storage.delete_row("users", True), E_ROW_NOT_FOUND)

    storage.delete_row("users", 2)

    rows = sorted(storage.scan("users"), key=lambda row: row[0])
    assert [row[0] for row in rows] == [1, 3]


def test_drop_table_clears_engine_mapping_for_same_name(storage):
    """drop 后重建同名表，旧引擎映射不得残留导致 update 误定位。

    断言的改动：drop_table 忘了丢弃 TableEngine，重建后旧 rid 还能命中。
    """
    storage.create_table("users", _columns())
    storage.insert("users", (1, "old", 1.0))
    storage.drop_table("users")

    storage.create_table("users", _columns())
    new_id = storage.insert("users", (7, "new", 2.0))

    assert new_id == 1
    storage.update_row("users", 1, (1, "fresh", 3.0))
    assert list(storage.scan("users")) == [(1, (1, "fresh", 3.0))]
