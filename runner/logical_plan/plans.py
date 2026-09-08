from __future__ import annotations

from dataclasses import dataclass

from contracts.ast import ColumnDef
from contracts.errors import E_COLUMN_NOT_FOUND, E_TYPE_MISMATCH, E_VALUE_COUNT, SqlError

from runner.logical_plan.base import (
    EMPTY_SCHEMA,
    LogicalColumn,
    LogicalPlan,
    LogicalSchema,
)
from runner.logical_plan.expressions import (
    BoundArith,
    BoundAssignment,
    BoundCast,
    BoundColumnRef,
    BoundComparison,
    BoundExpr,
    BoundLiteral,
    BoundLogical,
    BoundUnaryNot,
)

# ---------- 查询节点 ----------


@dataclass(frozen=True, slots=True)
class LogicalScan(LogicalPlan):
    """读取一张表：叶子节点，输出 Schema 为表的完整 Schema。

    只声明“读哪张表”，物理计划可以把它映射为 SeqScan/IndexScan。
    """

    table: str
    schema: LogicalSchema

    @property
    def children(self) -> tuple[LogicalPlan, ...]:
        return ()

    @property
    def output_schema(self) -> LogicalSchema:
        return self.schema


@dataclass(frozen=True, slots=True)
class LogicalFilter(LogicalPlan):
    """按谓词过滤 child 输出行；输出 Schema 与 child 相同。

    谓词顶层恒为 BoundLogical(AND) 且 terms 非空；其中的列引用都必须能在
    child 的输出 Schema 中按 index 定位。
    """

    predicate: BoundExpr
    child: LogicalPlan

    def __post_init__(self) -> None:
        if not isinstance(self.predicate, BoundLogical) or not self.predicate.terms:
            raise SqlError(
                E_TYPE_MISMATCH,
                "filter predicate must be a non-empty AND conjunction",
            )
        _check_expr_columns(self.predicate, self.child.output_schema)

    @property
    def children(self) -> tuple[LogicalPlan, ...]:
        return (self.child,)

    @property
    def output_schema(self) -> LogicalSchema:
        return self.child.output_schema


@dataclass(frozen=True, slots=True)
class LogicalProjection(LogicalPlan):
    """按 SELECT 书写顺序投影：输出 Schema 依 columns 重新编号，允许重复列。

    SELECT name, name FROM users 在 V1 中合法，所以输出列不去重；`SELECT *`
    由 Builder 展开为全列后仍保留本节点，使所有 SELECT 都有一致的根。
    """

    columns: tuple[BoundColumnRef, ...]
    child: LogicalPlan

    def __post_init__(self) -> None:
        schema = self.child.output_schema
        for col in self.columns:
            _check_column(col.column, schema)

    @property
    def children(self) -> tuple[LogicalPlan, ...]:
        return (self.child,)

    @property
    def output_schema(self) -> LogicalSchema:
        # 输出列沿用原表/名/类型，位置从 0 重新编号
        return LogicalSchema(
            tuple(
                LogicalColumn(
                    table=col.column.table,
                    name=col.column.name,
                    index=i,
                    type=col.column.type,
                )
                for i, col in enumerate(self.columns)
            )
        )


# ---------- DDL 与 DML 节点 ----------


@dataclass(frozen=True, slots=True)
class LogicalCreateTable(LogicalPlan):
    """建表：叶子节点，列信息原样保留（是否已存在由执行阶段交给 Storage）。"""

    table: str
    columns: tuple[ColumnDef, ...]

    @property
    def children(self) -> tuple[LogicalPlan, ...]:
        return ()

    @property
    def output_schema(self) -> LogicalSchema:
        return EMPTY_SCHEMA


@dataclass(frozen=True, slots=True)
class LogicalDropTable(LogicalPlan):
    """删表：叶子节点；表是否存在由执行阶段交给 Storage 检查。"""

    table: str

    @property
    def children(self) -> tuple[LogicalPlan, ...]:
        return ()

    @property
    def output_schema(self) -> LogicalSchema:
        return EMPTY_SCHEMA


