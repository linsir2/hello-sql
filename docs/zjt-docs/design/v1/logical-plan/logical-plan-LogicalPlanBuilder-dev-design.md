# LogicalPlanBuilder 开发设计

## 1. 代码落点

```text
runner/logical_plan/
├── builder.py       # 新增：Statement -> LogicalPlan
├── plans.py         # 新增三个数据库命令计划节点
└── __init__.py      # 导出 Builder 和新增节点
```

`LogicalPlanBuilder` 覆盖 `contracts.ast.Statement` 联合类型中的全部九种语句：

| Statement | 计划根节点 |
|---|---|
| `CreateDatabaseStmt` | `LogicalCreateDatabase` |
| `DropDatabaseStmt` | `LogicalDropDatabase` |
| `UseDatabaseStmt` | `LogicalUseDatabase` |
| `CreateTableStmt` | `LogicalCreateTable` |
| `DropTableStmt` | `LogicalDropTable` |
| `InsertStmt` | `LogicalInsert` |
| `SelectStmt` | `LogicalProjection` |
| `UpdateStmt` | `LogicalUpdate` |
| `DeleteStmt` | `LogicalDelete` |

## 2. 补充数据库命令计划节点

当前 `plans.py` 没有对应三种数据库 Statement 的节点，需要加入：

```python
@dataclass(frozen=True, slots=True)
class LogicalCreateDatabase(LogicalPlan):
    name: str

    @property
    def children(self) -> tuple[LogicalPlan, ...]:
        return ()

    @property
    def output_schema(self) -> LogicalSchema:
        return EMPTY_SCHEMA


@dataclass(frozen=True, slots=True)
class LogicalDropDatabase(LogicalPlan):
    name: str

    @property
    def children(self) -> tuple[LogicalPlan, ...]:
        return ()

    @property
    def output_schema(self) -> LogicalSchema:
        return EMPTY_SCHEMA


@dataclass(frozen=True, slots=True)
class LogicalUseDatabase(LogicalPlan):
    name: str

    @property
    def children(self) -> tuple[LogicalPlan, ...]:
        return ()

    @property
    def output_schema(self) -> LogicalSchema:
        return EMPTY_SCHEMA
```

数据库存在性、当前数据库不可删除、`USE` 后替换当前 Storage 连接，都由后续执行阶段处理。

## 3. Builder 依赖与接口

Builder 只依赖当前 Storage 的 `describe(table) -> TableInfo` 能力。通过回调注入，避免
`runner` 导入 `storage` 实现类：

```python
from collections.abc import Callable

from contracts.ast import Statement
from contracts.storage import TableInfo
from runner.logical_plan.base import LogicalPlan


DescribeTable = Callable[[str], TableInfo]


class LogicalPlanBuilder:
    def __init__(self, describe_table: DescribeTable) -> None:
        self._describe_table = describe_table

    def build(self, statement: Statement) -> LogicalPlan:
        ...
```

Runner 的调用方式：

```python
plan = LogicalPlanBuilder(current_storage.describe).build(statement)
```

每次执行 SQL 时使用当前 Storage 的 `describe`。执行 `USE` 并切换连接后，下一条语句自然使用
新数据库的 Catalog。

## 4. `builder.py` 实现

```python
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
    def __init__(self, describe_table: DescribeTable) -> None:
        self._describe_table = describe_table

    def build(self, statement: Statement) -> LogicalPlan:
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
                BoundColumnRef(column)
                for column in input_schema.columns
            )
        return tuple(
            BoundColumnRef(input_schema.column(name))
            for name in columns
        )

    @staticmethod
    def _bind_assignments(
        assignments: tuple[Assignment, ...],
        schema: LogicalSchema,
    ) -> tuple[BoundAssignment, ...]:
        latest_values = {
            assignment.column: assignment.value
            for assignment in assignments
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
        columns = self._bind_projection(
            statement.columns,
            child.output_schema,
        )
        return LogicalProjection(columns=columns, child=child)

    def _build_update(self, statement: UpdateStmt) -> LogicalUpdate:
        schema = self._load_schema(statement.table)
        scan = self._build_scan(statement.table, schema)
        child = self._build_filter(scan, statement.where)
        assignments = self._bind_assignments(
            statement.assignments,
            schema,
        )
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
```

## 5. 关键构建规则

### 5.1 Schema 构建

`_load_schema()` 对 `TableInfo.columns` 使用 `enumerate()`，生成：

```text
LogicalColumn(table, name, index, type)
```

`index` 必须与 `Storage.scan()` 返回的 values 元组位置一致。同一条 Statement 只调用一次
`describe()`，Scan、WHERE、Projection 和 assignment 绑定共同使用这一份 Schema。

