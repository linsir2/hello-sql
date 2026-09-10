from __future__ import annotations

from collections.abc import Callable
from typing import assert_never

from contracts.ast import (
    Assignment,
    Column,
    CreateDatabaseStmt,
    CreateTableStmt,
    DeleteStmt,
    DropDatabaseStmt,
    DropTableStmt,
    Expr,
    InsertStmt,
    SelectStmt,
    Statement,
    TableRef,
    UpdateStmt,
    UseDatabaseStmt,
)
from contracts.errors import E_DUP_TABLE_ALIAS, E_VALUE_COUNT, SqlError
from contracts.storage import TableInfo
from runner.logical_plan.base import (
    LogicalColumn,
    LogicalPlan,
    LogicalSchema,
    join_schema,
)
from runner.logical_plan.expressions import (
    BoundAssignment,
    BoundColumnRef,
    bind_conjunction,
    normalize_literal,
)
from runner.logical_plan.plans import (
    LogicalCreateDatabase,
    LogicalCreateTable,
    LogicalDelete,
    LogicalDropDatabase,
    LogicalDropTable,
    LogicalFilter,
    LogicalInsert,
    LogicalJoin,
    LogicalProjection,
    LogicalScan,
    LogicalUpdate,
    LogicalUseDatabase,
)


DescribeTable = Callable[[str], TableInfo]