@dataclass(frozen=True, slots=True)
class LogicalInsert(LogicalPlan):
    """单行插入：值已绑定为 BoundLiteral，数量与类型必须与表 Schema 一致。"""

    table: str
    table_schema: LogicalSchema
    values: tuple[BoundLiteral, ...]

    def __post_init__(self) -> None:
        if len(self.values) != len(self.table_schema.columns):
            raise SqlError(
                E_VALUE_COUNT,
                f"insert value count: {len(self.values)} != "
                f"{len(self.table_schema.columns)}",
            )
        for value, col in zip(self.values, self.table_schema.columns):
            if value.type != col.type:
                raise SqlError(
                    E_TYPE_MISMATCH,
                    f"value {value.value!r} is not a valid {col.type.value} "
                    f"value for column {col.name}",
                )

    @property
    def children(self) -> tuple[LogicalPlan, ...]:
        return ()

    @property
    def output_schema(self) -> LogicalSchema:
        return EMPTY_SCHEMA


@dataclass(frozen=True, slots=True)
class LogicalUpdate(LogicalPlan):
    """整行替换前的一次 UPDATE：child 先找出命中行，执行期收集 row_id 后写表。

    目标表必须与 child 底层的 LogicalScan 一致；assignments
    按 Builder 排好的顺序（last-write-wins 后按列序号排列）保存。
    """

    table: str
    assignments: tuple[BoundAssignment, ...]
    child: LogicalPlan

    def __post_init__(self) -> None:
        if _scan_under(self.child).table != self.table:
            raise SqlError(
                E_TYPE_MISMATCH,
                f"update target {self.table!r} does not match its scan",
            )
        schema = self.child.output_schema
        for assignment in self.assignments:
            _check_column(assignment.column, schema)
            if assignment.value.type != assignment.column.type:
                raise SqlError(
                    E_TYPE_MISMATCH,
                    f"assignment value type mismatch for column {assignment.column.name}",
                )

    @property
    def children(self) -> tuple[LogicalPlan, ...]:
        return (self.child,)

    @property
    def output_schema(self) -> LogicalSchema:
        return EMPTY_SCHEMA


@dataclass(frozen=True, slots=True)
class LogicalDelete(LogicalPlan):
    """删除命中行：与 LogicalUpdate 同构，child 先找行、执行期收集后删除。"""

    table: str
    child: LogicalPlan

    def __post_init__(self) -> None:
        if _scan_under(self.child).table != self.table:
            raise SqlError(
                E_TYPE_MISMATCH,
                f"delete target {self.table!r} does not match its scan",
            )

    @property
    def children(self) -> tuple[LogicalPlan, ...]:
        return (self.child,)

    @property
    def output_schema(self) -> LogicalSchema:
        return EMPTY_SCHEMA


# ---------- 不变式校验辅助 ----------


def _scan_under(plan: LogicalPlan) -> LogicalScan:
    """沿一元链向下找到底层的 LogicalScan（只允许 Scan / Filter）。"""
    while isinstance(plan, LogicalFilter):
        plan = plan.child
    if isinstance(plan, LogicalScan):
        return plan
    raise SqlError(
        E_TYPE_MISMATCH, "UPDATE/DELETE child must be Scan or Filter over a Scan"
    )


def _check_column(column: LogicalColumn, schema: LogicalSchema) -> None:
    """校验一个列引用能在 schema 中按 index 定位，且名称、类型一致。"""
    if not 0 <= column.index < len(schema.columns):
        raise SqlError(E_COLUMN_NOT_FOUND, f"column not found: {column.name}")
    expect = schema.columns[column.index]
    if (
        expect.table != column.table
        or expect.name != column.name
        or expect.type != column.type
    ):
        raise SqlError(
            E_TYPE_MISMATCH,
            f"bound column mismatch: {column.table}.{column.name}@"
            f"{column.index}({column.type.value}) vs schema {expect.table}."
            f"{expect.name}({expect.type.value})",
        )


def _check_expr_columns(expr: BoundExpr, schema: LogicalSchema) -> None:
    """递归校验表达式中所有列引用都能在 schema 中定位。"""
    match expr:
        case BoundColumnRef():
            _check_column(expr.column, schema)
        case BoundComparison():
            _check_expr_columns(expr.left, schema)
            _check_expr_columns(expr.right, schema)
        case BoundLogical():
            for term in expr.terms:
                _check_expr_columns(term, schema)
        case BoundUnaryNot():
            _check_expr_columns(expr.operand, schema)
        case BoundArith():
            _check_expr_columns(expr.left, schema)
            _check_expr_columns(expr.right, schema)
        case BoundCast():
            _check_expr_columns(expr.expr, schema)
        case BoundLiteral():
            pass
