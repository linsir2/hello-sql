"""LogicalPlanBuilder 名称绑定测试：别名、限定列、JOIN 范围与结果表头。

编译器当前仍输出 V1 形状的 SelectStmt，因此这里直接手写 V2 契约对象覆盖 Builder。
"""

from __future__ import annotations

import unittest

from contracts.ast import (
    And,
    Assignment,
    Column,
    ColumnDef,
    Cmp,
    DeleteStmt,
    InsertStmt,
    JoinClause,
    JoinType,
    Literal,
    Not,
    Or,
    SelectStmt,
    SqlType,
    TableRef,
    UpdateStmt,
)
from contracts.errors import (
    E_AMBIGUOUS_COLUMN,
    E_BOOLEAN_REQUIRED,
    E_COLUMN_NOT_FOUND,
    E_DUP_TABLE_ALIAS,
    E_TABLE_NOT_FOUND,
    E_TABLE_QUALIFIER_NOT_FOUND,
    E_VALUE_COUNT,
    SqlError,
)
from contracts.storage import TableInfo
from runner.logical_plan import (
    BoundColumnRef,
    BoundComparison,
    BoundLogical,
    BoundUnaryNot,
    LogicOp,
    LogicalDelete,
    LogicalFilter,
    LogicalInsert,
    LogicalJoin,
    LogicalPlanBuilder,
    LogicalProjection,
    LogicalScan,
    LogicalUpdate,
    join_schema,
)

# 三张表覆盖左右同名 id（歧义）、四种类型、链式 JOIN 需要的第三个来源
TABLES: dict[str, tuple[ColumnDef, ...]] = {
    "users": (
        ColumnDef("id", SqlType.INT),
        ColumnDef("name", SqlType.TEXT),
        ColumnDef("flag", SqlType.BOOLEAN),
    ),
    "orders": (
        ColumnDef("id", SqlType.INT),
        ColumnDef("user_id", SqlType.INT),
        ColumnDef("amount", SqlType.REAL),
    ),
    "items": (
        ColumnDef("id", SqlType.INT),
        ColumnDef("order_id", SqlType.INT),
    ),
}


class RecordingDescribe:
    """字典驱动的 describe_table 替身，同时记录调用顺序。"""

    def __init__(self, tables: dict[str, tuple[ColumnDef, ...]] = TABLES) -> None:
        self.tables = tables
        self.calls: list[str] = []

    def __call__(self, table: str) -> TableInfo:
        self.calls.append(table)
        if table not in self.tables:
            raise SqlError(E_TABLE_NOT_FOUND, f"table not found: {table}")
        return TableInfo(name=table, columns=self.tables[table])


def _builder(tables: dict[str, tuple[ColumnDef, ...]] = TABLES) -> LogicalPlanBuilder:
    return LogicalPlanBuilder(RecordingDescribe(tables))


def _users(alias: str | None = None) -> TableRef:
    return TableRef("users", alias)


def _orders(alias: str | None = None) -> TableRef:
    return TableRef("orders", alias)


def _items(alias: str | None = None) -> TableRef:
    return TableRef("items", alias)


def _join(
    right: TableRef,
    left_column: Column,
    right_column: Column,
    kind: JoinType = JoinType.INNER,
) -> JoinClause:
    """`INNER JOIN right ON left_column = right_column` 的简写。"""
    return JoinClause(right=right, on=Cmp(left_column, "=", right_column), kind=kind)


def _select(
    columns: tuple[Column, ...] | None,
    table: TableRef,
    where=None,
    joins: tuple[JoinClause, ...] = (),
) -> SelectStmt:
    return SelectStmt(columns=columns, table=table, where=where, joins=joins)


def _projection(
    statement: SelectStmt,
    tables: dict[str, tuple[ColumnDef, ...]] = TABLES,
) -> LogicalProjection:
    """构建一条 SELECT，并断言根节点是 LogicalProjection。"""
    plan = _builder(tables).build(statement)
    assert isinstance(plan, LogicalProjection), type(plan)
    return plan


