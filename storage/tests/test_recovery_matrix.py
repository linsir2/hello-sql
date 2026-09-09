"""恢复矩阵：每种改数据操作之后都重建 DatabaseServer 验证完整可读。"""

from __future__ import annotations

import pytest

from contracts.ast import ColumnDef, SqlType
from storage import DatabaseServer


def _columns() -> tuple[ColumnDef, ColumnDef]:
    return (ColumnDef("id", SqlType.INT), ColumnDef("body", SqlType.TEXT))


@pytest.fixture
def data_dir(tmp_path) -> str:
    return str(tmp_path / "data")


def test_recovery_matrix_full_script(data_dir):
    """按脚本逐步执行，每步后立即重开并核对当前期望状态。"""
    huge = "h" * 9000
    expected: list[tuple] = []

    def storage():
        return DatabaseServer(data_dir).connect("main")

    s = storage()
    s.create_table("t", _columns())
    expected = []
    assert list(storage().scan("t")) == expected

    rid1 = s.insert("t", (1, "a"))
    expected = [(rid1, (1, "a"))]
    assert list(storage().scan("t")) == expected

    rid2 = s.insert("t", (2, "b"))
    expected = sorted(expected + [(rid2, (2, "b"))], key=lambda r: r[0])
    assert sorted(storage().scan("t"), key=lambda r: r[0]) == expected

    s.update_row("t", rid1, (1, huge))
    expected = [(rid2, (2, "b")), (rid1, (1, huge))]
    assert sorted(storage().scan("t"), key=lambda r: r[0]) == sorted(
        expected, key=lambda r: r[0]
    )

    s.update_row("t", rid1, (1, "short"))
    expected = [(rid2, (2, "b")), (rid1, (1, "short"))]
    assert sorted(storage().scan("t"), key=lambda r: r[0]) == sorted(
        expected, key=lambda r: r[0]
    )

    s.delete_row("t", rid2)
    expected = [(rid1, (1, "short"))]
    assert sorted(storage().scan("t"), key=lambda r: r[0]) == expected

    s.delete_row("t", rid1)
    expected = []
    assert list(storage().scan("t")) == expected

    s.drop_table("t")
    assert storage().list_tables() == []
