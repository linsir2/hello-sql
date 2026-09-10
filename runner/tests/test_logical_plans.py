"""LogicalPlan 节点不变式测试：列校验、顶层合取、投影表头、Scan 别名与 JOIN。"""

from __future__ import annotations

import unittest
from dataclasses import replace

from contracts.ast import Column, Cmp, JoinType, Literal, SqlType
from contracts.errors import (
    E_BOOLEAN_REQUIRED,
    E_COLUMN_NOT_FOUND,
    E_DUP_TABLE_ALIAS,
    E_TYPE_MISMATCH,
    SqlError,
)
from runner.logical_plan import (
    BoundColumnRef,
    BoundComparison,
    BoundLiteral,
    BoundLogical,
    ComparisonOp,
    LogicOp,
    LogicalColumn,
    LogicalFilter,
    LogicalJoin,
    LogicalProjection,
    LogicalScan,
    LogicalSchema,
    bind_conjunction,
    join_schema,
)


def _users(alias: str | None = None) -> LogicalSchema:
    """构造 users(id INT, name TEXT)；给了别名则限定符为别名。"""
    return LogicalSchema(
        (
            LogicalColumn.of("users", "id", 0, SqlType.INT, alias),
            LogicalColumn.of("users", "name", 1, SqlType.TEXT, alias),
        )
    )


def _scan(
    schema: LogicalSchema,
    table: str = "users",
    alias: str | None = None,
) -> LogicalScan:
    """叶子节点：输出 Schema 就是传入的表 Schema。"""
    return LogicalScan(table=table, schema=schema, alias=alias)


def _self_join() -> tuple[LogicalScan, LogicalScan, LogicalSchema]:
    """users AS u1 与 users AS u2 两侧，以及拼接后的 Schema。"""
    left = _scan(_users("u1"), alias="u1")
    right = _scan(_users("u2"), alias="u2")
    return left, right, join_schema(left.output_schema, right.output_schema)


def _bind_on(merged: LogicalSchema) -> BoundLogical:
    """按第四步 Builder 的路径绑定 ON：u1.id = u2.id。"""
    return bind_conjunction(Cmp(Column("id", "u1"), "=", Column("id", "u2")), merged)


def _true_on(schema: LogicalSchema) -> BoundLogical:
    """与列无关的合法 ON：单元素 AND 信封包一个 BOOLEAN 字面量。"""
    return bind_conjunction(Literal(True), schema)


class CheckColumnQualifierTest(unittest.TestCase):
    """列校验同时比对限定符：自连接下同名同型列不再互相误判。"""

    def assert_code(self, code: str, fn) -> SqlError:
        with self.assertRaises(SqlError) as ctx:
            fn()
        self.assertEqual(ctx.exception.code, code)
        return ctx.exception

    def test_matching_qualifier_is_accepted(self) -> None:
        schema = _users("u1")
        plan = LogicalProjection(
            columns=(BoundColumnRef(schema.resolve("id")),),
            output_names=("id",),
            child=_scan(schema, alias="u1"),
        )
        self.assertEqual(plan.output_schema.columns[0].qualifier, "u1")

    def test_qualifier_mismatch_is_rejected(self) -> None:
        # 表名、列名、index、类型全部相同，只有限定符指向另一侧
        wrong = LogicalColumn.of("users", "id", 0, SqlType.INT, alias="u2")
        error = self.assert_code(
            E_TYPE_MISMATCH,
            lambda: LogicalProjection(
                columns=(BoundColumnRef(wrong),),
                output_names=("id",),
                child=_scan(_users("u1"), alias="u1"),
            ),
        )
        self.assertIn("u2", error.message)
        self.assertIn("u1", error.message)

    def test_qualifier_is_checked_inside_filter_predicate(self) -> None:
        wrong = LogicalColumn.of("users", "id", 0, SqlType.INT, alias="u2")
        predicate = BoundLogical(
            LogicOp.AND,
            (
                BoundComparison(
                    BoundColumnRef(wrong),
                    ComparisonOp.EQ,
                    BoundLiteral(1, SqlType.INT),
                ),
            ),
        )
        self.assert_code(
            E_TYPE_MISMATCH,
            lambda: LogicalFilter(
                predicate=predicate,
                child=_scan(_users("u1"), alias="u1"),
            ),
        )

    def test_table_mismatch_is_still_checked(self) -> None:
        # of() 造不出表名不符的列，只能直接构造
        wrong = LogicalColumn("orders", "u1", "id", 0, SqlType.INT)
        self.assert_code(
            E_TYPE_MISMATCH,
            lambda: LogicalProjection(
                columns=(BoundColumnRef(wrong),),
                output_names=("id",),
                child=_scan(_users("u1"), alias="u1"),
            ),
        )

    def test_out_of_range_index_is_rejected(self) -> None:
        schema = _users()
        self.assert_code(
            E_COLUMN_NOT_FOUND,
            lambda: LogicalProjection(
                columns=(BoundColumnRef(LogicalColumn.of("users", "id", 9, SqlType.INT)),),
                output_names=("id",),
                child=_scan(schema),
            ),
        )