class LogicalPlanBuilder:
    """把契约 AST 转换为完成名称与类型绑定的 LogicalPlan。"""

    def __init__(self, describe_table: DescribeTable) -> None:
        self._describe_table = describe_table

    def build(self, statement: Statement) -> LogicalPlan:
        """为一条 Statement 构建计划树根节点。"""
        match statement:
            case CreateDatabaseStmt():
                return LogicalCreateDatabase(name=statement.name)
            case DropDatabaseStmt():
                return LogicalDropDatabase(name=statement.name)
            case UseDatabaseStmt():
                return LogicalUseDatabase(name=statement.name)
            case CreateTableStmt():
                return LogicalCreateTable(
                    table=statement.table,
                    columns=statement.columns,
                )
            case DropTableStmt():
                return LogicalDropTable(table=statement.table)
            case InsertStmt():
                return self._build_insert(statement)
            case SelectStmt():
                return self._build_select(statement)
            case UpdateStmt():
                return self._build_update(statement)
            case DeleteStmt():
                return self._build_delete(statement)
            case _:
                assert_never(statement)

    def _build_source(self, ref: TableRef) -> tuple[LogicalSchema, LogicalScan]:
        """描述一张表，返回（完成绑定的 Schema, Scan 叶子）。

        限定符按“别名优先，否则表名”写入每一列；alias 同时保存在 Scan 上供计划树
        自描述。表不存在由 Storage.describe 抛 E_TABLE_NOT_FOUND。INSERT / UPDATE /
        DELETE 的表名是 str（契约未引入别名），由调用方包成 TableRef 走同一路径。
        """
        table_info = self._describe_table(ref.name)
        schema = LogicalSchema(
            tuple(
                LogicalColumn.of(
                    table=ref.name,
                    name=column.name,
                    index=index,
                    type=column.type,
                    alias=ref.alias,
                )
                for index, column in enumerate(table_info.columns)
            )
        )
        return schema, LogicalScan(table=ref.name, schema=schema, alias=ref.alias)

    def _build_source_range(self, statement: SelectStmt) -> LogicalPlan:
        """把 FROM 表与各 JOIN 按书写顺序构造成左深树。

        ON 只绑定在“左侧累积 Schema + 当前右表”上，引用后续表时该限定符尚不存在，
        抛 E_TABLE_QUALIFIER_NOT_FOUND；未限定列在两侧同名抛 E_AMBIGUOUS_COLUMN。
        右表限定符与已建范围相交时抛 E_DUP_TABLE_ALIAS，与 join_schema 的兜底检查
        同码同文，两处都保留：这里报错更贴近 SQL 书写位置，join_schema 保证节点不变式。
        """
        _, plan = self._build_source(statement.table)
        for join in statement.joins:
            right_schema, right_scan = self._build_source(join.right)
            overlap = set(plan.output_schema.qualifiers) & set(right_schema.qualifiers)
            if overlap:
                raise SqlError(
                    E_DUP_TABLE_ALIAS,
                    f"duplicate table alias: {', '.join(sorted(overlap))}",
                )
            merged = join_schema(plan.output_schema, right_schema)
            plan = LogicalJoin(
                left=plan,
                right=right_scan,
                on=bind_conjunction(join.on, merged),
                schema=merged,
                kind=join.kind,
            )
        return plan

    @staticmethod
    def _build_filter(
        child: LogicalPlan,
        where: Expr | None,
    ) -> LogicalPlan:
        """把 WHERE 绑定在 child 的输出 Schema 上。

        child 是 JOIN 时输出 Schema 即合并 Schema：WHERE 恒位于 JOIN 之上，
        因此未限定列在左右两侧同名时报 E_AMBIGUOUS_COLUMN。
        """
        if where is None:
            return child
        return LogicalFilter(
            predicate=bind_conjunction(where, child.output_schema),
            child=child,
        )

    @staticmethod
    def _bind_projection(
        columns: tuple[Column, ...] | None,
        input_schema: LogicalSchema,
        *,
        star_qualified: bool,
    ) -> tuple[tuple[BoundColumnRef, ...], tuple[str, ...]]:
        """绑定 SELECT 列表，返回（绑定列引用, 结果表头）。

        - columns 为 None（SELECT *）：按输入 Schema 顺序展开全列；查询含 JOIN 时
          表头取“限定符.列名”，否则取原列名（单表即使起了别名也保持原列名）；
        - 显式列表：逐项按限定符解析，书写了限定符时表头取“限定符.列名”，
          未限定取原列名。表头取解析后列的限定符，保证与 Schema 一致的小写形式。
        """
        if columns is None:
            return (
                tuple(BoundColumnRef(column) for column in input_schema.columns),
                tuple(
                    f"{column.qualifier}.{column.name}"
                    if star_qualified
                    else column.name
                    for column in input_schema.columns
                ),
            )
        refs: list[BoundColumnRef] = []
        names: list[str] = []
        for item in columns:
            column = input_schema.resolve(item.name, item.qualifier)
            refs.append(BoundColumnRef(column))
            names.append(
                f"{column.qualifier}.{column.name}" if item.qualifier else column.name
            )
        return tuple(refs), tuple(names)

    @staticmethod
    def _bind_assignments(
        assignments: tuple[Assignment, ...],
        schema: LogicalSchema,
    ) -> tuple[BoundAssignment, ...]:
        latest_values = {
            assignment.column: assignment.value for assignment in assignments
        }
        resolved = [
            (schema.column(name), value)
            for name, value in latest_values.items()
        ]
        resolved.sort(key=lambda item: item[0].index)

        return tuple(
            BoundAssignment(
                column=column,
                value=normalize_literal(value, column.type),
            )
            for column, value in resolved
        )

    def _build_insert(self, statement: InsertStmt) -> LogicalInsert:
        schema, _ = self._build_source(TableRef(statement.table))
        if len(statement.values) != len(schema.columns):
            raise SqlError(
                E_VALUE_COUNT,
                f"insert value count: {len(statement.values)} != "
                f"{len(schema.columns)}",
            )

        values = tuple(
            normalize_literal(value, column.type)
            for value, column in zip(statement.values, schema.columns)
        )
        return LogicalInsert(
            table=statement.table,
            table_schema=schema,
            values=values,
        )

    def _build_select(self, statement: SelectStmt) -> LogicalProjection:
        child = self._build_filter(
            self._build_source_range(statement),
            statement.where,
        )
        columns, names = self._bind_projection(
            statement.columns,
            child.output_schema,
            star_qualified=bool(statement.joins),
        )
        return LogicalProjection(
            columns=columns,
            output_names=names,
            child=child,
        )

    def _build_update(self, statement: UpdateStmt) -> LogicalUpdate:
        schema, scan = self._build_source(TableRef(statement.table))
        child = self._build_filter(scan, statement.where)
        assignments = self._bind_assignments(statement.assignments, schema)
        return LogicalUpdate(
            table=statement.table,
            assignments=assignments,
            child=child,
        )

    def _build_delete(self, statement: DeleteStmt) -> LogicalDelete:
        _, scan = self._build_source(TableRef(statement.table))
        child = self._build_filter(scan, statement.where)
        return LogicalDelete(table=statement.table, child=child)
