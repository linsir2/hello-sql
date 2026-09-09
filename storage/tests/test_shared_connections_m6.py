"""多 Storage 句柄共享状态（方案 A 拍板项）。

单用户单进程仍可能出现同一库的多个 Storage 句柄（connect 可多次调用）。
拍板：catalog 与 TableEngine 的内存真相上收到 DatabaseServer，一个库只有
一份；Storage 是薄视图。因此：
- 任一句柄 create/drop 表，其它句柄立即可见；
- 任一句柄 insert 后，其它句柄可用同一 rid update/delete（共享 rid 映射）；
- drop + 同名重建（不同 schema 或同 schema）不会让旧句柄残留的旧 engine
  命中新文件；
- 句柄对应的库被 drop 后，句柄不应静默写进同名重建的新库。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from contracts.ast import ColumnDef, SqlType
from contracts.errors import E_ROW_NOT_FOUND, E_STORAGE, E_TABLE_NOT_FOUND, SqlError
from storage import DatabaseServer, Storage
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


def test_two_connections_share_catalog_after_drop_recreate(data_dir):
    """drop/重建后另一句柄立即看到新 schema，可按新 schema 正常读写。"""
    server = DatabaseServer(data_dir)
    s1: Storage = server.connect("main")
    s2: Storage = server.connect("main")

    s1.create_table("t", (ColumnDef("a", SqlType.INT),))
    assert s2.describe("t").columns == (ColumnDef("a", SqlType.INT),)

    s1.drop_table("t")
    assert s2.list_tables() == []
    _expect_code(lambda: s2.describe("t"), E_TABLE_NOT_FOUND)

    s1.create_table("t", (ColumnDef("a", SqlType.INT), ColumnDef("b", SqlType.TEXT)))
    rid = s2.insert("t", (7, "from-s2"))
    rows1 = sorted(s1.scan("t"))
    rows2 = sorted(s2.scan("t"))
    assert rows1 == rows2 == [(rid, (7, "from-s2"))]

    reopened = DatabaseServer(data_dir).connect("main")
    assert list(reopened.scan("t")) == [(rid, (7, "from-s2"))]


def test_two_connections_share_engine_rid_mapping(data_dir):
    """s1 insert 后，s2 用同一 rid 能 update/delete（共享 engine 而非各持快照）。"""
    server = DatabaseServer(data_dir)
    s1 = server.connect("main")
    s2 = server.connect("main")

    s1.create_table("t", (ColumnDef("a", SqlType.INT),))
    rid = s1.insert("t", (1,))

    s2.update_row("t", rid, (2,))
    assert list(s1.scan("t")) == [(rid, (2,))]

    s2.delete_row("t", rid)
    assert list(s1.scan("t")) == []
    _expect_code(lambda: s1.update_row("t", rid, (3,)), E_ROW_NOT_FOUND)


def test_recreate_same_schema_does_not_reuse_old_engine_state(data_dir):
    """同 schema drop+重建：旧句柄不得用旧 rid→页 映射命中新文件的行。"""
    server = DatabaseServer(data_dir)
    s1 = server.connect("main")
    s2 = server.connect("main")
    s1.create_table("t", (ColumnDef("a", SqlType.INT),))
    for i in range(1, 6):
        s1.insert("t", (i,))  # 旧 rid 1..5

    s1.drop_table("t")
    s1.create_table("t", (ColumnDef("a", SqlType.INT),))
    new_rid = s1.insert("t", (100,))  # 新文件 row_id 从 1 重新开始
    assert new_rid == 1

    # 旧文件里的 rid 5 在新文件不存在：update 只能 E_ROW_NOT_FOUND，
    # 不许被旧映射指到新文件某页上的新行。
    _expect_code(lambda: s2.update_row("t", 5, (999,)), E_ROW_NOT_FOUND)
    _expect_code(lambda: s2.delete_row("t", 5), E_ROW_NOT_FOUND)
    assert list(s1.scan("t")) == [(1, (100,))]
    assert list(s2.scan("t")) == [(1, (100,))]


def test_create_through_one_handle_visible_to_other_handle(data_dir):
    """建表/建库的目录动作经共享 registry，另一个句柄的 list 也更新。"""
    server = DatabaseServer(data_dir)
    s1 = server.connect("main")
    s2 = server.connect("main")

    s1.create_table("u", (ColumnDef("id", SqlType.INT),))
    s1.create_table("v", (ColumnDef("id", SqlType.INT),))
    assert s2.list_tables() == ["u", "v"]

    s2.drop_table("u")
    assert s1.list_tables() == ["v"]


def test_dropped_database_handle_does_not_write_recreated_db(data_dir):
    """库被 drop 后旧句柄失效：同名重建后的新库不能被旧句柄静默写入。"""
    server = DatabaseServer(data_dir)
    server.create_database("shop")
    old = server.connect("shop")
    old.create_table("t", (ColumnDef("a", SqlType.INT),))

    server.drop_database("shop")
    server.create_database("shop")

    _expect_code(lambda: old.create_table("t", (ColumnDef("a", SqlType.INT),)), E_STORAGE)
    fresh = server.connect("shop")
    assert fresh.list_tables() == []