class FilterInvariantTest(unittest.TestCase):
    """谓词顶层恒为非空 AND 合取。"""

    def assert_code(self, code: str, fn) -> SqlError:
        with self.assertRaises(SqlError) as ctx:
            fn()
        self.assertEqual(ctx.exception.code, code)
        return ctx.exception

    def test_non_and_predicate_is_rejected(self) -> None:
        schema = _users()
        for predicate in (
            BoundLogical(LogicOp.OR, (BoundLiteral(True, SqlType.BOOLEAN),)),
            BoundLiteral(True, SqlType.BOOLEAN),
        ):
            self.assert_code(
                E_TYPE_MISMATCH,
                lambda predicate=predicate: LogicalFilter(
                    predicate=predicate, child=_scan(schema)
                ),
            )

    def test_empty_and_is_rejected(self) -> None:
        self.assert_code(
            E_TYPE_MISMATCH,
            lambda: LogicalFilter(
                predicate=BoundLogical(LogicOp.AND, ()), child=_scan(_users())
            ),
        )

    def test_valid_and_is_accepted(self) -> None:
        schema = _users()
        scan = _scan(schema)
        plan = LogicalFilter(
            predicate=bind_conjunction(Cmp(Column("id"), "=", Literal(1)), schema),
            child=scan,
        )
        self.assertIs(plan.predicate.op, LogicOp.AND)
        self.assertEqual(plan.children, (scan,))
        self.assertIs(plan.output_schema, scan.output_schema)


class ProjectionOutputNamesTest(unittest.TestCase):
    """output_names 决定结果表头，与 columns 等长且一一对应。"""

    def assert_code(self, code: str, fn) -> SqlError:
        with self.assertRaises(SqlError) as ctx:
            fn()
        self.assertEqual(ctx.exception.code, code)
        return ctx.exception

    def test_length_mismatch_is_rejected(self) -> None:
        schema = _users()
        for names in (("id", "name"), ()):
            self.assert_code(
                E_TYPE_MISMATCH,
                lambda names=names: LogicalProjection(
                    columns=(BoundColumnRef(schema.resolve("id")),),
                    output_names=names,
                    child=_scan(schema),
                ),
            )

    def test_single_table_header_keeps_column_names(self) -> None:
        schema = _users()
        plan = LogicalProjection(
            columns=(
                BoundColumnRef(schema.resolve("id")),
                BoundColumnRef(schema.resolve("name")),
            ),
            output_names=("id", "name"),
            child=_scan(schema),
        )
        columns = plan.output_schema.columns
        self.assertEqual([column.name for column in columns], ["id", "name"])
        self.assertEqual([column.index for column in columns], [0, 1])

    def test_index_renumbered_for_reordered_subset(self) -> None:
        # SELECT name, id：来源 index 为 1、0，输出重新编号为 0、1
        schema = _users()
        plan = LogicalProjection(
            columns=(
                BoundColumnRef(schema.resolve("name")),
                BoundColumnRef(schema.resolve("id")),
            ),
            output_names=("name", "id"),
            child=_scan(schema),
        )
        columns = plan.output_schema.columns
        self.assertEqual(
            [(column.name, column.index) for column in columns],
            [("name", 0), ("id", 1)],
        )
        self.assertEqual([column.type for column in columns], [SqlType.TEXT, SqlType.INT])

    def test_qualified_header_over_join_input(self) -> None:
        left, right, merged = _self_join()
        join = LogicalJoin(left=left, right=right, on=_bind_on(merged), schema=merged)
        plan = LogicalProjection(
            columns=(
                BoundColumnRef(merged.resolve("name", "u1")),
                BoundColumnRef(merged.resolve("id", "u2")),
            ),
            output_names=("u1.name", "u2.id"),
            child=join,
        )
        columns = plan.output_schema.columns
        self.assertEqual([column.name for column in columns], ["u1.name", "u2.id"])
        self.assertEqual([column.index for column in columns], [0, 1])
        self.assertEqual(
            [(column.table, column.qualifier) for column in columns],
            [("users", "u1"), ("users", "u2")],
        )
        self.assertEqual([column.type for column in columns], [SqlType.TEXT, SqlType.INT])
        self.assertEqual(plan.output_schema.qualifiers, ("u1", "u2"))

    def test_duplicate_output_columns_are_kept(self) -> None:
        schema = _users()
        plan = LogicalProjection(
            columns=(
                BoundColumnRef(schema.resolve("name")),
                BoundColumnRef(schema.resolve("name")),
            ),
            output_names=("name", "name"),
            child=_scan(schema),
        )
        columns = plan.output_schema.columns
        self.assertEqual([column.name for column in columns], ["name", "name"])
        self.assertEqual([column.index for column in columns], [0, 1])