def _users_join_orders() -> SelectStmt:
    """users AS u JOIN orders AS o ON o.user_id = u.id 的 SELECT *。"""
    return _select(
        None,
        _users("u"),
        joins=(_join(_orders("o"), Column("user_id", "o"), Column("id", "u")),),
    )


class StarHeaderTest(unittest.TestCase):
    """SELECT * 的表头：单表保持原列名，JOIN 取“限定符.列名”。"""

    def test_single_table_star_headers_are_plain(self) -> None:
        plan = _projection(_select(None, _users()))
        self.assertEqual(plan.output_names, ("id", "name", "flag"))
        self.assertEqual(
            [column.name for column in plan.output_schema.columns],
            ["id", "name", "flag"],
        )
        self.assertEqual([column.index for column in plan.output_schema.columns], [0, 1, 2])
        self.assertEqual(plan.output_schema.qualifiers, ("users",))

    def test_single_table_star_child_is_scan(self) -> None:
        plan = _projection(_select(None, _users()))
        scan = plan.child
        self.assertIsInstance(scan, LogicalScan)
        self.assertEqual(scan.table, "users")
        self.assertIsNone(scan.alias)
        self.assertEqual(scan.qualifier, "users")
        self.assertEqual(scan.children, ())
        self.assertIs(scan.output_schema, scan.schema)

    def test_aliased_single_table_star_headers_stay_plain(self) -> None:
        plan = _projection(_select(None, _users("u")))
        self.assertEqual(plan.output_names, ("id", "name", "flag"))
        self.assertEqual(plan.output_schema.qualifiers, ("u",))
        scan = plan.child
        self.assertIsInstance(scan, LogicalScan)
        self.assertEqual((scan.table, scan.alias, scan.qualifier), ("users", "u", "u"))

    def test_join_star_headers_are_qualified(self) -> None:
        plan = _projection(_users_join_orders())
        self.assertEqual(
            plan.output_names,
            ("u.id", "u.name", "u.flag", "o.id", "o.user_id", "o.amount"),
        )
        self.assertEqual([column.index for column in plan.output_schema.columns], [0, 1, 2, 3, 4, 5])
        self.assertEqual(plan.output_schema.qualifiers, ("u", "o"))

    def test_unaliased_join_star_headers_use_table_names(self) -> None:
        statement = _select(
            None,
            _users(),
            joins=(
                _join(_orders(), Column("user_id", "orders"), Column("id", "users")),
            ),
        )
        plan = _projection(statement)
        self.assertEqual(
            plan.output_names,
            (
                "users.id",
                "users.name",
                "users.flag",
                "orders.id",
                "orders.user_id",
                "orders.amount",
            ),
        )


class ExplicitProjectionTest(unittest.TestCase):
    """显式投影：绑定列保留来源位置，输出层重新编号，表头按书写形式决定。"""

    def assert_code(self, code: str, fn) -> SqlError:
        with self.assertRaises(SqlError) as ctx:
            fn()
        self.assertEqual(ctx.exception.code, code)
        return ctx.exception

    def test_unqualified_projection_reorders_and_renumbers(self) -> None:
        plan = _projection(_select((Column("name"), Column("id")), _users()))
        self.assertEqual(plan.output_names, ("name", "id"))
        self.assertEqual([ref.column.index for ref in plan.columns], [1, 0])
        self.assertEqual([column.index for column in plan.output_schema.columns], [0, 1])
        self.assertEqual(
            [column.type for column in plan.output_schema.columns],
            [SqlType.TEXT, SqlType.INT],
        )

    def test_qualified_projection_header(self) -> None:
        plan = _projection(_select((Column("name", "u"),), _users("u")))
        self.assertEqual(plan.output_names, ("u.name",))
        self.assertEqual(plan.columns[0].column.qualifier, "u")
        column = plan.output_schema.columns[0]
        self.assertEqual((column.table, column.qualifier), ("users", "u"))
        self.assertEqual(column.name, "u.name")

    def test_table_name_is_out_of_scope_after_alias(self) -> None:
        error = self.assert_code(
            E_TABLE_QUALIFIER_NOT_FOUND,
            lambda: _projection(_select((Column("name", "users"),), _users("u"))),
        )
        self.assertIn("table qualifier not found: users", error.message)

    def test_unknown_column_in_projection(self) -> None:
        error = self.assert_code(
            E_COLUMN_NOT_FOUND, lambda: _projection(_select((Column("nope"),), _users()))
        )
        self.assertIn("column not found: nope", error.message)

    def test_unknown_table(self) -> None:
        error = self.assert_code(
            E_TABLE_NOT_FOUND,
            lambda: _projection(_select(None, TableRef("missing"))),
        )
        self.assertIn("table not found: missing", error.message)


