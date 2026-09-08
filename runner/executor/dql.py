"""DQL 执行器：把 LogicalProjection/Filter/Scan 计划树转换为拉取式行流水线。

- SeqScanExecutor：读取 Storage 的整表行；
- FilterExecutor：按谓词过滤，命中行原样下传；
- ProjectionExecutor：按投影列重排行值；
- SelectExecutor：把行流水线物化为 QueryResult。
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass

from contracts.result import QueryResult
from runner.executor.base import RowExecutor, StatementExecutor
from runner.executor.context import ExecutionContext
from runner.executor.row import ExecRow
from runner.logical_plan.base import LogicalPlan, LogicalSchema
from runner.logical_plan.expressions import BoundColumnRef, BoundExpr, eval_expr
from runner.logical_plan.plans import LogicalFilter, LogicalProjection, LogicalScan


@dataclass(frozen=True, slots=True)
class SeqScanExecutor(RowExecutor):
    """顺序扫描一张表，输出 Storage 中的完整行。"""

    table: str
    schema: LogicalSchema

    @property
    def output_schema(self) -> LogicalSchema:
        return self.schema

    def rows(self, context: ExecutionContext) -> Iterator[ExecRow]:
        # Storage.scan 的 values 顺序与 LogicalScan.schema.columns 顺序一致
        for row_id, values in context.storage.scan(self.table):
            yield ExecRow(row_id=row_id, values=values)


@dataclass(frozen=True, slots=True)
class FilterExecutor(RowExecutor):
    """按谓词过滤 child 输出行，输出 Schema 与 child 相同。"""

    predicate: BoundExpr
    child: RowExecutor

    @property
    def output_schema(self) -> LogicalSchema:
        return self.child.output_schema

    def rows(self, context: ExecutionContext) -> Iterator[ExecRow]:
        # 命中行原样下传，列位置与 row_id 都不变；AND 短路由 eval_expr 负责
        for row in self.child.rows(context):
            if eval_expr(self.predicate, row.values):
                yield row


@dataclass(frozen=True, slots=True)
class ProjectionExecutor(RowExecutor):
    """按 SELECT 书写顺序重排行值，输出 Schema 的列索引已重新编号。"""

    columns: tuple[BoundColumnRef, ...]
    child: RowExecutor
    schema: LogicalSchema

    @property
    def output_schema(self) -> LogicalSchema:
        return self.schema

    def rows(self, context: ExecutionContext) -> Iterator[ExecRow]:
        # 逐项按 columns 顺序读取 child 的行值，保留重复列与书写顺序
        for row in self.child.rows(context):
            values = tuple(
                row.values[column.column.index]
                for column in self.columns
            )
            yield ExecRow(row_id=row.row_id, values=values)


@dataclass(frozen=True, slots=True)
class SelectExecutor(StatementExecutor):
    """SELECT 语句级执行器：把根算子的行流水线物化为 QueryResult。"""

    root: RowExecutor

    def execute(self, context: ExecutionContext) -> QueryResult:
        return QueryResult(
            columns=tuple(
                column.name
                for column in self.root.output_schema.columns
            ),
            rows=tuple(
                row.values
                for row in self.root.rows(context)
            ),
            affected_rows=None,
        )


# ---------- 构建 ----------


def build_row_executor(plan: LogicalPlan) -> RowExecutor:
    """把逻辑计划递归转换为行执行器；UPDATE/DELETE 也用它构建 Scan/Filter child。"""
    match plan:
        case LogicalScan():
            return SeqScanExecutor(
                table=plan.table,
                schema=plan.output_schema,
            )
        case LogicalFilter():
            return FilterExecutor(
                predicate=plan.predicate,
                child=build_row_executor(plan.child),
            )
        case LogicalProjection():
            return ProjectionExecutor(
                columns=plan.columns,
                child=build_row_executor(plan.child),
                schema=plan.output_schema,
            )
        case _:
            raise TypeError(
                f"plan cannot produce rows: {type(plan).__name__}"
            )


def build_select_executor(plan: LogicalProjection) -> SelectExecutor:
    """SELECT 语句级执行器的构建入口。"""
    return SelectExecutor(root=build_row_executor(plan))
