# DQL Executor 开发设计

## 1. 实现目标

DQL 执行层消费以下逻辑计划链：

```text
LogicalProjection
└── LogicalFilter（可选）
    └── LogicalScan
```

对应实现三个行算子和一个 SELECT 结果执行器：

| 逻辑节点 | 执行器 | 输出 |
|---|---|---|
| `LogicalScan` | `SeqScanExecutor` | `Iterator[ExecRow]` |
| `LogicalFilter` | `FilterExecutor` | `Iterator[ExecRow]` |
| `LogicalProjection` | `ProjectionExecutor` | `Iterator[ExecRow]` |
| SELECT 根计划 | `SelectExecutor` | `QueryResult` |

## 2. 文件结构

```text
runner/executor/
├── base.py          # RowExecutor、StatementExecutor
├── dql.py           # 三个行算子、SelectExecutor、构建函数
├── row.py           # 已有 ExecRow
├── context.py       # ExecutionContext；DDL 阶段补充数据库会话字段
└── __init__.py      # 公开导出
```

## 3. 公共执行器接口

在 `base.py` 中定义两类接口：

```python
from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Iterator

from contracts.result import QueryResult
from runner.executor.context import ExecutionContext
from runner.executor.row import ExecRow
from runner.logical_plan.base import LogicalSchema


class RowExecutor(ABC):
    """产生行的内部算子。"""

    @property
    @abstractmethod
    def output_schema(self) -> LogicalSchema:
        raise NotImplementedError

    @abstractmethod
    def rows(self, context: ExecutionContext) -> Iterator[ExecRow]:
        raise NotImplementedError


class StatementExecutor(ABC):
    """执行一条完整语句并返回对外结果。"""

    @abstractmethod
    def execute(self, context: ExecutionContext) -> QueryResult:
        raise NotImplementedError
```

`RowExecutor.rows()` 使用生成器实现拉取式流水线。`StatementExecutor.execute()` 是 Runner 调用的
语句级入口，DQL、DML、DDL 根执行器统一实现该接口。

## 4. SeqScanExecutor

### 4.1 字段

```python
@dataclass(frozen=True, slots=True)
class SeqScanExecutor(RowExecutor):
    table: str
    schema: LogicalSchema
```

### 4.2 执行逻辑

```python
@property
def output_schema(self) -> LogicalSchema:
    return self.schema


def rows(self, context: ExecutionContext) -> Iterator[ExecRow]:
    for row_id, values in context.storage.scan(self.table):
        yield ExecRow(row_id=row_id, values=values)
```

`Storage.scan()` 返回的 `values` 顺序与 `LogicalScan.schema.columns` 顺序一致。`row_id` 作为执行期
内部元数据保留，不加入 Schema 和 SELECT 输出列。

## 5. FilterExecutor

### 5.1 字段

```python
@dataclass(frozen=True, slots=True)
class FilterExecutor(RowExecutor):
    predicate: BoundExpr
    child: RowExecutor
```

### 5.2 执行逻辑

```python
@property
def output_schema(self) -> LogicalSchema:
    return self.child.output_schema


def rows(self, context: ExecutionContext) -> Iterator[ExecRow]:
    for row in self.child.rows(context):
        if eval_expr(self.predicate, row.values):
            yield row
```

Filter 对 `row.values` 求值，命中时原样传递同一个 `ExecRow`，因此它不会改变列位置或丢失
`row_id`。AND 的短路逻辑由现有 `eval_expr()` 负责。

## 6. ProjectionExecutor

### 6.1 字段

```python
@dataclass(frozen=True, slots=True)
class ProjectionExecutor(RowExecutor):
    columns: tuple[BoundColumnRef, ...]
    child: RowExecutor
    schema: LogicalSchema
```

`schema` 取自 `LogicalProjection.output_schema`，其中列索引已经按投影结果重新编号。

### 6.2 执行逻辑

```python
@property
def output_schema(self) -> LogicalSchema:
    return self.schema


def rows(self, context: ExecutionContext) -> Iterator[ExecRow]:
    for row in self.child.rows(context):
        values = tuple(
            row.values[column.column.index]
            for column in self.columns
        )
        yield ExecRow(row_id=row.row_id, values=values)
```

投影索引必须作用于 child 的输入 `row.values`。按 `columns` 顺序逐项读取可以同时保证：

- SELECT 书写顺序不变；
- 重复列不去重；
- `SELECT *` 按 Builder 展开后的顺序输出；
- `row_id` 继续保留在执行期记录中。

## 7. SelectExecutor

`SelectExecutor` 将行流水线物化为契约要求的 `QueryResult`：

```python
@dataclass(frozen=True, slots=True)
class SelectExecutor(StatementExecutor):
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
```

结果表头来自根算子的 `output_schema`，因此显式列顺序、重复列和 `SELECT *` 展开结果与行值
保持一致。当前 `QueryResult.rows` 为 tuple，物化发生在 SELECT 根执行器，不发生在中间算子。

## 8. 递归构建

在 `dql.py` 中实现逻辑计划到行执行器的递归转换：

```python
def build_row_executor(plan: LogicalPlan) -> RowExecutor:
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
```

SELECT 语句级执行器的构建入口：

```python
def build_select_executor(plan: LogicalProjection) -> SelectExecutor:
    return SelectExecutor(root=build_row_executor(plan))
```

`build_row_executor()` 同时供 UPDATE、DELETE 构建其 Scan/Filter child 使用。

## 9. 执行时序

以 `SELECT name FROM users WHERE age >= 18` 为例：

```text
SelectExecutor.execute(context)
  -> ProjectionExecutor.rows(context)
     -> FilterExecutor.rows(context)
        -> SeqScanExecutor.rows(context)
           -> context.storage.scan("users")
        <- ExecRow(row_id, full_values)
     <- 仅保留谓词为 True 的 ExecRow
  <- ExecRow(row_id, projected_values)
  -> QueryResult(columns=("name",), rows=(...))
```

## 10. 包导出

`runner/executor/__init__.py` 增加：

```python
from runner.executor.base import RowExecutor, StatementExecutor
from runner.executor.dql import (
    FilterExecutor,
    ProjectionExecutor,
    SelectExecutor,
    SeqScanExecutor,
    build_row_executor,
    build_select_executor,
)
```

同步加入 `__all__`：

```python
"RowExecutor",
"StatementExecutor",
"SeqScanExecutor",
"FilterExecutor",
"ProjectionExecutor",
"SelectExecutor",
"build_row_executor",
"build_select_executor",
```
