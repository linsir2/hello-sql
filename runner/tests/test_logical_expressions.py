"""表达式绑定的类型体系、布尔值与逻辑连接测试。"""

from __future__ import annotations

import unittest

from contracts.ast import (
    And,
    Assignment,
    Column,
    Cmp,
    ColumnDef,
    InsertStmt,
    Literal,
    Not,
    Or,
    SqlType,
    UpdateStmt,
)
from contracts.errors import (
    E_AMBIGUOUS_COLUMN,
    E_BOOLEAN_REQUIRED,
    E_TABLE_QUALIFIER_NOT_FOUND,
    E_TYPE_MISMATCH,
    SqlError,
)
from contracts.storage import TableInfo
from runner.logical_plan import (
    BoundColumnRef,
    BoundComparison,
    BoundLiteral,
    BoundLogical,
    BoundUnaryNot,
    LogicOp,
    LogicalColumn,
    LogicalPlanBuilder,
    LogicalSchema,
    bind_conjunction,
    bind_literal,
    deduce_type,
    eval_expr,
    normalize_literal,
)


def _schema(*columns: tuple[str, SqlType], table: str = "t") -> LogicalSchema:
    """按 (列名, 类型) 顺序构造单表 Schema，index 取列表下标。"""
    return LogicalSchema(
        tuple(
            LogicalColumn.of(table, name, index, type)
            for index, (name, type) in enumerate(columns)
        )
    )


# age INT / flag BOOLEAN / name TEXT：覆盖数值、布尔、文本三类比较
MIXED = _schema(("age", SqlType.INT), ("flag", SqlType.BOOLEAN), ("name", SqlType.TEXT))

# 两个布尔列，用于逻辑连接与求值
FLAGS = _schema(("x", SqlType.BOOLEAN), ("y", SqlType.BOOLEAN))


def _cmp(name: str, op: str, value) -> Cmp:
    return Cmp(Column(name), op, Literal(value))


def _out_of_range() -> BoundColumnRef:
    """永不存在的列位置：求值到它必然越界，用于验证短路。"""
    return BoundColumnRef(LogicalColumn.of("t", "ghost", 99, SqlType.BOOLEAN))


class BindLiteralTest(unittest.TestCase):
    """无目标类型时按 Python 值推断类型。"""

    def test_bool_binds_to_boolean(self) -> None:
        for value in (True, False):
            bound = bind_literal(value)
            self.assertIs(bound.type, SqlType.BOOLEAN)
            self.assertIs(bound.value, value)

    def test_int_is_not_swallowed_by_bool(self) -> None:
        # bool 是 int 子类，先判 bool 不能把普通整数吞掉
        self.assertIs(bind_literal(1).type, SqlType.INT)

    def test_other_natural_types(self) -> None:
        self.assertIs(bind_literal(1.5).type, SqlType.REAL)
        self.assertIs(bind_literal("a").type, SqlType.TEXT)


class NormalizeLiteralTest(unittest.TestCase):
    """按目标类型校验字面量。"""

    def test_boolean_accepts_bool(self) -> None:
        for value in (True, False):
            self.assertIs(
                normalize_literal(value, SqlType.BOOLEAN).type, SqlType.BOOLEAN
            )

    def test_boolean_rejects_int_and_str(self) -> None:
        for value in (1, 0, "true"):
            with self.assertRaises(SqlError) as ctx:
                normalize_literal(value, SqlType.BOOLEAN)
            self.assertEqual(ctx.exception.code, E_TYPE_MISMATCH)

    def test_int_and_real_still_reject_bool(self) -> None:
        for target in (SqlType.INT, SqlType.REAL):
            with self.assertRaises(SqlError) as ctx:
                normalize_literal(True, target)
            self.assertEqual(ctx.exception.code, E_TYPE_MISMATCH)


class BooleanComparisonTest(unittest.TestCase):
    """布尔只支持相等性比较；大小比较与跨类型比较属于类型不匹配。"""

    def assert_code(self, code: str, fn) -> None:
        with self.assertRaises(SqlError) as ctx:
            fn()
        self.assertEqual(ctx.exception.code, code)

    def test_equality_and_inequality_are_allowed(self) -> None:
        for op in ("=", "<>"):
            bound = bind_conjunction(Cmp(Column("flag"), op, Literal(True)), MIXED)
            self.assertIs(deduce_type(bound.terms[0]), SqlType.BOOLEAN)

    def test_ordering_is_rejected(self) -> None:
        for op in ("<", "<=", ">", ">="):
            self.assert_code(
                E_TYPE_MISMATCH,
                lambda op=op: bind_conjunction(
                    Cmp(Column("flag"), op, Literal(True)), MIXED
                ),
            )

    def test_boolean_vs_numeric_is_rejected(self) -> None:
        self.assert_code(
            E_TYPE_MISMATCH,
            lambda: bind_conjunction(Cmp(Column("flag"), "=", Literal(1)), MIXED),
        )

    def test_boolean_literals_compare(self) -> None:
        # 两侧均为字面量，无列引用
        bound = bind_conjunction(Cmp(Literal(True), "=", Literal(False)), MIXED)
        self.assertIs(deduce_type(bound.terms[0]), SqlType.BOOLEAN)


