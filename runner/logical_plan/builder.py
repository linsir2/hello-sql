from __future__ import annotations

from collections.abc import Callable
from typing import assert_never

from contracts.ast import (
    Assignment,
    CreateDatabaseStmt,
    CreateTableStmt,
    DeleteStmt,
    DropDatabaseStmt,
    DropTableStmt,
    Expr,
    InsertStmt,
    SelectStmt,
    Statement,
    UpdateStmt,
    UseDatabaseStmt,
)
from contracts.errors import E_VALUE_COUNT, SqlError
from contracts.storage import TableInfo
from runner.logical_plan.base import LogicalColumn, LogicalPlan, LogicalSchema
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

    def _load_schema(self, table: str) -> LogicalSchema:
        table_info = self._describe_table(table)
        return LogicalSchema(
            tuple(
                LogicalColumn(
                    table=table,
                    name=column.name,
                    index=index,
                    type=column.type,
                )
                for index, column in enumerate(table_info.columns)
            )
        )

    @staticmethod
    def _build_scan(table: str, schema: LogicalSchema) -> LogicalScan:
        return LogicalScan(table=table, schema=schema)

    @staticmethod
    def _build_filter(
        child: LogicalPlan,
        where: Expr | None,
    ) -> LogicalPlan:
        if where is None:
            return child
        return LogicalFilter(
            predicate=bind_conjunction(where, child.output_schema),
            child=child,
        )

    @staticmethod
    def _bind_projection(
        columns: tuple[str, ...] | None,
        input_schema: LogicalSchema,
    ) -> tuple[BoundColumnRef, ...]:
        if columns is None:
            return tuple(
                BoundColumnRef(column) for column in input_schema.columns
            )
        return tuple(
            BoundColumnRef(input_schema.column(name)) for name in columns
        )

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
        schema = self._load_schema(statement.table)
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
        schema = self._load_schema(statement.table)
        scan = self._build_scan(statement.table, schema)
        child = self._build_filter(scan, statement.where)
        columns = self._bind_projection(statement.columns, child.output_schema)
        return LogicalProjection(columns=columns, child=child)

    def _build_update(self, statement: UpdateStmt) -> LogicalUpdate:
        schema = self._load_schema(statement.table)
        scan = self._build_scan(statement.table, schema)
        child = self._build_filter(scan, statement.where)
        assignments = self._bind_assignments(statement.assignments, schema)
        return LogicalUpdate(
            table=statement.table,
            assignments=assignments,
            child=child,
        )

    def _build_delete(self, statement: DeleteStmt) -> LogicalDelete:
        schema = self._load_schema(statement.table)
        scan = self._build_scan(statement.table, schema)
        child = self._build_filter(scan, statement.where)
        return LogicalDelete(table=statement.table, child=child)
