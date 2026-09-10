# DDL Executor 开发设计

## 1. 实现目标

DDL 执行器覆盖五种命令计划：

| 逻辑计划 | 执行器 | 调用目标 |
|---|---|---|
| `LogicalCreateDatabase` | `CreateDatabaseExecutor` | `DatabaseServer.create_database()` |
| `LogicalDropDatabase` | `DropDatabaseExecutor` | `DatabaseServer.drop_database()` |
| `LogicalUseDatabase` | `UseDatabaseExecutor` | `DatabaseServer.connect()` |
| `LogicalCreateTable` | `CreateTableExecutor` | 当前 `Storage.create_table()` |
| `LogicalDropTable` | `DropTableExecutor` | 当前 `Storage.drop_table()` |

实现文件为 `runner/executor/ddl.py`。五个执行器均实现
[DQL Executor](DQL-executor-dev-design.md) 中定义的 `StatementExecutor`，成功时统一返回
`QueryResult(affected_rows=0)`。

## 2. 扩展 ExecutionContext

当前 `ExecutionContext` 只有 `storage`，无法执行数据库级命令。`context.py` 需要调整为：

```python
from dataclasses import dataclass


@dataclass(slots=True)
class ExecutionContext:
    server: object
    storage: object
    current_database: str
```

字段职责：

| 字段 | 用途 |
|---|---|
| `server` | CREATE/DROP DATABASE、连接数据库 |
| `storage` | 当前数据库的表级操作连接 |
| `current_database` | DROP 当前库检查和会话状态 |

Runner 初始化时应保证三者一致：

```python
storage = server.connect("main")
context = ExecutionContext(
    server=server,
    storage=storage,
    current_database="main",
)
```

## 3. 公共成功结果

每次执行都创建新的 `QueryResult`：

```python
def _ddl_success() -> QueryResult:
    return QueryResult(affected_rows=0)
```

不复用模块级 `QueryResult` 单例，因为 `QueryResult` 是可变 dataclass。

## 4. 表级 DDL

### 4.1 CreateTableExecutor

```python
@dataclass(frozen=True, slots=True)
class CreateTableExecutor(StatementExecutor):
    table: str
    columns: tuple[ColumnDef, ...]

    def execute(self, context: ExecutionContext) -> QueryResult:
        context.storage.create_table(self.table, self.columns)
        return _ddl_success()
```

列定义按 `LogicalCreateTable.columns` 原顺序传递给 Storage。

### 4.2 DropTableExecutor

```python
@dataclass(frozen=True, slots=True)
class DropTableExecutor(StatementExecutor):
    table: str

    def execute(self, context: ExecutionContext) -> QueryResult:
        context.storage.drop_table(self.table)
        return _ddl_success()
```

两个表级执行器始终使用 `context.storage`，因此作用域是当前数据库。

## 5. 数据库级 DDL

### 5.1 CreateDatabaseExecutor

```python
@dataclass(frozen=True, slots=True)
class CreateDatabaseExecutor(StatementExecutor):
    name: str

    def execute(self, context: ExecutionContext) -> QueryResult:
        context.server.create_database(self.name)
        return _ddl_success()
```

### 5.2 DropDatabaseExecutor

```python
@dataclass(frozen=True, slots=True)
class DropDatabaseExecutor(StatementExecutor):
    name: str

    def execute(self, context: ExecutionContext) -> QueryResult:
        if self.name == context.current_database:
            raise SqlError(
                E_DATABASE_IN_USE,
                f"database in use: {self.name}",
            )

        context.server.drop_database(self.name)
        return _ddl_success()
```

当前数据库检查必须先于 `server.drop_database()`。默认库 `main` 的永久保护继续由
DatabaseServer 负责；当 `main` 恰好是当前库时，上述会话检查会先返回同一个错误码。

### 5.3 UseDatabaseExecutor

```python
@dataclass(frozen=True, slots=True)
class UseDatabaseExecutor(StatementExecutor):
    name: str

    def execute(self, context: ExecutionContext) -> QueryResult:
        new_storage = context.server.connect(self.name)
        context.storage = new_storage
        context.current_database = self.name
        return _ddl_success()
```

必须先完成 `connect()`，再修改 Context。连接失败时，原 `storage` 和
`current_database` 保持不变；连接成功后再连续替换两个字段。

## 6. DDL 执行器构建

定义 DDL 计划联合类型：

```python
from typing import TypeAlias, assert_never


DdlPlan: TypeAlias = (
    LogicalCreateDatabase
    | LogicalDropDatabase
    | LogicalUseDatabase
    | LogicalCreateTable
    | LogicalDropTable
)
```

构建函数：

```python
def build_ddl_executor(plan: DdlPlan) -> StatementExecutor:
    match plan:
        case LogicalCreateDatabase():
            return CreateDatabaseExecutor(plan.name)
        case LogicalDropDatabase():
            return DropDatabaseExecutor(plan.name)
        case LogicalUseDatabase():
            return UseDatabaseExecutor(plan.name)
        case LogicalCreateTable():
            return CreateTableExecutor(plan.table, plan.columns)
        case LogicalDropTable():
            return DropTableExecutor(plan.table)
        case _:
            assert_never(plan)
```

## 7. 状态与错误规则

| 命令 | Context 状态变化 | 主要错误来源 |
|---|---|---|
| CREATE DATABASE | 无 | Server：`E_DATABASE_EXISTS` |
| DROP DATABASE | 无 | Executor：`E_DATABASE_IN_USE`；Server：不存在或默认库保护 |
| USE | 成功后替换当前库和 Storage | Server：`E_DATABASE_NOT_FOUND` |
| CREATE TABLE | 无 | Storage：`E_TABLE_EXISTS`、`E_DUP_COLUMN` |
| DROP TABLE | 无 | Storage：`E_TABLE_NOT_FOUND` |

所有 Server/Storage 抛出的 `SqlError` 原样向上传播。

## 8. 包导出

`runner/executor/__init__.py` 增加：

```python
from runner.executor.ddl import (
    CreateDatabaseExecutor,
    CreateTableExecutor,
    DdlPlan,
    DropDatabaseExecutor,
    DropTableExecutor,
    UseDatabaseExecutor,
    build_ddl_executor,
)
```

同步加入 `__all__`：

```python
"DdlPlan",
"CreateDatabaseExecutor",
"DropDatabaseExecutor",
"UseDatabaseExecutor",
"CreateTableExecutor",
"DropTableExecutor",
"build_ddl_executor",
```