class LogicBindingTest(unittest.TestCase):
    """同层同类型展平，跨类型保持嵌套，AST 优先级在绑定结果中保留。"""

    def test_and_is_flattened(self) -> None:
        bound = bind_conjunction(And(And(Column("x"), Column("y")), Column("x")), FLAGS)
        self.assertIs(bound.op, LogicOp.AND)
        self.assertEqual(len(bound.terms), 3)

    def test_or_is_flattened(self) -> None:
        # OR 顶层被包进 AND 信封，展平结果在唯一 conjunct 内
        bound = bind_conjunction(Or(Or(Column("x"), Column("y")), Column("x")), FLAGS)
        self.assertEqual(len(bound.terms), 1)
        inner = bound.terms[0]
        self.assertIs(inner.op, LogicOp.OR)
        self.assertEqual(len(inner.terms), 3)

    def test_flatten_does_not_cross_operator_types(self) -> None:
        # a AND (b OR c)：OR 保持为一个 term，不被 AND 展平吞掉
        bound = bind_conjunction(And(Column("x"), Or(Column("y"), Column("x"))), FLAGS)
        self.assertEqual(len(bound.terms), 2)
        self.assertIs(bound.terms[1].op, LogicOp.OR)

    def test_ast_precedence_is_preserved(self) -> None:
        # a = 1 AND b = 2 OR c = 3 的 AST 为 Or(And(...), Cmp(...))
        node = Or(And(_cmp("age", "=", 1), _cmp("age", "=", 2)), _cmp("age", "=", 3))
        outer = bind_conjunction(node, MIXED).terms[0]
        self.assertIs(outer.op, LogicOp.OR)
        self.assertEqual(len(outer.terms), 2)
        self.assertIs(outer.terms[0].op, LogicOp.AND)
        self.assertIsInstance(outer.terms[1], BoundComparison)


class TopLevelFormTest(unittest.TestCase):
    """谓词顶层恒为 AND 信封，terms 即 conjunct 列表。"""

    def _top(self, node, schema: LogicalSchema = MIXED) -> BoundLogical:
        return bind_conjunction(node, schema)

    def test_and_top_is_kept(self) -> None:
        bound = self._top(And(_cmp("age", "=", 1), _cmp("age", "=", 2)))
        self.assertIs(bound.op, LogicOp.AND)
        self.assertEqual(len(bound.terms), 2)

    def test_single_condition_is_wrapped(self) -> None:
        bound = self._top(_cmp("age", "=", 1))
        self.assertIs(bound.op, LogicOp.AND)
        self.assertEqual(len(bound.terms), 1)
        self.assertIsInstance(bound.terms[0], BoundComparison)

    def test_or_top_becomes_one_conjunct(self) -> None:
        bound = self._top(Or(_cmp("age", "=", 1), _cmp("age", "=", 2)))
        self.assertEqual(len(bound.terms), 1)
        self.assertIs(bound.terms[0].op, LogicOp.OR)

    def test_not_top_is_wrapped(self) -> None:
        bound = self._top(Not(Column("flag")))
        self.assertEqual(len(bound.terms), 1)
        self.assertIsInstance(bound.terms[0], BoundUnaryNot)

    def test_boolean_column_top_is_wrapped(self) -> None:
        bound = self._top(Column("flag"))
        self.assertEqual(len(bound.terms), 1)
        self.assertIsInstance(bound.terms[0], BoundColumnRef)

    def test_or_is_never_split_into_separate_conjuncts(self) -> None:
        # OR 跨来源不可分别下推，只能整体作为单个 conjunct
        bound = self._top(Or(_cmp("age", "=", 1), _cmp("name", "=", "a")))
        self.assertEqual(len(bound.terms), 1)


class BooleanRequiredTest(unittest.TestCase):
    """非 BOOLEAN 的操作数在 WHERE / AND / OR / NOT 处报错。"""

    def assert_code(self, code: str, fn) -> None:
        with self.assertRaises(SqlError) as ctx:
            fn()
        self.assertEqual(ctx.exception.code, code)

    def test_non_boolean_where(self) -> None:
        for node in (Literal(1), Column("age"), Not(Column("age"))):
            self.assert_code(
                E_BOOLEAN_REQUIRED, lambda node=node: bind_conjunction(node, MIXED)
            )

    def test_non_boolean_term_inside_and(self) -> None:
        self.assert_code(
            E_BOOLEAN_REQUIRED,
            lambda: bind_conjunction(And(Column("age"), Column("flag")), MIXED),
        )

    def test_non_boolean_term_inside_or(self) -> None:
        self.assert_code(
            E_BOOLEAN_REQUIRED,
            lambda: bind_conjunction(Or(Column("age"), Column("flag")), MIXED),
        )

    def test_boolean_operands_are_accepted(self) -> None:
        # WHERE TRUE / WHERE flag 合法：比较操作数不要求 BOOLEAN，但谓词本身要求
        for node in (Literal(True), Column("flag")):
            self.assertIs(
                deduce_type(bind_conjunction(node, MIXED).terms[0]), SqlType.BOOLEAN
            )


