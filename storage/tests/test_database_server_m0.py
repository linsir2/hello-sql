"""DatabaseServer 库级 M0 测试：库目录、catalog、main 保护、错误边界。

每个测试命名“会让它失败的生产改动”；期望值（目录名、错误码）全部字面量。
重启 = 在同一个 data_dir 上重建 DatabaseServer。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from contracts.errors import (
    E_BAD_ARG,
    E_DATABASE_EXISTS,
    E_DATABASE_IN_USE,
    E_DATABASE_NOT_FOUND,
    E_STORAGE,
    SqlError,
)
from storage import DatabaseServer, Storage
from storage.constants import CATALOG_FILE_NAME


@pytest.fixture
def data_dir(tmp_path) -> str:
    return str(tmp_path / "data")


@pytest.fixture
def server(data_dir) -> DatabaseServer:
    return DatabaseServer(data_dir)


def _reopen(data_dir: str) -> DatabaseServer:
    """重启模拟：同一数据目录上新建 DatabaseServer。"""
    return DatabaseServer(data_dir)


def _catalog_path(data_dir: str, db_name: str):
    return Path(data_dir) / db_name / CATALOG_FILE_NAME


def test_init_creates_data_dir_main_and_empty_catalog(data_dir):
    """构造必须建 data_dir/main 目录和 catalog.json，list 恒含 main。

    断言的改动：__init__ 不建目录、不初始化 main、list_databases 漏 main。
    """
    DatabaseServer(data_dir)

    assert _catalog_path(data_dir, "main").is_file()
    assert DatabaseServer(data_dir).list_databases() == ["main"]


def test_init_detects_corrupt_main_catalog(data_dir):
    """main 的 catalog 已存在但损坏时，启动必须报 E_STORAGE，不得静默重建。

    断言的改动：__init__ 忽略损坏文件或直接覆盖成空库（数据丢失）。
    """
    main_dir = _catalog_path(data_dir, "main")
    main_dir.parent.mkdir(parents=True)
    main_dir.write_text("{broken", encoding="utf-8")

    with pytest.raises(SqlError) as exc:
        DatabaseServer(data_dir)

    assert exc.value.code == E_STORAGE


def test_init_raises_storage_when_main_dir_has_no_catalog(data_dir):
    """main 目录存在但 catalog 缺失视为损坏（E_STORAGE），不是正常空库。"""
    main_dir = Path(data_dir) / "main"
    main_dir.mkdir(parents=True)

    with pytest.raises(SqlError) as exc:
        DatabaseServer(data_dir)

    assert exc.value.code == E_STORAGE


def test_create_database_creates_dir_catalog_and_survives_restart(data_dir):
    server = DatabaseServer(data_dir)

    server.create_database("shop")

    assert _catalog_path(data_dir, "shop").is_file()
    assert server.list_databases() == ["main", "shop"]
    assert _reopen(data_dir).list_databases() == ["main", "shop"]


def test_create_database_existing_raises_exists(data_dir):
    server = DatabaseServer(data_dir)
    server.create_database("shop")

    with pytest.raises(SqlError) as exc:
        server.create_database("shop")

    assert exc.value.code == E_DATABASE_EXISTS


@pytest.mark.parametrize(
    "name", ["", "Shop", "1abc", "a-b", "a b", "库"]
)
def test_create_database_rejects_invalid_names(data_dir, name):
    """非法库名必须先抛 E_BAD_ARG，且不能留下目录副作用。"""
    server = DatabaseServer(data_dir)

    with pytest.raises(SqlError) as exc:
        server.create_database(name)

    assert exc.value.code == E_BAD_ARG
    assert server.list_databases() == ["main"]


@pytest.mark.parametrize("name", ["", "Shop", "1abc", "a-b"])
def test_drop_database_rejects_invalid_names(data_dir, name):
    server = DatabaseServer(data_dir)

    with pytest.raises(SqlError) as exc:
        server.drop_database(name)

    assert exc.value.code == E_BAD_ARG


@pytest.mark.parametrize("name", ["", "Shop", "1abc", "a-b"])
def test_connect_rejects_invalid_names(data_dir, name):
    server = DatabaseServer(data_dir)

    with pytest.raises(SqlError) as exc:
        server.connect(name)

    assert exc.value.code == E_BAD_ARG


@pytest.mark.parametrize("name", ["", "Shop", "1abc", "a-b"])
def test_has_database_rejects_invalid_names(data_dir, name):
    server = DatabaseServer(data_dir)

    with pytest.raises(SqlError) as exc:
        server.has_database(name)

    assert exc.value.code == E_BAD_ARG


def test_drop_database_removes_directory_and_survives_restart(data_dir):
    server = DatabaseServer(data_dir)
    server.create_database("shop")
    shop_dir = Path(data_dir) / "shop"
    assert shop_dir.is_dir()

    server.drop_database("shop")

    assert not shop_dir.exists()
    assert server.list_databases() == ["main"]
    assert _reopen(data_dir).list_databases() == ["main"]


def test_drop_database_main_raises_in_use(data_dir):
    server = DatabaseServer(data_dir)

    with pytest.raises(SqlError) as exc:
        server.drop_database("main")

    assert exc.value.code == E_DATABASE_IN_USE


def test_drop_database_missing_raises_not_found(data_dir):
    server = DatabaseServer(data_dir)

    with pytest.raises(SqlError) as exc:
        server.drop_database("nope")

    assert exc.value.code == E_DATABASE_NOT_FOUND


def test_has_database_true_after_create_false_after_drop(data_dir):
    server = DatabaseServer(data_dir)

    server.create_database("shop")
    assert server.has_database("shop") is True

    server.drop_database("shop")
    assert server.has_database("shop") is False


def test_connect_returns_storage_bound_to_existing_db(data_dir):
    server = DatabaseServer(data_dir)

    storage = server.connect("main")

    assert isinstance(storage, Storage)


def test_connect_missing_db_raises_not_found(data_dir):
    server = DatabaseServer(data_dir)

    with pytest.raises(SqlError) as exc:
        server.connect("nope")

    assert exc.value.code == E_DATABASE_NOT_FOUND


def test_connect_corrupt_catalog_raises_storage(data_dir):
    server = DatabaseServer(data_dir)
    server.create_database("shop")
    _catalog_path(data_dir, "shop").write_text("{broken", encoding="utf-8")

    with pytest.raises(SqlError) as exc:
        server.connect("shop")

    assert exc.value.code == E_STORAGE


def test_list_databases_ignores_stray_dirs_without_catalog(data_dir):
    """只有“目录 + catalog.json 齐全”才算一个库，杂目录/文件不进列表。

    断言的改动：list_databases 把任意子目录当库返回。
    """
    server = DatabaseServer(data_dir)
    stray = Path(data_dir) / "stray"
    stray.mkdir()
    (Path(data_dir) / "notes.txt").write_text("x", encoding="utf-8")
    server.create_database("shop")

    assert server.list_databases() == ["main", "shop"]
