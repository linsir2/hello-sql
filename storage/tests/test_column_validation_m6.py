"""非法列定义防御（第二轮对抗测试拍板项）。

决策：create_table 的 ColumnDef 自身非法一律 E_BAD_ARG（不新增错误码）：
- 列名非 str / 空 / 不匹配 [a-z_][a-z0-9_]*；
- column.type 不是 SqlType（不是“值 vs 列”错，不能用 E_TYPE_MISMATCH）；
- 条目本身不是 ColumnDef。
校验在建表文件之前完成：失败不得留下孤儿表文件、不得污染 catalog；
空列 / 重复列维持 E_DUP_COLUMN（契约固定）；表已存在仍优先 E_TABLE_EXISTS。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from contracts.ast import ColumnDef, SqlType
from contracts.errors import E_BAD_ARG, E_DUP_COLUMN, E_TABLE_EXISTS, SqlError
from storage import DatabaseServer
from storage.constants import TABLE_FILE_SUFFIX


def _expect_code(call, code: str) -> None:
    with pytest.raises(SqlError) as exc:
        call()
    assert exc.value.code == code


@pytest.fixture
def data_dir(tmp_path) -> str:
    return str(tmp_path / "data")


def _table_path(data_dir: str) -> Path:
    return Path(data_dir) / "main" / f"t{TABLE_FILE_SUFFIX}"


@pytest.mark.parametrize(
    "name",
    ["", "Bad", "bad name", "9abc", "中", "a.b", None, 123],
    ids=["empty", "upper", "space", "leading-digit", "unicode", "dot", "none", "int"],
)
def test_create_table_rejects_invalid_column_names(data_dir, name):
    """非法列名 → E_BAD_ARG，catalog 不变、无孤儿表文件、重启不受影响。"""
    storage = DatabaseServer(data_dir).connect("main")

    _expect_code(
        lambda: storage.create_table("t", (ColumnDef(name, SqlType.INT),)),
        E_BAD_ARG,
    )
    assert storage.list_tables() == []
    assert not _table_path(data_dir).exists()

    reopened = DatabaseServer(data_dir).connect("main")
    assert reopened.list_tables() == []

    reopened.create_table("t", (ColumnDef("ok", SqlType.INT),))
    assert reopened.describe("t").columns == (ColumnDef("ok", SqlType.INT),)


@pytest.mark.parametrize(
    "bad_type",
    ["INT", None, 1, object()],
    ids=["str", "none", "int", "object"],
)
def test_create_table_rejects_non_sqltype_column_type(data_dir, bad_type):
    """column.type 不是 SqlType → E_BAD_ARG（参数本身坏，不是入库值错）。"""
    storage = DatabaseServer(data_dir).connect("main")

    _expect_code(
        lambda: storage.create_table("t", (ColumnDef("a", bad_type),)),
        E_BAD_ARG,
    )
    assert storage.list_tables() == []
    assert not _table_path(data_dir).exists()


def test_create_table_rejects_non_columndef_entries(data_dir):
    """columns 条目不是 ColumnDef → E_BAD_ARG。"""
    storage = DatabaseServer(data_dir).connect("main")

    _expect_code(
        lambda: storage.create_table("t", (("a", SqlType.INT),)),
        E_BAD_ARG,
    )
    assert storage.list_tables() == []
    assert not _table_path(data_dir).exists()


def test_invalid_column_shape_beats_duplicate_columns(data_dir):
    """同一条列定义既非法又重名：报 E_BAD_ARG（自身格式错优先），
    不报 E_DUP_COLUMN。"""
    storage = DatabaseServer(data_dir).connect("main")
    bad = ColumnDef("Bad", SqlType.INT)

    _expect_code(
        lambda: storage.create_table("t", (bad, bad)),
        E_BAD_ARG,
    )
    assert storage.list_tables() == []


def test_table_exists_still_beats_invalid_columns(data_dir):
    """既有错误优先级不变：表已存在先于列定义校验。"""
    storage = DatabaseServer(data_dir).connect("main")
    storage.create_table("t", (ColumnDef("a", SqlType.INT),))

    _expect_code(
        lambda: storage.create_table(
            "t", (ColumnDef("Bad", SqlType.INT),)
        ),
        E_TABLE_EXISTS,
    )


def test_empty_and_duplicate_columns_still_dup_column(data_dir):
    """契约不变：空列 / 重复合法列仍 E_DUP_COLUMN，不因新防御变成 E_BAD_ARG。"""
    storage = DatabaseServer(data_dir).connect("main")

    _expect_code(lambda: storage.create_table("t", ()), E_DUP_COLUMN)
    dup = (ColumnDef("a", SqlType.INT), ColumnDef("a", SqlType.TEXT))
    _expect_code(lambda: storage.create_table("t", dup), E_DUP_COLUMN)
    assert storage.list_tables() == []