class EvalBooleanTest(unittest.TestCase):
    """布尔列取值、逻辑短路与取反求值。"""

    ROW = (True, False)  # x, y

    def _eval(self, node):
        """对整条谓词求值：AND 顶层含多个 conjunct，不能只取 terms[0]。"""
        return eval_expr(bind_conjunction(node, FLAGS), self.ROW)

    def test_boolean_column_value(self) -> None:
        self.assertIs(self._eval(Column("x")), True)
        self.assertIs(self._eval(Column("y")), False)

    def test_and_or_not(self) -> None:
        self.assertTrue(self._eval(Or(Column("y"), Column("x"))))
        self.assertFalse(self._eval(And(Column("x"), Column("y"))))
        self.assertTrue(self._eval(Not(Column("y"))))

    def test_nested_expression(self) -> None:
        self.assertTrue(self._eval(And(Or(Column("x"), Column("y")), Column("x"))))

    def test_or_short_circuits(self) -> None:
        expr = BoundLogical(
            LogicOp.OR, (BoundLiteral(True, SqlType.BOOLEAN), _out_of_range())
        )
        self.assertTrue(eval_expr(expr, self.ROW))

    def test_and_short_circuits(self) -> None:
        expr = BoundLogical(
            LogicOp.AND, (BoundLiteral(False, SqlType.BOOLEAN), _out_of_range())
        )
        self.assertFalse(eval_expr(expr, self.ROW))


class QualifiedColumnBindingTest(unittest.TestCase):
    """列分支按限定符解析来源，未限定列靠「恰好唯一」判定。"""

    SELF_JOIN = LogicalSchema(
        (
            LogicalColumn.of("users", "id", 0, SqlType.INT, alias="u1"),
            LogicalColumn.of("users", "id", 1, SqlType.INT, alias="u2"),
        )
    )

    def assert_code(self, code: str, fn) -> None:
        with self.assertRaises(SqlError) as ctx:
            fn()
        self.assertEqual(ctx.exception.code, code)

    def test_qualifier_selects_the_source(self) -> None:
        for qualifier, index in (("u1", 0), ("u2", 1)):
            bound = bind_conjunction(
                Cmp(Column("id", qualifier), "=", Literal(1)), self.SELF_JOIN
            )
            self.assertEqual(bound.terms[0].left.column.index, index)

    def test_unknown_qualifier(self) -> None:
        self.assert_code(
            E_TABLE_QUALIFIER_NOT_FOUND,
            lambda: bind_conjunction(
                Cmp(Column("id", "nope"), "=", Literal(1)), self.SELF_JOIN
            ),
        )

    def test_unqualified_duplicate_is_ambiguous(self) -> None:
        self.assert_code(
            E_AMBIGUOUS_COLUMN,
            lambda: bind_conjunction(Cmp(Column("id"), "=", Literal(1)), self.SELF_JOIN),
        )


class BooleanColumnDmlTest(unittest.TestCase):
    """BOOLEAN 列经 Builder 走 INSERT / UPDATE 的规范化路径。"""

    def _builder(self, *columns: tuple[str, SqlType]) -> LogicalPlanBuilder:
        info = TableInfo(
            name="t", columns=tuple(ColumnDef(name, type) for name, type in columns)
        )
        return LogicalPlanBuilder(lambda _name: info)

    def assert_code(self, code: str, fn) -> None:
        with self.assertRaises(SqlError) as ctx:
            fn()
        self.assertEqual(ctx.exception.code, code)

    def test_insert_accepts_bool(self) -> None:
        for value in (True, False):
            plan = self._builder(("flag", SqlType.BOOLEAN)).build(
                InsertStmt("t", (value,))
            )
            self.assertIs(plan.values[0].type, SqlType.BOOLEAN)

    def test_insert_rejects_int_for_boolean_column(self) -> None:
        self.assert_code(
            E_TYPE_MISMATCH,
            lambda: self._builder(("flag", SqlType.BOOLEAN)).build(InsertStmt("t", (1,))),
        )

    def test_insert_rejects_bool_for_int_column(self) -> None:
        self.assert_code(
            E_TYPE_MISMATCH,
            lambda: self._builder(("age", SqlType.INT)).build(InsertStmt("t", (True,))),
        )

    def test_update_accepts_bool(self) -> None:
        plan = self._builder(("flag", SqlType.BOOLEAN)).build(
            UpdateStmt("t", (Assignment("flag", True),), None)
        )
        self.assertIs(plan.assignments[0].value.type, SqlType.BOOLEAN)

    def test_update_rejects_int_for_boolean_column(self) -> None:
        self.assert_code(
            E_TYPE_MISMATCH,
            lambda: self._builder(("flag", SqlType.BOOLEAN)).build(
                UpdateStmt("t", (Assignment("flag", 0),), None)
            ),
        )


if __name__ == "__main__":
    unittest.main()