### 5.2 WHERE

SELECT、UPDATE、DELETE 共用 `_build_filter()`：

```text
where is None -> LogicalScan
where 存在    -> LogicalFilter -> LogicalScan
```

`bind_conjunction(where, child.output_schema)` 完成：

- WHERE 列名到 `LogicalColumn` 的绑定；
- 比较两侧类型协调；
- INT 到 REAL 的隐式转换；
- 单比较包装为顶层 `BoundLogical(AND, ...)`；
- 多个 AND 条件展平。

### 5.3 SELECT Projection

`columns is None` 表示 `SELECT *`，按 `input_schema.columns` 顺序展开。显式列按 Statement 中的
顺序逐个绑定，重复列原样保留。

SELECT 固定生成 Projection 根节点：

```text
LogicalProjection
└── LogicalFilter（可选）
    └── LogicalScan
```

例如：

```sql
SELECT name, age FROM users WHERE age >= 18;
```

当表结构为 `users(id INT, name TEXT, age REAL)` 时：

```text
LogicalProjection[users.name@1, users.age@2]
└── LogicalFilter[AND(users.age@2 >= REAL(18.0))]
    └── LogicalScan[users]
```

Projection 的 BoundColumnRef 索引相对于 child 输入行；Projection 自己的 `output_schema` 再将输出列
编号为 `0、1`。

### 5.4 INSERT

INSERT 的处理顺序：

```text
describe
-> 检查 values 数量
-> 按 Schema 对应列调用 normalize_literal
-> LogicalInsert
```

值数量必须在 `zip()` 前检查。类型规范化结果为：

| 目标类型 | 接受值 | BoundLiteral 中的值 |
|---|---|---|
| INT | 非 bool 的 int | int |
| TEXT | str | str |
| REAL | 非 bool 的 int/float | float |

### 5.5 UPDATE assignments

处理顺序：

```text
按列名覆盖，保留最后一次赋值
-> schema.column() 绑定目标列
-> 按 LogicalColumn.index 排序
-> normalize_literal(value, column.type)
-> BoundAssignment tuple
```

assignment 始终绑定到表的完整 Schema。UPDATE 计划形态固定为：

```text
LogicalUpdate
└── LogicalFilter（可选）
    └── LogicalScan
```

### 5.6 DELETE

DELETE 计划形态固定为：

```text
LogicalDelete
└── LogicalFilter（可选）
    └── LogicalScan
```

## 6. 错误传播

| 位置 | 条件 | 错误码 |
|---|---|---|
| `_load_schema()` | 表不存在 | `describe()` 传播 `E_TABLE_NOT_FOUND` |
| `_bind_projection()` | SELECT 列不存在 | `LogicalSchema.column()` 抛 `E_COLUMN_NOT_FOUND` |
| `_build_filter()` | WHERE 列不存在 | `bind_conjunction()` 抛 `E_COLUMN_NOT_FOUND` |
| `_build_filter()` | WHERE 类型不兼容 | `coerce()` 抛 `E_TYPE_MISMATCH` |
| `_build_insert()` | 值数量与列数不同 | Builder 抛 `E_VALUE_COUNT` |
| `_build_insert()` | 值类型不兼容 | `normalize_literal()` 抛 `E_TYPE_MISMATCH` |
| `_bind_assignments()` | UPDATE 列不存在 | `LogicalSchema.column()` 抛 `E_COLUMN_NOT_FOUND` |
| `_bind_assignments()` | UPDATE 值类型不兼容 | `normalize_literal()` 抛 `E_TYPE_MISMATCH` |

Builder 不捕获和包装上述 `SqlError`。计划节点的 `__post_init__()` 继续负责最终结构不变式检查。

## 7. 包导出

在 `runner/logical_plan/__init__.py` 中增加：

```python
from runner.logical_plan.builder import DescribeTable, LogicalPlanBuilder
from runner.logical_plan.plans import (
    LogicalCreateDatabase,
    LogicalDropDatabase,
    LogicalUseDatabase,
)
```

并加入 `__all__`：

```python
"DescribeTable",
"LogicalPlanBuilder",
"LogicalCreateDatabase",
"LogicalDropDatabase",
"LogicalUseDatabase",
```

## 8. 实施顺序

1. 在 `plans.py` 增加三个数据库命令计划节点。
2. 新建 `builder.py` 并实现完整 Statement 分派。
3. 实现 Schema、Scan、Filter 三个公共构建函数。
4. 实现 Projection 和 assignment 绑定。
5. 实现 INSERT、SELECT、UPDATE、DELETE 构建。
6. 更新 `runner/logical_plan/__init__.py` 的导出。
