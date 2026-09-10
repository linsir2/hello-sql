"""LogicalSchema 列解析与 Schema 拼接测试。"""

from __future__ import annotations

import unittest

from contracts.ast import SqlType
from contracts.errors import (
    E_AMBIGUOUS_COLUMN,
    E_COLUMN_NOT_FOUND,
    E_DUP_TABLE_ALIAS,
    E_TABLE_QUALIFIER_NOT_FOUND,
    SqlError,
)
from runner.logical_plan import LogicalColumn, LogicalSchema, join_schema


def _users(alias: str | None = None) -> LogicalSchema:
    """构造 users(id INT, name TEXT)；给了别名则限定符为别名。"""
    return LogicalSchema(
        (
            LogicalColumn.of("users", "id", 0, SqlType.INT, alias),
            LogicalColumn.of("users", "name", 1, SqlType.TEXT, alias),
        )
    )


def _orders() -> LogicalSchema:
    """构造 orders(id INT, user_id INT)。"""
    return LogicalSchema(
        (
            LogicalColumn.of("orders", "id", 0, SqlType.INT),
            LogicalColumn.of("orders", "user_id", 1, SqlType.INT),
        )
    )


class ResolveTest(unittest.TestCase):
    """resolve 的六种输入组合，检查顺序为先限定符、后列名。"""

    def assert_code(self, code: str, fn) -> None:
        with self.assertRaises(SqlError) as ctx:
            fn()
        self.assertEqual(ctx.exception.code, code)

    def test_qualified_hit(self) -> None:
        column = _users().resolve("name", "users")
        self.assertEqual((column.qualifier, column.name, column.index), ("users", "name", 1))

    def test_qualifier_not_in_schema(self) -> None:
        self.assert_code(
            E_TABLE_QUALIFIER_NOT_FOUND,
            lambda: _users().resolve("id", "orders"),
        )

    def test_qualifier_exists_but_column_missing(self) -> None:
        self.assert_code(
            E_COLUMN_NOT_FOUND,
            lambda: _users().resolve("amount", "users"),
        )

    def test_qualifier_is_checked_before_column(self) -> None:
        # name 确实存在，但限定符 x 不在范围内，应先报限定符错误
        self.assert_code(
            E_TABLE_QUALIFIER_NOT_FOUND,
            lambda: _users().resolve("name", "x"),
        )

    def test_unqualified_unique_hit(self) -> None:
        self.assertEqual(_users().resolve("id").name, "id")

    def test_unqualified_zero_match(self) -> None:
        self.assert_code(E_COLUMN_NOT_FOUND, lambda: _users().resolve("nope"))

    def test_unqualified_ambiguous(self) -> None:
        merged = join_schema(_users(), _orders())
        self.assert_code(E_AMBIGUOUS_COLUMN, lambda: merged.resolve("id"))

    def test_column_is_unqualified_shorthand(self) -> None:
        self.assertEqual(_users().column("id"), _users().resolve("id"))


class QualifiersTest(unittest.TestCase):
    """qualifiers 按首次出现顺序去重，has_qualifier 判断来源范围。"""

    def test_order_and_dedup(self) -> None:
        # 同一来源的多个列只贡献一次限定符
        self.assertEqual(_users().qualifiers, ("users",))
        self.assertEqual(join_schema(_users(), _orders()).qualifiers, ("users", "orders"))

    def test_alias_replaces_table_name(self) -> None:
        self.assertEqual(_users(alias="u1").qualifiers, ("u1",))

    def test_has_qualifier(self) -> None:
        schema = _users()
        self.assertTrue(schema.has_qualifier("users"))
        self.assertFalse(schema.has_qualifier("orders"))


class JoinSchemaTest(unittest.TestCase):
    """拼接规则：左列在前、右列在后、index 重编号、限定符相交报错。"""

    def test_column_order_is_left_then_right(self) -> None:
        merged = join_schema(_users(), _orders())
        self.assertEqual(
            [(col.qualifier, col.name) for col in merged.columns],
            [
                ("users", "id"),
                ("users", "name"),
                ("orders", "id"),
                ("orders", "user_id"),
            ],
        )

    def test_index_is_renumbered_from_zero(self) -> None:
        merged = join_schema(_users(), _orders())
        self.assertEqual([col.index for col in merged.columns], [0, 1, 2, 3])

    def test_table_qualifier_name_type_are_preserved(self) -> None:
        merged = join_schema(_users(alias="u1"), _orders())
        self.assertEqual(
            [(col.table, col.qualifier, col.type) for col in merged.columns],
            [
                ("users", "u1", SqlType.INT),
                ("users", "u1", SqlType.TEXT),
                ("orders", "orders", SqlType.INT),
                ("orders", "orders", SqlType.INT),
            ],
        )

    def test_duplicate_qualifier_is_rejected(self) -> None:
        with self.assertRaises(SqlError) as ctx:
            join_schema(_users(), _users())
        self.assertEqual(ctx.exception.code, E_DUP_TABLE_ALIAS)
        self.assertIn("users", ctx.exception.message)

    def test_aliased_sides_do_not_collide(self) -> None:
        # users AS u1 JOIN users AS u2：物理表相同、限定符不同，允许拼接
        merged = join_schema(_users(alias="u1"), _users(alias="u2"))
        self.assertEqual(merged.qualifiers, ("u1", "u2"))

    def test_resolution_disambiguates_after_join(self) -> None:
        merged = join_schema(_users(), _orders())
        self.assertEqual(merged.resolve("id", "orders").index, 2)
        self.assertEqual(merged.resolve("id", "users").index, 0)


if __name__ == "__main__":
    unittest.main()
