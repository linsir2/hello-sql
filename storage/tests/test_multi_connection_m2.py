"""M3 多 Storage 句柄在页式 Catalog 下的共享与隔离矩阵。"""

from __future__ import annotations

from pathlib import Path

import pytest

from contracts.ast import ColumnDef, SqlType
from contracts.errors import E_STORAGE, E_TABLE_NOT_FOUND, SqlError
from storage import DatabaseServer
from storage.constants import SYS_TABLES_FILE_NAME


def _expect_code(call, code: str) -> None:
    with pytest.raises(SqlError) as exc:
        call()
    assert exc.value.code == code


def test_three_handles_alternate_ddl_dml_and_survive_restart(tmp_path):
    """三句柄交替 create/insert/update/drop，重启后状态一致。"""
    data_dir = str(tmp_path / "data")
    server = DatabaseServer(data_dir)
    a = server.connect("main")
    b = server.connect("main")
    c = server.connect("main")

    a.create_table("t", (ColumnDef("id", SqlType.INT),))
    row_id = b.insert("t", (1,))
    c.update_row("t", row_id, (2,))
    assert list(a.scan("t")) == [(row_id, (2,))]
    assert b.list_tables() == ["t"]

    c.drop_table("t")
    _expect_code(lambda: b.describe("t"), E_TABLE_NOT_FOUND)

    reopened = DatabaseServer(data_dir).connect("main")
    assert reopened.list_tables() == []


def test_same_table_name_in_two_databases_is_isolated(tmp_path):
    """两个库中的同名表经不同句柄访问时 schema/数据互不污染。"""
    data_dir = str(tmp_path / "data")
    server = DatabaseServer(data_dir)
    server.create_database("shop")
    main = server.connect("main")
    shop = server.connect("shop")

    main.create_table("t", (ColumnDef("id", SqlType.INT),))
    shop.create_table(
        "t", (ColumnDef("id", SqlType.INT), ColumnDef("tag", SqlType.TEXT))
    )
    main_rid = main.insert("t", (1,))
    shop_rid = shop.insert("t", (1, "shop"))

    assert list(main.scan("t")) == [(main_rid, (1,))]
    assert list(shop.scan("t")) == [(shop_rid, (1, "shop"))]

    reopened = DatabaseServer(data_dir)
    assert list(reopened.connect("main").scan("t")) == [(main_rid, (1,))]
    assert list(reopened.connect("shop").scan("t")) == [
        (shop_rid, (1, "shop"))
    ]


def test_connect_validation_does_not_replace_shared_catalog(tmp_path):
    """外部损坏时 connect 报 E_STORAGE；修复后共享对象仍可继续使用。"""
    data_dir = str(tmp_path / "data")
    server = DatabaseServer(data_dir)
    storage = server.connect("main")
    storage.create_table("t", (ColumnDef("id", SqlType.INT),))
    rid = storage.insert("t", (1,))
    sys_path = Path(data_dir) / "main" / SYS_TABLES_FILE_NAME
    good = sys_path.read_bytes()
    sys_path.write_bytes(b"BAD!" + good[4:])

    _expect_code(lambda: server.connect("main"), E_STORAGE)

    sys_path.write_bytes(good)
    fresh = server.connect("main")
    assert fresh.describe("t").columns == (ColumnDef("id", SqlType.INT),)
    assert list(storage.scan("t")) == [(rid, (1,))]