class JoinStructureTest(unittest.TestCase):
    """JOIN 按书写顺序左结合，ON 只绑定在左侧累积范围加当前右表上。"""

    def assert_code(self, code: str, fn) -> SqlError:
        with self.assertRaises(SqlError) as ctx:
            fn()
        self.assertEqual(ctx.exception.code, code)
        return ctx.exception

    def test_single_join_binds_on_merged_schema(self) -> None:
        plan = _projection(_users_join_orders())
        join = plan.child
        self.assertIsInstance(join, LogicalJoin)
        self.assertIs(join.kind, JoinType.INNER)
        self.assertEqual(
            join.schema, join_schema(join.left.output_schema, join.right.output_schema)
        )
        self.assertEqual(join.children, (join.left, join.right))
        self.assertIs(join.on.op, LogicOp.AND)
        self.assertEqual(len(join.on.terms), 1)
        term = join.on.terms[0]
        self.assertEqual((term.left.column.index, term.right.column.index), (4, 0))

    def _chain(self) -> SelectStmt:
        """users AS u JOIN orders AS o ON o.user_id = u.id JOIN items AS i ON i.order_id = o.id"""
        return _select(
            (Column("name", "u"),),
            _users("u"),
            joins=(
                _join(_orders("o"), Column("user_id", "o"), Column("id", "u")),
                _join(_items("i"), Column("order_id", "i"), Column("id", "o")),
            ),
        )

    def test_chain_is_left_deep(self) -> None:
        plan = _projection(self._chain())
        outer = plan.child
        self.assertIsInstance(outer, LogicalJoin)
        self.assertIsInstance(outer.left, LogicalJoin)
        middle = outer.left
        self.assertEqual((middle.left.table, middle.left.alias), ("users", "u"))
        self.assertEqual((middle.right.table, middle.right.alias), ("orders", "o"))
        self.assertEqual((outer.right.table, outer.right.alias), ("items", "i"))
        self.assertEqual(outer.schema.qualifiers, ("u", "o", "i"))
        self.assertEqual([column.index for column in outer.schema.columns], list(range(8)))

    def test_chain_star_headers_span_the_whole_range(self) -> None:
        statement = self._chain()
        plan = _projection(
            _select(None, statement.table, joins=statement.joins)
        )
        self.assertEqual(
            plan.output_names,
            (
                "u.id",
                "u.name",
                "u.flag",
                "o.id",
                "o.user_id",
                "o.amount",
                "i.id",
                "i.order_id",
            ),
        )

    def test_second_on_binds_on_accumulated_left_side(self) -> None:
        plan = _projection(self._chain())
        term = plan.child.on.terms[0]
        # i.order_id@7 与 o.id@3 只在第二次合并后的 Schema 中同时存在
        self.assertEqual((term.left.column.index, term.right.column.index), (7, 3))

    def test_on_referencing_a_later_table_is_rejected(self) -> None:
        describe = RecordingDescribe()
        statement = _select(
            None,
            _users("u"),
            joins=(
                # 第一个 ON 就引用了尚未出现的 items
                _join(_orders("o"), Column("user_id", "o"), Column("id", "i")),
                _join(_items("i"), Column("order_id", "i"), Column("id", "o")),
            ),
        )
        with self.assertRaises(SqlError) as ctx:
            LogicalPlanBuilder(describe).build(statement)
        self.assertEqual(ctx.exception.code, E_TABLE_QUALIFIER_NOT_FOUND)
        self.assertIn("table qualifier not found: i", ctx.exception.message)
        self.assertEqual(describe.calls, ["users", "orders"])

    def test_kind_is_threaded_from_the_clause(self) -> None:
        plan = _projection(_users_join_orders())
        self.assertIs(plan.child.kind, JoinType.INNER)


