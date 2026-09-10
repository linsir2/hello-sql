# Runner 开发设计

## 1. 实现目标

`Runner` 是运行层的语句级编排入口，负责把 SQL 文本依次交给现有组件：

```text
SQL
 ↓
parse(sql)
 ↓
Statement
 ↓
LogicalPlanBuilder.build(statement)
 ↓
LogicalPlan
 ↓
ExecutorTreeBuilder.build(plan)
 ↓
StatementExecutor.execute(context)
 ↓
QueryResult
```

对外接口遵循项目契约：

```python
runner = Runner(server=server, parse=parse)
result = runner.execute(sql)
```

创建会话时可通过 `current_database` 选择初始数据库，省略时使用 `main`。

`Runner` 还提供 `repl()`，供根目录 `main.py` 启动交互式命令行。核心执行能力必须集中在
`execute()`；REPL 只负责输入、输出和可预期错误的展示。

## 2. 职责边界

`Runner` 承担以下职责：

- 保存一条会话的 `ExecutionContext`；
- 初始化并连接默认数据库 `main`；
- 调用注入的 Parser；
- 串联逻辑计划构建、Executor 树构建和执行；
- 保证后续语句始终使用 `USE` 切换后的当前 Storage；
- 将 Executor 产生的 `QueryResult` 原样返回；
- 在 REPL 边界捕获并展示 `SqlError`。

`Runner` 不承担以下职责：

- 不解析 SQL 语法；
- 不重复执行表、列和类型检查；
- 不识别具体 LogicalPlan 或 Executor 节点；
- 不直接执行 Storage 的表级 CRUD；
- 不修改或包装 `QueryResult`；
- 不在 `execute()` 中吞掉 `SqlError`；
- 不负责 Storage 的事务、回滚和持久化。

## 3. 文件结构

```text
runner/
├── runner.py                 # Runner 编排与 REPL
├── __init__.py               # 对外导出 Runner
├── logical_plan/
│   └── builder.py            # Statement -> LogicalPlan
└── executor/
    ├── builder.py            # LogicalPlan -> StatementExecutor
    └── context.py            # 执行环境与当前数据库状态
```

`main.py` 仍是唯一同时导入 `compiler`、`storage` 和 `runner` 的装配入口。`runner/` 内不得导入
`compiler` 或 `storage`。运行层通过 `contracts.storage.BaseDatabaseServer` 和
`contracts.storage.BaseStorage` 使用库级与表级公开接口。

## 4. 依赖与状态

### 4.1 Parser 类型

Runner 不依赖 Parser 实现，只依赖调用形状：

```python
from collections.abc import Callable

from contracts.ast import Statement


ParseSql = Callable[[str], Statement]
```

生产环境由 `main.py` 注入 `compiler.parse`，单元测试可以注入返回手写 AST 的假 Parser。

### 4.2 Runner 字段

```python
from contracts.storage import BaseDatabaseServer


class Runner:
    def __init__(
        self,
        server: BaseDatabaseServer,
        parse: ParseSql,
        current_database: str = DEFAULT_DATABASE,
    ) -> None:
        self._parse = parse
        self._context = ExecutionContext(...)
        self._logical_plan_builder = LogicalPlanBuilder(
            self._describe_current_table
        )
        self._executor_tree_builder = ExecutorTreeBuilder()
```

各字段职责如下：

| 字段 | 生命周期 | 职责 |
|---|---|---|
| `_parse` | Runner 全生命周期 | SQL 文本转换为 `Statement` |
| `_context` | Runner 全生命周期 | 保存 Server、当前 Storage 和当前库名 |
| `_logical_plan_builder` | Runner 全生命周期 | 绑定 AST 并构建 LogicalPlan |
| `_executor_tree_builder` | Runner 全生命周期 | 构建语句级 Executor 树 |

一个 `Runner` 实例对应一条有状态数据库会话。构造参数 `current_database` 决定会话的初始数据库，
默认值为 `main`。V1 按顺序执行语句，不支持同一实例并发调用 `execute()`。

## 5. 初始化

默认数据库名集中定义为常量：

```python
DEFAULT_DATABASE = "main"
```

构造 Runner 时先连接指定的初始数据库，再建立上下文：

```python
storage = server.connect(current_database)
self._context = ExecutionContext(
    server=server,
    storage=storage,
    current_database=current_database,
)
```

初始化完成后必须满足：

```text
context.current_database == 构造参数 current_database
context.storage 绑定 current_database 对应的数据库
context.server 是构造参数传入的 Server
```

省略 `current_database` 时，Runner 连接 `main`：