class ScanAliasTest(unittest.TestCase):
    """alias 只让计划树自描述，解析依据始终是 Schema 列上的限定符。"""

    def test_default_alias_is_none(self) -> None:
        schema = _users()
        scan = LogicalScan(table="users", schema=schema)
        self.assertIsNone(scan.alias)
        self.assertEqual(scan.qualifier, "users")
        self.assertIs(scan.output_schema, schema)
        self.assertEqual(scan.children, ())

    def test_alias_wins_as_qualifier(self) -> None:
        scan = _scan(_users("u"), alias="u")
        self.assertEqual(scan.qualifier, "u")
        self.assertEqual(scan.output_schema.qualifiers, ("u",))

    def test_alias_is_not_used_for_resolution(self) -> None:
        # Schema 没有别名时，column("id") 仍按表名限定符解析
        scan = _scan(_users(), alias="u")
        self.assertEqual(scan.qualifier, "u")
        self.assertEqual(scan.output_schema.qualifiers, ("users",))
        self.assertEqual(scan.output_schema.resolve("id").qualifier, "users")


class JoinHappyPathTest(unittest.TestCase):
    """自连接两侧限定符不同，拼接 Schema 与 ON 的列位置一致。"""

    def test_self_join_with_aliases(self) -> None:
        left, right, merged = _self_join()
        on = _bind_on(merged)
        join = LogicalJoin(left=left, right=right, on=on, schema=merged)
        self.assertIs(join.output_schema, merged)
        self.assertEqual(join.children, (left, right))
        self.assertIs(join.kind, JoinType.INNER)
        self.assertEqual([column.index for column in merged.columns], [0, 1, 2, 3])
        self.assertIs(join.on.op, LogicOp.AND)
        self.assertEqual(
            (on.terms[0].left.column.index, on.terms[0].right.column.index),
            (0, 2),
        )

    def test_schema_equality_is_by_value(self) -> None:
        left, right, merged = _self_join()
        rebuilt = join_schema(left.output_schema, right.output_schema)
        join = LogicalJoin(
            left=left, right=right, on=_bind_on(rebuilt), schema=rebuilt
        )
        self.assertEqual(join.output_schema, merged)

    def test_filter_over_join_child(self) -> None:
        left, right, merged = _self_join()
        join = LogicalJoin(left=left, right=right, on=_bind_on(merged), schema=merged)
        predicate = bind_conjunction(Cmp(Column("id", "u1"), "=", Literal(1)), merged)
        plan = LogicalFilter(predicate=predicate, child=join)
        self.assertIs(plan.output_schema, merged)
        self.assertEqual(predicate.terms[0].left.column.index, 0)


class JoinSchemaMismatchTest(unittest.TestCase):
    """schema 必须是 join_schema(left, right) 的值等价物。"""

    def assert_code(self, code: str, fn) -> SqlError:
        with self.assertRaises(SqlError) as ctx:
            fn()
        self.assertEqual(ctx.exception.code, code)
        return ctx.exception

    def _join_with(self, schema: LogicalSchema) -> LogicalJoin:
        left, right, _ = _self_join()
        return LogicalJoin(
            left=left, right=right, on=_true_on(left.output_schema), schema=schema
        )

    def test_reversed_schema_is_rejected(self) -> None:
        left, right, _ = _self_join()
        error = self.assert_code(
            E_TYPE_MISMATCH,
            lambda: self._join_with(join_schema(right.output_schema, left.output_schema)),
        )
        self.assertIn("join schema", error.message)

    def test_single_side_schema_is_rejected(self) -> None:
        left, _, _ = _self_join()
        self.assert_code(
            E_TYPE_MISMATCH, lambda: self._join_with(left.output_schema)
        )

    def test_renumbered_schema_is_rejected(self) -> None:
        _, _, merged = _self_join()
        shifted = LogicalSchema(
            tuple(replace(column, index=column.index + 1) for column in merged.columns)
        )
        self.assert_code(E_TYPE_MISMATCH, lambda: self._join_with(shifted))