class JoinResolutionTest(unittest.TestCase):
    """合并 Schema 上的限定与歧义解析。"""

    def assert_code(self, code: str, fn) -> SqlError:
        with self.assertRaises(SqlError) as ctx:
            fn()
        self.assertEqual(ctx.exception.code, code)
        return ctx.exception

    def test_ambiguous_unqualified_column(self) -> None:
        error = self.assert_code(
            E_AMBIGUOUS_COLUMN,
            lambda: _projection(_select((Column("id"),), _users("u"), joins=_users_join_orders().joins)),
        )
        self.assertIn("ambiguous column: id", error.message)
        self.assertIn("u", error.message)
        self.assertIn("o", error.message)

    def test_unknown_qualifier(self) -> None:
        error = self.assert_code(
            E_TABLE_QUALIFIER_NOT_FOUND,
            lambda: _projection(_select((Column("id", "x"),), _users("u"), joins=_users_join_orders().joins)),
        )
        self.assertIn("table qualifier not found: x", error.message)

    def test_qualifier_exists_but_column_missing(self) -> None:
        error = self.assert_code(
            E_COLUMN_NOT_FOUND,
            lambda: _projection(_select((Column("amount", "u"),), _users("u"), joins=_users_join_orders().joins)),
        )
        self.assertIn("column not found: u.amount", error.message)

    def test_unqualified_unique_column_resolves_over_join(self) -> None:
        plan = _projection(
            _select((Column("amount"),), _users("u"), joins=_users_join_orders().joins)
        )
        self.assertEqual(plan.output_names, ("amount",))
        self.assertEqual(plan.columns[0].column.index, 5)
        self.assertEqual(plan.columns[0].column.qualifier, "o")


class DuplicateQualifierTest(unittest.TestCase):
    """范围限定符唯一性：与 join_schema 兜底同码同文。"""

    def assert_code(self, code: str, fn) -> SqlError:
        with self.assertRaises(SqlError) as ctx:
            fn()
        self.assertEqual(ctx.exception.code, code)
        return ctx.exception

    def test_unaliased_self_join_is_rejected(self) -> None:
        statement = _select(
            None,
            _users(),
            joins=(_join(_users(), Column("id", "users"), Column("id", "users")),),
        )
        error = self.assert_code(
            E_DUP_TABLE_ALIAS, lambda: _projection(statement)
        )
        # 与 join_schema 的兜底消息逐字一致
        self.assertEqual(error.message, "duplicate table alias: users")

    def test_same_alias_on_both_sides_is_rejected(self) -> None:
        statement = _select(
            None,
            _users("u"),
            joins=(_join(_orders("u"), Column("user_id", "u"), Column("id", "u")),),
        )
        error = self.assert_code(E_DUP_TABLE_ALIAS, lambda: _projection(statement))
        self.assertIn("duplicate table alias: u", error.message)

    def test_alias_colliding_with_left_table_name_is_rejected(self) -> None:
        statement = _select(
            None,
            _users(),
            joins=(
                _join(
                    TableRef("orders", "users"),
                    Column("user_id", "users"),
                    Column("id", "users"),
                ),
            ),
        )
        error = self.assert_code(E_DUP_TABLE_ALIAS, lambda: _projection(statement))
        self.assertIn("users", error.message)

    def test_right_table_is_described_before_the_conflict_is_reported(self) -> None:
        describe = RecordingDescribe()
        statement = _select(
            None,
            _users(),
            joins=(_join(_users(), Column("id", "users"), Column("id", "users")),),
        )
        with self.assertRaises(SqlError) as ctx:
            LogicalPlanBuilder(describe).build(statement)
        self.assertEqual(ctx.exception.code, E_DUP_TABLE_ALIAS)
        self.assertEqual(describe.calls, ["users", "users"])

    def test_distinct_aliases_are_allowed(self) -> None:
        statement = _select(
            None,
            _users("u1"),
            joins=(_join(_users("u2"), Column("id", "u2"), Column("id", "u1")),),
        )
        plan = _projection(statement)
        self.assertEqual(plan.output_schema.qualifiers, ("u1", "u2"))
        self.assertEqual(
            plan.output_names,
            ("u1.id", "u1.name", "u1.flag", "u2.id", "u2.name", "u2.flag"),
        )