```python
runner = Runner(server=server, parse=parse)
```

也可以在创建会话时选择已有数据库：

```python
runner = Runner(
    server=server,
    parse=parse,
    current_database="shop",
)
```

`server.connect(current_database)` 负责检查数据库名称和存在性。连接成功后才创建
`ExecutionContext`；连接失败时，构造异常原样向上传播。该参数只选择已有数据库，不负责创建数据库。

`ExecutionContext` 是当前数据库状态的唯一来源。Runner 如需向外暴露当前数据库，提供只读属性：

```python
@property
def current_database(self) -> str:
    return self._context.current_database
```

Runner 的 `current_database` 属性始终读取 `ExecutionContext`，使数据库名与当前 Storage 保持同步。

## 6. 动态 Schema 查询

`LogicalPlanBuilder` 需要通过 `describe(table)` 读取当前数据库的表结构。这里不能在 Runner 初始化时
永久保存：

```python
context.storage.describe
```

这个绑定方法指向创建它时的 Storage；执行 `USE` 后，即使 `context.storage` 已替换，旧绑定方法仍可能
查询原数据库。

Runner 应提供动态转发方法：

```python
def _describe_current_table(self, table: str) -> TableInfo:
    return self._context.storage.describe(table)
```

然后将该方法注入 `LogicalPlanBuilder`：

```python
self._logical_plan_builder = LogicalPlanBuilder(
    self._describe_current_table
)
```

每次 Builder 请求表结构时，转发方法都会重新读取当前 `context.storage`，从而保证跨库隔离：

```text
USE shop
  -> context.storage 替换为 shop Storage

下一条 SELECT / INSERT / UPDATE / DELETE
  -> _describe_current_table()
  -> shop Storage.describe()
```

DDL 数据库语句和 `CREATE/DROP TABLE` 不需要读取表 Schema，因此不会产生额外的 `describe()` 调用。

## 7. execute 执行流程

`execute()` 是 Runner 的核心接口：

```python
def execute(self, sql: str) -> QueryResult:
    statement = self._parse(sql)
    plan = self._logical_plan_builder.build(statement)
    executor = self._executor_tree_builder.build(plan)
    return executor.execute(self._context)
```

各阶段只消费前一阶段的输出：

| 阶段 | 输入 | 输出 | 允许的副作用 |
|---|---|---|---|
| Parser | `str` | `Statement` | 无 |
| LogicalPlanBuilder | `Statement` | `LogicalPlan` | 只读 `describe()` |
| ExecutorTreeBuilder | `LogicalPlan` | `StatementExecutor` | 无 |
| Executor | `ExecutionContext` | `QueryResult` | 按语句调用 Server/Storage；`USE` 更新会话 |

`execute()` 不识别语句类别。SELECT、DML 和 DDL 的分发由 `ExecutorTreeBuilder` 完成，具体行为由相应
Executor 完成。

当前不增加独立 Optimizer。未来需要逻辑优化时，插入位置固定在逻辑计划构建与 Executor 树构建之间：

```text
LogicalPlanBuilder
 ↓
LogicalOptimizer
 ↓
ExecutorTreeBuilder
```

## 8. 数据库会话状态

数据库状态只保存在 `ExecutionContext` 中：

| 语句 | 成功后的状态 | 失败后的状态 |
|---|---|---|
| `CREATE DATABASE` | 当前库不变 | 不变 |
| `DROP DATABASE` | 当前库不变 | 不变 |
| `USE name` | `storage` 和 `current_database` 同时切换 | 两者都保持原值 |
| 表级 DDL / DML / DQL | 使用当前 `storage` | 当前库不变 |

`UseDatabaseExecutor` 必须先完成 `server.connect(name)`，成功后再更新上下文。因此 Runner 不需要额外
处理 `USE`，也不能在 Parser 或 LogicalPlan 阶段提前修改会话。

初始化参数和 `USE` 共同构成会话数据库的完整生命周期：

```text
Runner(current_database="shop")
  -> server.connect("shop")
  -> context.current_database = "shop"
  -> context.storage = shop Storage

USE main
  -> server.connect("main")
  -> context.current_database = "main"
  -> context.storage = main Storage
```

以下序列必须保持跨库隔离：

```text
USE shop
CREATE TABLE orders (...)
USE main
SELECT * FROM orders
```

最后一条语句必须在 `main` 中查找 `orders`，不能继续访问 `shop` 的 Schema 或数据。

## 9. 错误传播

`execute()` 不捕获 `SqlError`。错误由实际发现问题的阶段抛出并原样传播：