class JoinDuplicateQualifierTest(unittest.TestCase):
    """范围限定符相交由 join_schema 兜底报错，与 Builder 侧检查同码同因。"""

    def assert_code(self, code: str, fn) -> SqlError:
        with self.assertRaises(SqlError) as ctx:
            fn()
        self.assertEqual(ctx.exception.code, code)
        return ctx.exception

    def test_unaliased_self_join_is_rejected(self) -> None:
        scan = _scan(_users())
        error = self.assert_code(
            E_DUP_TABLE_ALIAS,
            lambda: LogicalJoin(
                left=scan,
                right=scan,
                on=_true_on(_users()),
                schema=_users(),
            ),
        )
        self.assertIn("users", error.message)

    def test_same_alias_on_both_sides_is_rejected(self) -> None:
        scan = _scan(_users("u1"), alias="u1")
        error = self.assert_code(
            E_DUP_TABLE_ALIAS,
            lambda: LogicalJoin(
                left=scan,
                right=scan,
                on=_true_on(_users("u1")),
                schema=_users("u1"),
            ),
        )
        self.assertIn("u1", error.message)

    def test_distinct_aliases_do_not_collide(self) -> None:
        _, _, merged = _self_join()
        self.assertEqual(merged.qualifiers, ("u1", "u2"))


class JoinOnInvariantTest(unittest.TestCase):
    """on 顶层为非空 AND 合取，每个 conjunct 可定位且结果为 BOOLEAN。"""

    def assert_code(self, code: str, fn) -> SqlError:
        with self.assertRaises(SqlError) as ctx:
            fn()
        self.assertEqual(ctx.exception.code, code)
        return ctx.exception

    def _join_with(self, on) -> LogicalJoin:
        left, right, merged = _self_join()
        return LogicalJoin(left=left, right=right, on=on, schema=merged)

    def test_non_and_on_is_rejected(self) -> None:
        for on in (
            BoundLogical(LogicOp.OR, (BoundLiteral(True, SqlType.BOOLEAN),)),
            BoundComparison(
                BoundLiteral(True, SqlType.BOOLEAN),
                ComparisonOp.EQ,
                BoundLiteral(True, SqlType.BOOLEAN),
            ),
            BoundLogical(LogicOp.AND, ()),
        ):
            self.assert_code(E_TYPE_MISMATCH, lambda on=on: self._join_with(on))

    def test_non_boolean_conjunct_is_rejected(self) -> None:
        # 列可定位但类型为 INT，只违反 BOOLEAN 这一条不变式
        left, _, merged = _self_join()
        on = BoundLogical(
            LogicOp.AND, (BoundColumnRef(merged.resolve("id", "u1")),)
        )
        error = self.assert_code(E_BOOLEAN_REQUIRED, lambda: self._join_with(on))
        self.assertIn("JOIN ON", error.message)
        self.assertIn("INT", error.message)

    def test_out_of_range_column_ref_is_rejected(self) -> None:
        # 比较结果本身是 BOOLEAN，只有 index 越界这一条被违反
        on = BoundLogical(
            LogicOp.AND,
            (
                BoundComparison(
                    BoundColumnRef(LogicalColumn.of("users", "id", 99, SqlType.INT)),
                    ComparisonOp.EQ,
                    BoundLiteral(1, SqlType.INT),
                ),
            ),
        )
        error = self.assert_code(E_COLUMN_NOT_FOUND, lambda: self._join_with(on))
        self.assertIn("id", error.message)

    def test_qualifier_mismatch_inside_on_is_rejected(self) -> None:
        on = BoundLogical(
            LogicOp.AND,
            (
                BoundComparison(
                    BoundColumnRef(
                        LogicalColumn("users", "u3", "id", 0, SqlType.INT)
                    ),
                    ComparisonOp.EQ,
                    BoundLiteral(1, SqlType.INT),
                ),
            ),
        )
        error = self.assert_code(E_TYPE_MISMATCH, lambda: self._join_with(on))
        self.assertIn("u3", error.message)

    def test_true_literal_on_is_accepted(self) -> None:
        left, right, merged = _self_join()
        join = LogicalJoin(
            left=left, right=right, on=_true_on(merged), schema=merged
        )
        self.assertIs(join.on.op, LogicOp.AND)


if __name__ == "__main__":
    unittest.main()
