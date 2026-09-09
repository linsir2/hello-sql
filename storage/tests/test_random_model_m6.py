"""随机模型测试（deterministic fuzz-lite）：

每个固定种子跑一条 150 步的随机 CRUD/DDL 序列；内存模型是“真相”，
每步或定期用 scan 对照，每 20 步重建 DatabaseServer 模拟重启再全量对照。
失败可复现；若失败，先判定是产品缺陷还是测试期望写错。
"""

from __future__ import annotations

import random

import pytest

from contracts.ast import ColumnDef, SqlType
from contracts.errors import E_TABLE_EXISTS
from storage import DatabaseServer


def _random_columns(rng: random.Random) -> tuple[ColumnDef, ...]:
    kinds = [SqlType.INT, SqlType.TEXT, SqlType.REAL]
    cols: list[ColumnDef] = []
    for i in range(rng.randint(1, 3)):
        cols.append(ColumnDef(f"c{i}", kinds[rng.randrange(len(kinds))]))
    return tuple(cols)


def _random_value(
    rng: random.Random, sql_type: SqlType
) -> int | float | str:
    if sql_type is SqlType.INT:
        return rng.randint(-(2**40), 2**40)
    if sql_type is SqlType.REAL:
        if rng.random() < 0.5:
            return rng.randint(-10**6, 10**6)
        return rng.uniform(-10**6, 10**6)
    lengths = [0, 1, 100, 4068, 4069, 9000]
    return "x" * rng.choice(lengths)


def _normalize(columns: tuple[ColumnDef, ...], values):
    out = []
    for col, value in zip(columns, values):
        if col.type is SqlType.REAL:
            out.append(float(value))
        else:
            out.append(value)
    return tuple(out)


def _tables_of(storage) -> list[str]:
    return storage.list_tables()


def _assert_model_matches(storage, model) -> None:
    actual_tables = set(_tables_of(storage))
    expected_tables = set(model)
    assert actual_tables == expected_tables
    for table, (columns, rows) in model.items():
        scan_rows = sorted(storage.scan(table), key=lambda r: r[0])
        expected_rows = sorted(rows.items())
        assert scan_rows == expected_rows, f"table {table} mismatch"
        assert columns == tuple(storage.describe(table).columns)


def _run_scenario(seed: int, data_dir) -> None:
    rng = random.Random(seed)
    server = DatabaseServer(data_dir)
    storage = server.connect("main")
    model: dict[str, tuple[tuple[ColumnDef, ...], dict[int, tuple]]] = {}
    table_pool = [f"t{i}" for i in range(6)]

    def reopen() -> None:
        nonlocal storage
        storage = DatabaseServer(data_dir).connect("main")

    for step in range(150):
        available = list(model)
        roll = rng.random()
        if not available or (
            roll < 0.08 and len(available) < len(table_pool)
        ):
            name = rng.choice(
                [t for t in table_pool if t not in model]
            )
            columns = _random_columns(rng)
            storage.create_table(name, columns)
            model[name] = (columns, {})
        elif roll < 0.16:
            name = rng.choice(available)
            storage.drop_table(name)
            del model[name]
        elif roll < 0.55:
            name = rng.choice(available)
            columns, rows = model[name]
            if len(rows) >= 60:
                continue
            values = tuple(_random_value(rng, col.type) for col in columns)
            try:
                row_id = storage.insert(name, values)
            except Exception as exc:  # noqa: BLE001 - 随机序列把任何异常当缺陷
                raise AssertionError(
                    f"seed {seed} step {step}: insert raised {exc!r}"
                ) from exc
            rows[row_id] = _normalize(columns, values)
        elif roll < 0.75:
            name = rng.choice(available)
            columns, rows = model[name]
            if not rows:
                continue
            row_id = rng.choice(list(rows))
            values = tuple(_random_value(rng, col.type) for col in columns)
            try:
                storage.update_row(name, row_id, values)
            except Exception as exc:  # noqa: BLE001
                raise AssertionError(
                    f"seed {seed} step {step}: update raised {exc!r}"
                ) from exc
            rows[row_id] = _normalize(columns, values)
        else:
            name = rng.choice(available)
            columns, rows = model[name]
            if not rows:
                continue
            row_id = rng.choice(list(rows))
            try:
                storage.delete_row(name, row_id)
            except Exception as exc:  # noqa: BLE001
                raise AssertionError(
                    f"seed {seed} step {step}: delete raised {exc!r}"
                ) from exc
            del rows[row_id]

        if step % 10 == 0:
            try:
                _assert_model_matches(storage, model)
            except AssertionError:
                raise
            except Exception as exc:  # noqa: BLE001
                raise AssertionError(
                    f"seed {seed} step {step}: model check raised {exc!r}"
                ) from exc
        if step % 20 == 0 and step > 0:
            reopen()

    reopen()
    _assert_model_matches(storage, model)


@pytest.mark.parametrize("seed", list(range(1, 21)))
def test_random_scenario_matches_model(seed, tmp_path):
    """固定种子 150 步随机序列全程与模型一致（含定期重启）。"""
    _run_scenario(seed, str(tmp_path / "data"))