| 失败阶段 | 示例 | 后续阶段是否运行 |
|---|---|---|
| Parser | `E_SYNTAX` | 否 |
| LogicalPlanBuilder | 表、列、值数量或类型错误 | 否 |
| Executor | 数据库冲突、表冲突、Storage 错误 | 已进入执行阶段 |

这保证调用者可以稳定读取：

```python
error.code
error.message
```

Runner 不把错误转换成 `QueryResult`，也不改变错误码。V1 没有事务管理；执行阶段发生错误时，已经由
Server 或 Storage 完成的副作用是否回滚不属于 Runner 职责。

`USE` 是明确的状态例外：连接失败时必须保持原会话，成功时才更新两个会话字段。

## 10. REPL

`repl()` 是 `execute()` 的薄适配层，循环职责为：

```text
读取一条 SQL
 -> execute(sql)
 -> 成功：打印 QueryResult
 -> SqlError：打印 [错误码] 消息
 -> 继续读取下一条 SQL
```

REPL 可以捕获 `SqlError`，因为它是面向用户的最终边界；不应捕获所有 `Exception`，避免把程序缺陷伪装
成普通 SQL 错误。EOF 应正常结束循环。

结果展示遵守 `QueryResult` 的三种互斥形态：

| 结果形态 | 展示依据 |
|---|---|
| SELECT | `columns` 与 `rows` |
| DML | `affected_rows` |
| DDL | `affected_rows == 0` |

契约只规定结果数据形状，没有冻结表格边框或提示符样式。格式化逻辑应保持简单，不能反向影响
`execute()` 的返回值。`USE` 成功后的 “Database changed” 属于可选展示，不作为执行结果语义。

## 11. 包导出

`runner/__init__.py` 作为模块 C 的稳定入口，只需导出：

```python
from runner.runner import Runner

__all__ = ["Runner"]
```

外部调用者不需要了解 LogicalPlan、Executor 或 ExecutionContext：

```python
from runner import Runner
```

## 12. 测试方案

### 12.1 Runner 单元测试

使用 Fake Parser、FakeDatabaseServer 和 FakeStorage，覆盖：

1. 省略初始数据库时只连接一次 `main`；
2. 指定初始数据库时连接对应数据库并保存一致的会话状态；
3. 初始数据库连接失败时原样传播异常；
4. `execute()` 将原 SQL 交给注入的 Parser；
5. 九种 Statement 均能返回契约规定的 `QueryResult`；
6. Parser 抛错后不构建计划、不访问 Storage；
7. 绑定阶段抛错后不构建和执行 Executor；
8. Executor 或 Storage 的 `SqlError` 原样传播；
9. `USE` 成功后，下一条语句通过新 Storage 执行 `describe()` 和 CRUD；
10. `USE` 失败后，原 `storage` 与 `current_database` 均不改变；
11. 不同数据库中的同名表和异名表互不泄漏。

Fake 对象只实现当前用例需要的公开方法，并记录调用参数。测试应断言调用目标、调用顺序、返回值和
会话状态，不能依赖真实页文件实现。

### 12.2 集成测试

单元测试通过后，由根目录装配真实模块：

```python
from compiler import parse
from runner import Runner
from storage import DatabaseServer


server = DatabaseServer(data_dir)
runner = Runner(server=server, parse=parse)
```

按 `tests/golden_sql.py` 的既定顺序执行全部用例：

- 成功用例检查 `affected_rows`、`columns` 和 `rows`；
- SELECT 行顺序不保证，按无序集合比较；
- 失败用例检查 `SqlError.code`；
- 全部用例共享同一个 Runner，验证会话状态和前序数据变更能够延续。

## 13. 实现顺序与完成标准

实现顺序：

1. 在 `runner/runner.py` 实现初始化和 `execute()`；
2. 在 `runner/__init__.py` 导出 `Runner`；
3. 使用 Fake 依赖完成 Runner 单元测试；
4. 实现最小 REPL 输入输出；
5. 运行真实模块的 golden 集成测试。

完成时应同时满足：

- 对外调用为 `Runner(server, parse).execute(sql) -> QueryResult`；
- 构造 Runner 时可通过 `current_database` 选择初始数据库，默认连接 `main`；
- 一条 SQL 严格经过 Parser、LogicalPlanBuilder、ExecutorTreeBuilder 和 Executor；
- `USE` 后所有 Schema 查询和数据操作都进入新数据库；
- `execute()` 原样传播 `SqlError`；
- `runner/` 接收注入的 Parser，并通过 `contracts` 中的协议访问 Storage 能力；
- `main.py` 可以通过 `runner.repl()` 启动交互；
- golden 用例按顺序全部通过。
