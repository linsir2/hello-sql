"""V1 catalog.json → V2 页式系统表迁移测试（D24）。

覆盖：迁移成功、改名、重启、sys 权威、中断恢复、损坏拒绝与旧库按需迁移。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from contracts.ast import ColumnDef, SqlType
from contracts.errors import E_STORAGE, SqlError
from storage import DatabaseServer
from storage.cache import BufferPool
from storage.constants import (
    CATALOG_FILE_NAME,
    CATALOG_VERSION,
    JSON_COLUMNS_KEY,
    JSON_NAME_KEY,
    JSON_TABLES_KEY,
    JSON_TYPE_KEY,
    JSON_VERSION_KEY,
    LEGACY_MIGRATED_FILE_NAME,
    SYS_COLUMNS_FILE_NAME,
    SYS_TABLES_FILE_NAME,
)
from storage.engine import TableEngine
from storage.pager import create_table_file


def _columns(*defs: tuple[str, str]) -> tuple[ColumnDef, ...]:
    return tuple(ColumnDef(name, SqlType(type_name)) for name, type_name in defs)


def _payload(tables: dict[str, list[tuple[str, str]]], version: int = CATALOG_VERSION):
    return {
        JSON_VERSION_KEY: version,
        JSON_TABLES_KEY: {
            name: {
                JSON_COLUMNS_KEY: [
                    {JSON_NAME_KEY: col_name, JSON_TYPE_KEY: col_type}
                    for col_name, col_type in columns
                ]
            }
            for name, columns in tables.items()
        },
    }


def _write_table(
    db_dir: Path,
    name: str,
    columns: tuple[ColumnDef, ...],
    rows: list[tuple],
) -> None:
    path = db_dir / f"{name}.table"
    create_table_file(path)
    if not rows:
        return
    pool = BufferPool(capacity=16)
    engine = TableEngine(path, columns, pool)
    for row in rows:
        engine.insert(row)
    pool.flush(path)


def _v1_db(
    db_dir: Path,
    tables: dict[str, list[tuple[str, str]]],
    *,
    data: dict[str, list[tuple]] | None = None,
    version: int = CATALOG_VERSION,
) -> None:
    db_dir.mkdir(parents=True, exist_ok=True)
    (db_dir / CATALOG_FILE_NAME).write_text(
        json.dumps(_payload(tables, version=version)), encoding="utf-8"
    )
    for name, column_defs in tables.items():
        rows = (data or {}).get(name, [])
        _write_table(db_dir, name, _columns(*column_defs), rows)


def _expect_code(call, code: str) -> None:
    with pytest.raises(SqlError) as exc:
        call()
    assert exc.value.code == code


@pytest.fixture
def data_dir(tmp_path) -> str:
    return str(tmp_path / "data")


def _main_dir(data_dir: str) -> Path:
    return Path(data_dir) / "main"


# ---- 迁移成功与改名 ----


def test_empty_v1_json_migrates_to_system_tables_and_renames_json(data_dir):
    """空 V1 目录迁移后：两张系统表存在、JSON 改名、不再有权威 JSON。"""
    root = _main_dir(data_dir)
    _v1_db(root, {})

    server = DatabaseServer(data_dir)

    assert server.list_databases() == ["main"]
    assert (root / SYS_TABLES_FILE_NAME).is_file()
    assert (root / SYS_COLUMNS_FILE_NAME).is_file()
    assert not (root / CATALOG_FILE_NAME).exists()
    assert (root / LEGACY_MIGRATED_FILE_NAME).is_file()


def test_migration_preserves_tables_columns_and_rows(data_dir):
    """V1 表结构与行数据必须原样进入 V2 页式系统表并继续可查。"""
    root = _main_dir(data_dir)
    _v1_db(
        root,
        {"users": [("id", "INT"), ("name", "TEXT"), ("flag", "BOOLEAN")]},
        data={"users": [(1, "alice", True)]},
    )

    storage = DatabaseServer(data_dir).connect("main")

    assert storage.describe("users").columns == _columns(
        ("id", "INT"), ("name", "TEXT"), ("flag", "BOOLEAN")
    )
    assert list(storage.scan("users")) == [(1, (1, "alice", True))]


def test_migrated_database_reopens_from_system_tables(data_dir):
    """迁移成功后重启不再依赖 catalog.json，数据仍可读。"""
    root = _main_dir(data_dir)
    _v1_db(root, {"users": [("id", "INT")]}, data={"users": [(7,)]})
    DatabaseServer(data_dir)

    storage = DatabaseServer(data_dir).connect("main")

    assert list(storage.scan("users")) == [(1, (7,))]
    assert not (root / CATALOG_FILE_NAME).exists()


def test_migration_rename_collision_overwrites_old_backup(data_dir):
    """改名目标已存在时按 os.replace 覆盖（迁移幂等）。"""
    root = _main_dir(data_dir)
    _v1_db(root, {})
    (root / LEGACY_MIGRATED_FILE_NAME).write_text("stale", encoding="utf-8")

    DatabaseServer(data_dir)

    assert not (root / CATALOG_FILE_NAME).exists()
    assert (root / LEGACY_MIGRATED_FILE_NAME).read_text(
        encoding="utf-8"
    ).startswith("{")


# ---- 页式权威优先 ----


def test_system_tables_are_authoritative_when_json_still_present(data_dir):
    """系统表已完整时以系统表为准；残留 JSON 只做改名收尾。"""
    root = _main_dir(data_dir)
    DatabaseServer(data_dir)  # 建出空页式目录
    # JSON 描述一张不存在的表，但系统表为空 → 必须听系统表。
    (root / CATALOG_FILE_NAME).write_text(
        json.dumps(_payload({"ghost": [("id", "INT")]})), encoding="utf-8"
    )

    server = DatabaseServer(data_dir)

    assert server.connect("main").list_tables() == []
    assert not (root / CATALOG_FILE_NAME).exists()
    assert (root / LEGACY_MIGRATED_FILE_NAME).is_file()


# ---- 中断恢复 ----


def test_partial_system_files_with_valid_json_recover_by_remigration(data_dir):
    """半成品系统表 + 有效 JSON → 清理半成品后重迁，数据保留。"""
    root = _main_dir(data_dir)
    _v1_db(
        root,
        {"users": [("id", "INT")]},
        data={"users": [(3,)]},
    )
    (root / SYS_TABLES_FILE_NAME).write_bytes(b"partial")

    storage = DatabaseServer(data_dir).connect("main")

    assert list(storage.scan("users")) == [(1, (3,))]
    assert not (root / CATALOG_FILE_NAME).exists()


def test_corrupt_complete_system_tables_with_valid_json_remigrate(data_dir):
    """两张系统表都存在但都坏，且 JSON 有效 → 视为中断迁移并重迁。"""
    root = _main_dir(data_dir)
    _v1_db(root, {"users": [("id", "INT")]}, data={"users": [(8,)]})
    (root / SYS_TABLES_FILE_NAME).write_bytes(b"broken-a")
    (root / SYS_COLUMNS_FILE_NAME).write_bytes(b"broken-b")

    storage = DatabaseServer(data_dir).connect("main")

    assert list(storage.scan("users")) == [(1, (8,))]
    assert not (root / CATALOG_FILE_NAME).exists()


def test_invalid_system_files_without_json_raise_storage(data_dir):
    """只有坏系统表、没有 JSON 可回退 → E_STORAGE。"""
    root = _main_dir(data_dir)
    root.mkdir(parents=True)
    (root / SYS_TABLES_FILE_NAME).write_bytes(b"broken")

    _expect_code(lambda: DatabaseServer(data_dir), E_STORAGE)


# ---- 损坏拒绝 ----


def test_invalid_json_without_system_tables_raises_storage_and_leaves_no_half_catalog(
    data_dir,
):
    """坏 JSON 且无系统表 → E_STORAGE，且不得留下半成品系统表。"""
    root = _main_dir(data_dir)
    root.mkdir(parents=True)
    (root / CATALOG_FILE_NAME).write_text("{not json", encoding="utf-8")

    _expect_code(lambda: DatabaseServer(data_dir), E_STORAGE)

    assert not (root / SYS_TABLES_FILE_NAME).exists()
    assert not (root / SYS_COLUMNS_FILE_NAME).exists()
    assert (root / CATALOG_FILE_NAME).is_file()


@pytest.mark.parametrize(
    "payload",
    [
        _payload({}, version=99),
        {JSON_VERSION_KEY: CATALOG_VERSION, JSON_TABLES_KEY: {"users": {"columns": []}}},
        {
            JSON_VERSION_KEY: CATALOG_VERSION,
            JSON_TABLES_KEY: {
                "users": {
                    "columns": [
                        {"name": "id", "type": "FLOAT"},
                    ]
                }
            },
        },
        {
            JSON_VERSION_KEY: CATALOG_VERSION,
            JSON_TABLES_KEY: {
                "users": {
                    "columns": [
                        {"name": "id", "type": "INT"},
                        {"name": "id", "type": "TEXT"},
                    ]
                }
            },
        },
        {
            JSON_VERSION_KEY: CATALOG_VERSION,
            JSON_TABLES_KEY: {
                "Users": {"columns": [{"name": "id", "type": "INT"}]}
            },
        },
        {
            JSON_VERSION_KEY: CATALOG_VERSION,
            JSON_TABLES_KEY: {
                "users": {"columns": [{"name": "Id", "type": "INT"}]}
            },
        },
    ],
)
def test_bad_v1_json_raises_storage(data_dir, payload):
    """版本/空列/未知类型/重复列都按 E_STORAGE 拒绝，保留 JSON。"""
    root = _main_dir(data_dir)
    root.mkdir(parents=True)
    (root / CATALOG_FILE_NAME).write_text(json.dumps(payload), encoding="utf-8")

    _expect_code(lambda: DatabaseServer(data_dir), E_STORAGE)

    assert (root / CATALOG_FILE_NAME).is_file()
    assert not (root / SYS_TABLES_FILE_NAME).exists()


def test_migration_rejects_v1_reserved_table_name(data_dir):
    """V1 里的 __sys_ 用户表名在 V2 成为保留名，迁移必须 E_STORAGE。"""
    root = _main_dir(data_dir)
    _v1_db(root, {"__sys_x": [("id", "INT")]})

    _expect_code(lambda: DatabaseServer(data_dir), E_STORAGE)

    assert (root / CATALOG_FILE_NAME).is_file()
    assert not (root / SYS_TABLES_FILE_NAME).exists()


def test_migration_rejects_orphan_table_file_and_keeps_json(data_dir):
    """JSON 之外的孤儿 .table 会让迁移失败；JSON 保留、系统表清理。"""
    root = _main_dir(data_dir)
    _v1_db(root, {"users": [("id", "INT")]}, data={"users": [(1,)]})
    (root / "ghost.table").write_bytes(b"ghost")

    _expect_code(lambda: DatabaseServer(data_dir), E_STORAGE)

    assert (root / CATALOG_FILE_NAME).is_file()
    assert not (root / SYS_TABLES_FILE_NAME).exists()
    assert not (root / SYS_COLUMNS_FILE_NAME).exists()


def test_migration_rejects_missing_table_file_and_keeps_json(data_dir):
    """JSON 引用的用户表文件缺失 → 迁移失败，JSON 保留、系统表清理。"""
    root = _main_dir(data_dir)
    root.mkdir(parents=True)
    (root / CATALOG_FILE_NAME).write_text(
        json.dumps(_payload({"users": [("id", "INT")]})), encoding="utf-8"
    )

    _expect_code(lambda: DatabaseServer(data_dir), E_STORAGE)

    assert (root / CATALOG_FILE_NAME).is_file()
    assert not (root / SYS_TABLES_FILE_NAME).exists()


# ---- 非 main 旧库按需迁移 ----


def test_legacy_shop_is_listed_and_migrates_on_connect(data_dir):
    """main 正常启动；旧 shop 先出现在列表，connect 时才迁移。"""
    DatabaseServer(data_dir)  # 建 main
    shop = Path(data_dir) / "shop"
    _v1_db(shop, {"orders": [("oid", "INT")]}, data={"orders": [(5,)]})

    server = DatabaseServer(data_dir)
    assert "shop" in server.list_databases()
    assert server.has_database("shop") is True

    storage = server.connect("shop")

    assert list(storage.scan("orders")) == [(1, (5,))]
    assert not (shop / CATALOG_FILE_NAME).exists()
    assert (shop / LEGACY_MIGRATED_FILE_NAME).is_file()
