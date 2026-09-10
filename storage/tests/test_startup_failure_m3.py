"""M3 启动/建库/迁移失败注入：失败后必须清理半成品并可重试。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from contracts.ast import ColumnDef, SqlType
from contracts.errors import E_STORAGE, SqlError
from storage import DatabaseServer
from storage import syscatalog
from storage.cache import BufferPool
from storage.catalog import Catalog
from storage.constants import (
    CATALOG_FILE_NAME,
    CATALOG_VERSION,
    JSON_COLUMNS_KEY,
    JSON_NAME_KEY,
    JSON_TABLES_KEY,
    JSON_TYPE_KEY,
    JSON_VERSION_KEY,
    SYS_COLUMNS_FILE_NAME,
    SYS_TABLES_FILE_NAME,
)
from storage.engine import TableEngine
from storage.pager import create_table_file


def _expect_code(call, code: str) -> None:
    with pytest.raises(SqlError) as exc:
        call()
    assert exc.value.code == code


def _fail_second_system_file(monkeypatch) -> None:
    """让 syscatalog 创建第二张系统表文件时抛 E_STORAGE。"""
    original = syscatalog.create_table_file
    state = {"calls": 0}

    def flaky(path):
        state["calls"] += 1
        if state["calls"] == 2:
            raise SqlError(E_STORAGE, "injected bootstrap failure")
        return original(path)

    monkeypatch.setattr(syscatalog, "create_table_file", flaky)


def test_fresh_main_bootstrap_failure_removes_partial_dir(tmp_path, monkeypatch):
    """首次 main 自举失败必须清理半成品目录，重启可正常重建。

    断言的改动：失败后留下 main/sys_tables.db，下次启动直接 E_STORAGE。
    """
    data_dir = str(tmp_path / "data")
    _fail_second_system_file(monkeypatch)

    _expect_code(lambda: DatabaseServer(data_dir), E_STORAGE)
    monkeypatch.undo()

    assert not (Path(data_dir) / "main").exists()
    assert DatabaseServer(data_dir).list_databases() == ["main"]


def test_create_database_bootstrap_failure_rolls_back_dir(tmp_path, monkeypatch):
    """create_database 自举失败不得留下 shop 半成品目录。"""
    data_dir = str(tmp_path / "data")
    server = DatabaseServer(data_dir)
    _fail_second_system_file(monkeypatch)

    _expect_code(lambda: server.create_database("shop"), E_STORAGE)
    monkeypatch.undo()

    assert not (Path(data_dir) / "shop").exists()
    server.create_database("shop")
    assert server.list_databases() == ["main", "shop"]


def test_migration_flush_failure_keeps_json_and_cleans_system_files(
    tmp_path, monkeypatch
):
    """迁移写系统表 flush 失败：保留 JSON、清掉系统表，重试可成功。"""
    root = tmp_path / "data" / "main"
    root.mkdir(parents=True)
    columns = (ColumnDef("id", SqlType.INT),)
    create_table_file(root / "users.table")
    pool = BufferPool(capacity=16)
    TableEngine(root / "users.table", columns, pool).insert((1,))
    pool.flush(root / "users.table")
    payload = {
        JSON_VERSION_KEY: CATALOG_VERSION,
        JSON_TABLES_KEY: {
            "users": {
                JSON_COLUMNS_KEY: [
                    {JSON_NAME_KEY: "id", JSON_TYPE_KEY: "INT"}
                ]
            }
        },
    }
    (root / CATALOG_FILE_NAME).write_text(json.dumps(payload), encoding="utf-8")

    def boom(self):
        raise SqlError(E_STORAGE, "injected flush failure")

    monkeypatch.setattr(Catalog, "flush", boom)
    _expect_code(lambda: DatabaseServer(str(tmp_path / "data")), E_STORAGE)
    monkeypatch.undo()

    assert (root / CATALOG_FILE_NAME).is_file()
    assert not (root / SYS_TABLES_FILE_NAME).exists()
    assert not (root / SYS_COLUMNS_FILE_NAME).exists()

    storage = DatabaseServer(str(tmp_path / "data")).connect("main")
    assert list(storage.scan("users")) == [(1, (1,))]
