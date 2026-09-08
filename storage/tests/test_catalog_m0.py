"""Catalog M0 单元测试：最底层、无外部依赖（只碰 catalog.json）。

命名规则：每个测试先问“哪个生产改动会让它失败”，断言真实行为，
期望值全部手写（列定义、错误码、文件名都是字面量）。
"""

from __future__ import annotations

import json

import pytest

from contracts.ast import ColumnDef, SqlType
from contracts.errors import E_DUP_COLUMN, E_STORAGE, E_TABLE_EXISTS, E_TABLE_NOT_FOUND, SqlError
from storage.catalog import Catalog
from storage.constants import CATALOG_VERSION, JSON_TABLES_KEY, JSON_TYPE_KEY, JSON_VERSION_KEY


def _columns() -> tuple[ColumnDef, ColumnDef, ColumnDef]:
    """测试用固定三列：id INT / name TEXT / age REAL（顺序即语义）。"""
    return (
        ColumnDef("id", SqlType.INT),
        ColumnDef("name", SqlType.TEXT),
        ColumnDef("age", SqlType.REAL),
    )


def test_empty_catalog_save_load_roundtrip(tmp_path):
    """空库 save 后再 load，names 应为空——断言的改动：save/load 没把表写丢。"""
    path = tmp_path / "catalog.json"
    Catalog(path).save()

    loaded = Catalog(path)
    loaded.load()

    assert loaded.names() == []


def test_register_save_reload_preserves_column_order(tmp_path):
    """注册三列 → save → 新实例 load → get 还原同一顺序三列。

    断言的改动：持久化丢列、乱序、类型写错任一项都会让测试失败。
    """
    path = tmp_path / "catalog.json"
    catalog = Catalog(path)
    catalog.register("users", _columns())
    catalog.save()

    loaded = Catalog(path)
    loaded.load()

    assert loaded.get("users") == (
        ColumnDef("id", SqlType.INT),
        ColumnDef("name", SqlType.TEXT),
        ColumnDef("age", SqlType.REAL),
    )


def test_register_duplicate_table_raises_table_exists(tmp_path):
    """同一 catalog 重复登记同名表必须拒绝——断言的改动：register 静默覆盖。"""
    catalog = Catalog(tmp_path / "catalog.json")
    catalog.register("users", _columns())

    with pytest.raises(SqlError) as exc:
        catalog.register("users", _columns())

    assert exc.value.code == E_TABLE_EXISTS


def test_register_empty_columns_raises_dup_column(tmp_path):
    """空列定义不能登记——断言的改动：register 接受空 columns。"""
    catalog = Catalog(tmp_path / "catalog.json")

    with pytest.raises(SqlError) as exc:
        catalog.register("users", ())

    assert exc.value.code == E_DUP_COLUMN


def test_register_duplicate_column_names_raises_dup_column(tmp_path):
    """同名列不能登记——断言的改动：register 接受重复列。"""
    catalog = Catalog(tmp_path / "catalog.json")
    columns = (ColumnDef("id", SqlType.INT), ColumnDef("id", SqlType.TEXT))

    with pytest.raises(SqlError) as exc:
        catalog.register("users", columns)

    assert exc.value.code == E_DUP_COLUMN


def test_get_missing_table_raises_table_not_found(tmp_path):
    """查不存在的表必须报 E_TABLE_NOT_FOUND——断言的改动：get 返回 None 或 KeyError。"""
    catalog = Catalog(tmp_path / "catalog.json")

    with pytest.raises(SqlError) as exc:
        catalog.get("nobody")

    assert exc.value.code == E_TABLE_NOT_FOUND


def test_unregister_missing_table_raises_table_not_found(tmp_path):
    """注销不存在的表必须报 E_TABLE_NOT_FOUND——断言的改动：unregister 静默通过。"""
    catalog = Catalog(tmp_path / "catalog.json")

    with pytest.raises(SqlError) as exc:
        catalog.unregister("nobody")

    assert exc.value.code == E_TABLE_NOT_FOUND


def test_unregister_removes_table_and_survives_reload(tmp_path):
    """注销后 names/get 都不再看到它，且重载后仍不存在。"""
    path = tmp_path / "catalog.json"
    catalog = Catalog(path)
    catalog.register("users", _columns())
    catalog.register("orders", (ColumnDef("oid", SqlType.INT),))
    catalog.save()

    catalog.unregister("users")
    catalog.save()

    assert catalog.names() == ["orders"]
    loaded = Catalog(path)
    loaded.load()
    assert loaded.names() == ["orders"]


def test_names_returns_sorted_names(tmp_path):
    """names 返回稳定排序——断言的改动：names 顺序依赖插入序。"""
    catalog = Catalog(tmp_path / "catalog.json")
    catalog.register("zeta", _columns())
    catalog.register("alpha", _columns())

    assert catalog.names() == ["alpha", "zeta"]


def test_load_missing_file_raises_storage(tmp_path):
    """catalog 文件缺失必须报 E_STORAGE——断言的改动：load 当空库静默通过。"""
    catalog = Catalog(tmp_path / "not_there.json")

    with pytest.raises(SqlError) as exc:
        catalog.load()

    assert exc.value.code == E_STORAGE


def test_load_invalid_json_raises_storage(tmp_path):
    """JSON 语法损坏必须报 E_STORAGE——断言的改动：json 解析异常漏出。"""
    path = tmp_path / "catalog.json"
    path.write_text("{not json", encoding="utf-8")
    catalog = Catalog(path)

    with pytest.raises(SqlError) as exc:
        catalog.load()

    assert exc.value.code == E_STORAGE


def test_load_wrong_version_raises_storage(tmp_path):
    """版本号不是 1 必须报 E_STORAGE——断言的改动：load 忽略 version。"""
    path = tmp_path / "catalog.json"
    payload = {JSON_VERSION_KEY: 99, JSON_TABLES_KEY: {}}
    path.write_text(json.dumps(payload), encoding="utf-8")
    catalog = Catalog(path)

    with pytest.raises(SqlError) as exc:
        catalog.load()

    assert exc.value.code == E_STORAGE


def test_load_unknown_type_raises_storage(tmp_path):
    """catalog 里出现未知列类型必须报 E_STORAGE——断言的改动：类型名非法被放过。"""
    path = tmp_path / "catalog.json"
    payload = {
        JSON_VERSION_KEY: CATALOG_VERSION,
        JSON_TABLES_KEY: {
            "users": {"columns": [{"name": "id", JSON_TYPE_KEY: "FLOAT"}]}
        },
    }
    path.write_text(json.dumps(payload), encoding="utf-8")
    catalog = Catalog(path)

    with pytest.raises(SqlError) as exc:
        catalog.load()

    assert exc.value.code == E_STORAGE