class WhereOverJoinTest(unittest.TestCase):
    """WHERE 恒位于 JOIN 之上，绑定在合并 Schema 上并保持顶层合取规范形。"""

    def assert_code(self, code: str, fn) -> SqlError:
        with self.assertRaises(SqlError) as ctx:
            fn()
        self.assertEqual(ctx.exception.code, code)
        return ctx.exception

    def _plan_with_where(self, where) -> LogicalProjection:
        statement = _users_join_orders()
        return _projection(_select(None, statement.table, where=where, joins=statement.joins))

    def test_where_sits_above_the_join(self) -> None:
        plan = self._plan_with_where(
            And(Column("flag", "u"), Cmp(Column("amount", "o"), ">", Literal(1)))
        )
        predicate = plan.child.predicate
        self.assertIsInstance(plan.child, LogicalFilter)
        self.assertIsInstance(plan.child.child, LogicalJoin)
        self.assertIs(predicate.op, LogicOp.AND)
        self.assertEqual(len(predicate.terms), 2)
        self.assertEqual(predicate.terms[0].column.index, 2)
        self.assertEqual(predicate.terms[1].left.column.index, 5)

    def test_where_resolves_unique_unqualified_column(self) -> None:
        plan = self._plan_with_where(Cmp(Column("amount"), ">", Literal(1)))
        self.assertEqual(plan.child.predicate.terms[0].left.column.index, 5)

    def test_where_unqualified_ambiguous_column_is_rejected(self) -> None:
        self.assert_code(
            E_AMBIGUOUS_COLUMN,
            lambda: self._plan_with_where(Cmp(Column("id"), "=", Literal(1))),
        )

    def test_where_unknown_qualifier_is_rejected(self) -> None:
        self.assert_code(
            E_TABLE_QUALIFIER_NOT_FOUND,
            lambda: self._plan_with_where(Cmp(Column("id", "x"), "=", Literal(1))),
        )

    def test_or_where_stays_one_conjunct(self) -> None:
        plan = self._plan_with_where(
            Or(Column("flag", "u"), Cmp(Column("amount", "o"), ">", Literal(1)))
        )
        terms = plan.child.predicate.terms
        self.assertEqual(len(terms), 1)
        self.assertIsInstance(terms[0], BoundLogical)
        self.assertIs(terms[0].op, LogicOp.OR)
        self.assertEqual(len(terms[0].terms), 2)

    def test_not_where_is_a_single_conjunct(self) -> None:
        plan = self._plan_with_where(Not(Column("flag", "u")))
        terms = plan.child.predicate.terms
        self.assertEqual(len(terms), 1)
        self.assertIsInstance(terms[0], BoundUnaryNot)

    def test_where_nested_and_or_shape_is_preserved(self) -> None:
        plan = self._plan_with_where(
            And(
                Cmp(Column("id", "u"), "=", Literal(1)),
                Or(Cmp(Column("amount", "o"), ">", Literal(1)), Column("flag", "u")),
            )
        )
        terms = plan.child.predicate.terms
        self.assertEqual(len(terms), 2)
        self.assertIsInstance(terms[0], BoundComparison)
        self.assertIs(terms[1].op, LogicOp.OR)


