# DML Executor 开发设计

## 1. 实现目标

DML 执行器消费三个逻辑计划根节点：

| 逻辑计划 | 执行器 | Storage 调用 | 结果 |
|---|---|---|---|
| `LogicalInsert` | `InsertExecutor` | `insert()` | `affected_rows=1` |
| `LogicalUpdate` | `UpdateExecutor` | `update_row()` | 实际更新行数 |
| `LogicalDelete` | `DeleteExecutor` | `delete_row()` | 实际删除行数 |

实现文件为 `runner/executor/dml.py`。三个执行器均实现
[DQL Executor](DQL-executor-dev-design.md) 中定义的 `StatementExecutor`。

## 2. 与 DQL 行执行器的关系

`LogicalUpdate.child` 和 `LogicalDelete.child` 的形态为：

```text
LogicalScan
```

或：

```text
LogicalFilter
└── LogicalScan
```

DML 不重复实现扫描和过滤，通过 `build_row_executor(plan.child)` 构建 DQL 的
`SeqScanExecutor/FilterExecutor`。child 输出 `ExecRow(row_id, values)`：

- `row_id` 用于调用 `update_row()`、`delete_row()`；
- `values` 是表的完整行，用于 UPDATE 生成替换后的整行。

## 3. InsertExecutor

### 3.1 字段

```python
@dataclass(frozen=True, slots=True)
class InsertExecutor(StatementExecutor):
    table: str
    values: tuple[BoundLiteral, ...]
```

### 3.2 执行流程

```text
BoundLiteral tuple
-> 提取每个 BoundLiteral.value
-> context.storage.insert(table, values)
-> QueryResult(affected_rows=1)
```

实现：

```python
def execute(self, context: ExecutionContext) -> QueryResult:
    context.storage.insert(
        self.table,
        tuple(value.value for value in self.values),
    )
    return QueryResult(affected_rows=1)
```

Builder 已完成值数量校验和 REAL 规范化，Executor 直接使用绑定后的值。

## 4. UpdateExecutor

### 4.1 字段

```python
@dataclass(frozen=True, slots=True)
class UpdateExecutor(StatementExecutor):
    table: str
    assignments: tuple[BoundAssignment, ...]
    child: RowExecutor
```

### 4.2 执行流程

```text
完全消费 child.rows(context)
-> 保存全部命中 ExecRow
-> 结束 Storage.scan() 迭代
-> 对每行复制 values
-> 按 assignment.column.index 替换值
-> update_row(table, row_id, complete_values)
-> QueryResult(affected_rows=命中行数)
```

实现：

```python
def execute(self, context: ExecutionContext) -> QueryResult:
    matched_rows = tuple(self.child.rows(context))

    for row in matched_rows:
        values = list(row.values)
        for assignment in self.assignments:
            values[assignment.column.index] = assignment.value.value
        context.storage.update_row(
            self.table,
            row.row_id,
            tuple(values),
        )

    return QueryResult(affected_rows=len(matched_rows))
```

必须先将 child 完全物化，再执行第一次写入，避免在 `Storage.scan()` 迭代期间修改表。每次
`update_row()` 传入完整行，而不是仅传入被修改的列。

`LogicalPlanBuilder` 已对重复赋值执行 last-write-wins，并按列索引排列 assignments；Executor
只按绑定结果覆盖对应位置。

## 5. DeleteExecutor

### 5.1 字段

```python
@dataclass(frozen=True, slots=True)
class DeleteExecutor(StatementExecutor):
    table: str
    child: RowExecutor
```

### 5.2 执行流程

```text
完全消费 child.rows(context)
-> 保存全部命中 row_id
-> 结束 Storage.scan() 迭代
-> delete_row(table, row_id)
-> QueryResult(affected_rows=命中行数)
```

实现：

```python
def execute(self, context: ExecutionContext) -> QueryResult:
    row_ids = tuple(
        row.row_id
        for row in self.child.rows(context)
    )

    for row_id in row_ids:
        context.storage.delete_row(self.table, row_id)

    return QueryResult(affected_rows=len(row_ids))
```

DELETE 只需要在物化阶段保留 `row_id`，无需保留整行 values。

## 6. DML 执行器构建

定义 DML 计划联合类型：

```python
from typing import TypeAlias, assert_never


DmlPlan: TypeAlias = LogicalInsert | LogicalUpdate | LogicalDelete
```

构建函数：

```python
def build_dml_executor(plan: DmlPlan) -> StatementExecutor:
    match plan:
        case LogicalInsert():
            return InsertExecutor(
                table=plan.table,
                values=plan.values,
            )
        case LogicalUpdate():
            return UpdateExecutor(
                table=plan.table,
                assignments=plan.assignments,
                child=build_row_executor(plan.child),
            )
        case LogicalDelete():
            return DeleteExecutor(
                table=plan.table,
                child=build_row_executor(plan.child),
            )
        case _:
            assert_never(plan)
```

## 7. 错误与影响行数

Storage 抛出的 `E_TABLE_NOT_FOUND`、`E_ROW_NOT_FOUND`、`E_VALUE_COUNT`、
`E_TYPE_MISMATCH` 和 `E_STORAGE` 直接向上传播。

影响行数规则：

| 执行结果 | affected_rows |
|---|---:|
| INSERT 成功 | 1 |
| UPDATE 无命中 | 0 |
| UPDATE 命中 N 行 | N |
| DELETE 无命中 | 0 |
| DELETE 命中 N 行 | N |

## 8. 包导出

`runner/executor/__init__.py` 增加：

```python
from runner.executor.dml import (
    DeleteExecutor,
    DmlPlan,
    InsertExecutor,
    UpdateExecutor,
    build_dml_executor,
)
```

同步加入 `__all__`：

```python
"DmlPlan",
"InsertExecutor",
"UpdateExecutor",
"DeleteExecutor",
"build_dml_executor",
```
