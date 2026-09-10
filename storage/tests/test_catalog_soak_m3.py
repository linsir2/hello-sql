"""M3 Soak：反复 DDL + 周期重启后，内存 Catalog、系统表与物理布局一致。"""

from __future__ import annotations

import random
from pathlib import Path

import pytest

from audit_util import audit_table_file
from contracts.ast import ColumnDef, SqlType
from contracts.errors import E_TABLE_EXISTS, E_TABLE_NOT_FOUND, SqlError
from storage import DatabaseServer
from storage.constants import SYS_COLUMNS_FILE_NAME, SYS_TABLES_FILE_NAME
from storage.syscatalog import SYS_COLUMNS_COLUMNS, SYS_TABLES_COLUMNS


def _columns() -> tuple[ColumnDef, ...]:
    return (ColumnDef("id", SqlType.INT), ColumnDef("flag", SqlType.BOOLEAN))


def _expect_code(call, code: str) -> None:
    with pytest.raises(SqlError) as exc:
        call()
    assert exc.value.code == code


def test_repeated_create_drop_cycles_keep_catalog_and_system_files_auditable(
    tmp_path,
):
    """30 轮建/插/删/重建后，系统表物理审计通过且目录为空。"""
    data_dir = str(tmp_path / "data")
    server = DatabaseServer(data_dir)
    storage = server.connect("main")

    for index in range(30):
        name = f"t{index}"
        storage.create_table(name, _columns())
        row_id = storage.insert(name, (index, index % 2 == 0))
        assert list(storage.scan(name)) == [(row_id, (index, index % 2 == 0))]
        storage.drop_table(name)
        if index % 10 == 9:
            storage = DatabaseServer(data_dir).connect("main")
            assert storage.list_tables() == []

    root = Path(data_dir) / "main"
    tables_summary = audit_table_file(
        root / SYS_TABLES_FILE_NAME, SYS_TABLES_COLUMNS
    )
    columns_summary = audit_table_file(
        root / SYS_COLUMNS_FILE_NAME, SYS_COLUMNS_COLUMNS
    )
    assert tables_summary["rows"] == 0
    assert columns_summary["rows"] == 0


@pytest.mark.parametrize("seed", range(5))
def test_random_ddl_sequence_with_restarts_matches_model(seed, tmp_path):
    """随机 create/drop + 周期重启：列表、schema 与模型始终一致。"""
    rng = random.Random(seed)
    data_dir = str(tmp_path / f"data-{seed}")
    server = DatabaseServer(data_dir)
    storage = server.connect("main")
    model: dict[str, tuple[ColumnDef, ...]] = {}

    for step in range(60):
        name = f"t{rng.randrange(6)}"
        action = rng.choice(("create", "drop"))
        if action == "create":
            if name in model:
                _expect_code(
                    lambda n=name: storage.create_table(n, _columns()),
                    E_TABLE_EXISTS,
                )
            else:
                columns = (
                    _columns()
                    if rng.random() < 0.5
                    else (ColumnDef("id", SqlType.INT),)
                )
                storage.create_table(name, columns)
                model[name] = columns
        else:
            if name in model:
                storage.drop_table(name)
                del model[name]
            else:
                _expect_code(
                    lambda n=name: storage.drop_table(n), E_TABLE_NOT_FOUND
                )

        if step % 17 == 16:
            server = DatabaseServer(data_dir)
            storage = server.connect("main")

        assert storage.list_tables() == sorted(model)
        for table_name, columns in model.items():
            assert storage.describe(table_name).columns == columns

    reopened = DatabaseServer(data_dir).connect("main")
    assert reopened.list_tables() == sorted(model)