class PredicateBooleanTest(unittest.TestCase):
    """WHERE 与 ON 的每个 conjunct 都必须是 BOOLEAN。"""

    def assert_code(self, code: str, fn) -> SqlError:
        with self.assertRaises(SqlError) as ctx:
            fn()
        self.assertEqual(ctx.exception.code, code)
        return ctx.exception

    def test_where_non_boolean_conjunct(self) -> None:
        error = self.assert_code(
            E_BOOLEAN_REQUIRED,
            lambda: _projection(_select(None, _users(), where=Column("id"))),
        )
        self.assertIn("WHERE/ON", error.message)
        self.assertIn("INT", error.message)

    def test_on_non_boolean_conjunct_reports_where_on(self) -> None:
        # ON 是一整列（INT），不是比较式；它由 bind_conjunction 在 LogicalJoin 构造
        # 之前校验，因此上下文是 WHERE/ON，节点内的 JOIN ON 上下文只对手写节点可达
        # （见 test_logical_plans.py）
        statement = _select(
            None,
            _users("u"),
            joins=(JoinClause(right=_orders("o"), on=Column("id", "u")),),
        )
        error = self.assert_code(E_BOOLEAN_REQUIRED, lambda: _projection(statement))
        self.assertIn("WHERE/ON", error.message)
        self.assertNotIn("JOIN ON", error.message)

    def test_boolean_column_is_accepted_as_where(self) -> None:
        plan = _projection(_select(None, _users(), where=Column("flag")))
        terms = plan.child.predicate.terms
        self.assertEqual(len(terms), 1)
        self.assertIsInstance(terms[0], BoundColumnRef)
        self.assertEqual(terms[0].column.index, 2)


class DmlPathTest(unittest.TestCase):
    """INSERT / UPDATE / DELETE 的表名仍是 str，限定符等于表名。"""

    def assert_code(self, code: str, fn) -> SqlError:
        with self.assertRaises(SqlError) as ctx:
            fn()
        self.assertEqual(ctx.exception.code, code)
        return ctx.exception

    def test_insert_builds_from_str_table(self) -> None:
        plan = _builder().build(InsertStmt("users", (1, "alice", True)))
        self.assertIsInstance(plan, LogicalInsert)
        self.assertEqual(plan.table, "users")
        self.assertEqual(
            [value.type for value in plan.values],
            [SqlType.INT, SqlType.TEXT, SqlType.BOOLEAN],
        )
        self.assertEqual(plan.table_schema.qualifiers, ("users",))
        self.assertEqual(plan.children, ())

    def test_insert_value_count_mismatch(self) -> None:
        self.assert_code(
            E_VALUE_COUNT, lambda: _builder().build(InsertStmt("users", (1,)))
        )

    def test_update_builds_from_str_table(self) -> None:
        plan = _builder().build(
            UpdateStmt(
                "users",
                (Assignment("name", "bob"),),
                Cmp(Column("id"), "=", Literal(1)),
            )
        )
        self.assertIsInstance(plan, LogicalUpdate)
        self.assertEqual(plan.table, "users")
        self.assertEqual(plan.assignments[0].column.index, 1)
        self.assertIsInstance(plan.child, LogicalFilter)
        scan = plan.child.child
        self.assertIsInstance(scan, LogicalScan)
        self.assertIsNone(scan.alias)
        self.assertEqual(scan.qualifier, "users")
        self.assertEqual(plan.child.predicate.terms[0].left.column.index, 0)

    def test_update_unknown_column(self) -> None:
        self.assert_code(
            E_COLUMN_NOT_FOUND,
            lambda: _builder().build(UpdateStmt("users", (Assignment("nope", "x"),), None)),
        )

    def test_delete_builds_from_str_table(self) -> None:
        plan = _builder().build(
            DeleteStmt("users", Cmp(Column("id"), "=", Literal(1)))
        )
        self.assertIsInstance(plan, LogicalDelete)
        self.assertEqual(plan.table, "users")
        self.assertEqual(plan.output_schema.columns, ())
        scan = plan.child.child
        self.assertIsInstance(scan, LogicalScan)
        self.assertIsNone(scan.alias)

    def test_dml_where_requires_boolean(self) -> None:
        error = self.assert_code(
            E_BOOLEAN_REQUIRED,
            lambda: _builder().build(DeleteStmt("users", Column("id"))),
        )
        self.assertIn("WHERE/ON", error.message)


if __name__ == "__main__":
    unittest.main()
