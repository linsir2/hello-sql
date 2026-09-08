# LogicalPlan 模型开发设计

## 1. 文档目标

本文给出 `runner/logical_plan/` 的第一版开发方案。目标不是执行 SQL，而是把
`contracts.ast.Statement` 转换为一棵已经完成表、列和类型绑定的 LogicalPlan 树，供后续
Optimizer、Physical Planner 或 Executor 消费。

本阶段只输出设计，不修改已经冻结的 `contracts/` 契约，也不实现 Executor 或 Storage。

## 2. 现状分析

### 2.1 当前模块边界

仓库已经冻结如下调用链：

```text
SQL text
  -> compiler.parse()
  -> contracts.ast.Statement
  -> runner（语义检查、规划、执行）
  -> contracts.storage.Storage
  -> contracts.result.QueryResult
```

与 LogicalPlan 直接相关的事实如下：

1. `contracts.ast` 是语法层模型，只保存表名、列名和 Python 字面量；它不知道表或列是否真实存在。
2. 当前没有独立 Catalog 接口。Runner 只能通过 `Storage.describe(table)` 获得 `TableInfo`。
3. `Storage.scan(table)` 返回 `Iterator[Row]`，其中 `Row = (RowId, tuple[Value, ...])`。
4. V1 仅支持单表 CRUD、`列 op 字面量` 和 AND；不支持 JOIN、ORDER BY、LIMIT、OR、NULL、别名和聚合。
5. SELECT 允许重复投影列，`SELECT *` 必须按建表顺序展开。
6. UPDATE/DELETE 必须先收集所有命中的 `row_id`，再修改数据，不能在扫描迭代期间写表。

### 2.2 为什么不能直接把 AST 当执行计划

例如 AST 中的：

```python
Cmp(Column("age"), ">=", Literal(18))
```

仍缺少以下运行信息：

- `age` 是否存在；
- `age` 在行元组中的位置；
- `age` 的 SQL 类型；
- 字面量与列类型是否兼容；
- `SELECT *` 最终展开成哪些列。

如果 Executor 直接解释 AST，就会在每一行上重复用字符串查列，并把语义检查、规划和执行混在一起。
因此需要在 AST 和执行层之间增加 LogicalPlan，并在建树时一次性完成绑定。

### 2.3 LogicalPlan 的准确职责

LogicalPlan 表达“需要完成哪些关系操作”及其上下游依赖：

```text
LogicalProjection[name, age]
  -> LogicalFilter[age >= 18]
    -> LogicalScan[users]
```

LogicalPlan 不承担以下职责：

- 不调用 `Storage.scan/insert/update_row/delete_row`；
- 不读取 Page、文件或 Buffer Pool；
- 不保存迭代器、游标或运行状态；
- 不实现 Volcano `open/next/close`；
- 不决定 SeqScan、IndexScan 等物理算法。

其中 `LogicalScan` 只声明“读取哪张表”。将来 Physical Planner 可以把它转换为
`SeqScanExec` 或 `IndexScanExec`。执行期的 `SeqScanExec.next()` 再调用 Storage 提供的扫描
迭代器，从而保持 runner 与底层存储实现解耦。

## 3. 设计原则

### 3.1 计划是树，不是顺序列表

即使 V1 的 SELECT 只有单输入链，也统一使用树接口：

- 叶子节点：`LogicalScan`、DDL、INSERT；
- 一元节点：`LogicalFilter`、`LogicalProjection`、UPDATE、DELETE；
- 后续可扩展二元节点：JOIN；
- 后续可扩展 N 元节点：UNION。

基类统一暴露 `children`，遍历器和未来优化器不需要知道每种节点的具体结构。

### 3.2 计划节点不可变

所有节点使用 `@dataclass(frozen=True, slots=True)`：

- 规划完成后不会被 Executor 意外修改；
- 优化规则通过构造新节点改写树；
- 测试可以稳定比较整棵树；
- 不在节点中保存 Storage、Iterator 等可变运行对象。

### 3.3 LogicalPlan 使用绑定后的表达式

AST 中的列由字符串表示，计划中的列必须绑定为：

```text
(table_name, column_name, column_index, sql_type)
```

Executor 之后可以直接用 `values[column_index]` 取值，而不需要逐行查 Schema。

### 3.4 输出 Schema 是节点契约

每个关系节点都提供 `output_schema`：

- Scan：表的完整 Schema；
- Filter：与 child 相同；
- Projection：按 SELECT 书写顺序生成，允许重复列；
- DDL/DML：空 Schema。

Schema 只描述 SQL 可见的值列。Storage 返回的 `row_id` 是执行期 Record 的内部元数据，不加入
SELECT 输出 Schema，避免它被错误暴露给用户。

### 3.5 只实现 V1，不把未来语法伪装成已支持

模型会保留扩展能力，但本阶段不增加 JOIN、Sort、Limit、Aggregate、IndexScan、NULL 或布尔类型。
这些功能需要先修改冻结契约，再新增相应计划节点。

## 4. 建议目录

```text
runner/logical_plan/
├── __init__.py       # 对外导出稳定类型
├── base.py           # LogicalPlan、LogicalColumn、LogicalSchema、EMPTY_SCHEMA
├── expressions.py    # 绑定后的列引用、字面量、比较、AND、赋值
├── plans.py          # 八类 V1 计划节点
├── builder.py        # Statement -> LogicalPlan，负责绑定与语义检查
└── explain.py        # 纯函数格式化计划树，便于测试与调试
```

当前已有空文件 `base.py`、`plans.py`；后续实现时补充其余文件即可。

`builder.py` 属于建模入口，可以保留在 `logical_plan` 包内。执行器应放在未来的
`runner/executor/`，不要放进本目录。

## 5. 基础模型

### 5.1 LogicalColumn 与 LogicalSchema

建议在 `base.py` 中定义：

```python
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass

from contracts.ast import SqlType


@dataclass(frozen=True, slots=True)
class LogicalColumn:
    table: str
    name: str
    index: int
    type: SqlType


@dataclass(frozen=True, slots=True)
class LogicalSchema:
    columns: tuple[LogicalColumn, ...]

    def column(self, name: str) -> LogicalColumn:
        """按名称解析列；找不到时抛 E_COLUMN_NOT_FOUND。"""


EMPTY_SCHEMA = LogicalSchema(())
```

字段语义：

| 字段 | 语义 |
|---|---|
| `table` | 列所属表；V1 没有别名 |
| `name` | 已小写的列名 |
| `index` | 该列在当前输入行值元组中的零基位置 |
| `type` | `contracts.ast.SqlType` |

`LogicalSchema.column()` 不能返回 `None`。未找到时统一抛
`SqlError(E_COLUMN_NOT_FOUND, ...)`，使错误行为与契约一致。

### 5.2 LogicalPlan 基类

```python
class LogicalPlan(ABC):
    @property
    @abstractmethod
    def children(self) -> tuple["LogicalPlan", ...]:
        raise NotImplementedError

    @property
    @abstractmethod
    def output_schema(self) -> LogicalSchema:
        raise NotImplementedError
```

基类只定义所有计划都具备的结构能力，不提供 `execute()` 或 `next()`。

暂不在 V1 基类加入 `accept(visitor)`。Python 可以通过 `isinstance` 做节点分派，通过统一
`children` 做通用遍历；需要多组复杂访问器时再引入 Visitor，避免首版过度设计。

## 6. 绑定表达式模型

见
[logical-plan-expression-dev-design.md](logical-plan-expression-dev-design.md)。

本文只维护 LogicalPlan 建模本身，表达式的绑定与求值细节不再在此定义。

## 7. V1 计划节点

### 7.1 节点总表

| 节点 | children | 关键字段 | output_schema |
|---|---:|---|---|
| `LogicalScan` | 0 | `table`, `schema` | 表 Schema |
| `LogicalFilter` | 1 | `predicate`, `child` | child Schema |
| `LogicalProjection` | 1 | `columns`, `child` | 投影 Schema |
| `LogicalCreateTable` | 0 | `table`, `columns` | 空 |
| `LogicalDropTable` | 0 | `table` | 空 |
| `LogicalInsert` | 0 | `table`, `table_schema`, `values` | 空 |
| `LogicalUpdate` | 1 | `table`, `assignments`, `child` | 空 |
| `LogicalDelete` | 1 | `table`, `child` | 空 |

### 7.2 查询节点

```python
@dataclass(frozen=True, slots=True)
class LogicalScan(LogicalPlan):
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
    predicate: BoundConjunction
    child: LogicalPlan

    @property
    def children(self) -> tuple[LogicalPlan, ...]:
        return (self.child,)

    @property
    def output_schema(self) -> LogicalSchema:
        return self.child.output_schema


@dataclass(frozen=True, slots=True)
class LogicalProjection(LogicalPlan):
    columns: tuple[BoundColumnRef, ...]
    child: LogicalPlan

    @property
    def children(self) -> tuple[LogicalPlan, ...]:
        return (self.child,)
```

`LogicalProjection.output_schema` 按 `columns` 的顺序重新生成列位置。不能对投影列去重，因为
`SELECT name, name FROM users` 在 V1 中合法。

### 7.3 DDL 与 DML 节点

```python
@dataclass(frozen=True, slots=True)
class LogicalCreateTable(LogicalPlan):
    table: str
    columns: tuple[ColumnDef, ...]


@dataclass(frozen=True, slots=True)
class LogicalDropTable(LogicalPlan):
    table: str


@dataclass(frozen=True, slots=True)
class LogicalInsert(LogicalPlan):
    table: str
    table_schema: LogicalSchema
    values: tuple[BoundLiteral, ...]


@dataclass(frozen=True, slots=True)
class LogicalUpdate(LogicalPlan):
    table: str
    assignments: tuple[BoundAssignment, ...]
    child: LogicalPlan


@dataclass(frozen=True, slots=True)
class LogicalDelete(LogicalPlan):
    table: str
    child: LogicalPlan
```

DDL、INSERT 是叶子节点；UPDATE、DELETE 是一元节点，因为它们必须先通过 child 找到目标行。

UPDATE/DELETE 的 child 只能是以下两种形态：

```text
LogicalScan[table]
```

或：

```text
LogicalFilter[predicate]
  -> LogicalScan[table]
```

执行阶段由 Scan 产生携带 `row_id` 的内部 Record，Filter 必须原样保留该元数据，最上层的
Update/Delete 先收集所有命中 Record 的 `row_id`，结束扫描后再调用 Storage 写接口。`row_id`
不属于 LogicalSchema，也不需要加入计划节点字段。

INSERT V1 只支持 `VALUES` 单行插入，直接把绑定值保存在 `LogicalInsert` 即可。暂不增加
`LogicalValues` 子节点；等语法支持多行 VALUES 或 INSERT ... SELECT 时再抽象为数据源节点。

## 8. LogicalPlanBuilder

### 8.1 对外接口

```python
class LogicalPlanBuilder:
    def __init__(self, storage: Storage) -> None:
        self._storage = storage

    def build(self, statement: Statement) -> LogicalPlan:
        ...
```

当前契约没有独立 Catalog，Builder 可以调用 `storage.describe()` 获取只读元数据，但必须遵守：

- 建树阶段只允许调用 `describe()`；
- 不允许调用 `scan()` 或任何写方法；
- 不把 `Storage` 引用保存进任何计划节点；
- `TableInfo` 转换成不可变 `LogicalSchema` 后存入计划。

未来若拆出 Catalog，只需把 Builder 的元数据依赖替换为只读 `Catalog` 协议，节点模型无需变化。

### 8.2 通用绑定步骤

对需要访问已有表的语句：

```text
statement.table
  -> storage.describe(table)
  -> TableInfo
  -> LogicalSchema
  -> 绑定列名为 LogicalColumn
  -> 检查并规范化字面量类型
  -> 构造计划树
```

类型规则必须与冻结契约一致：

| 目标列类型 | 合法 Python 值 | 绑定后的建议值 |
|---|---|---|
| INT | `int` 且不是 `bool` | `int` |
| TEXT | `str` | `str` |
| REAL | `int` 或 `float`，且不是 `bool` | `float` |

错误统一使用 `contracts.errors.SqlError`：

- 表不存在：沿用 `Storage.describe()` 的 `E_TABLE_NOT_FOUND`；
- 列不存在：`E_COLUMN_NOT_FOUND`；
- INSERT 值数量不符：`E_VALUE_COUNT`；
- WHERE、INSERT、UPDATE 类型不匹配：`E_TYPE_MISMATCH`。

### 8.3 各语句建树规则

#### SELECT

```text
1. describe 表并创建 LogicalScan
2. 有 WHERE：绑定谓词并包一层 LogicalFilter
3. 展开/绑定 SELECT 列并包一层 LogicalProjection
4. 返回 Projection 作为 root
```

即使是 `SELECT *`，首版也保留 Projection 节点，使所有 SELECT 都有一致的根节点和明确输出表头。
未来 Optimizer 可以删除等价的全列 Projection。

#### UPDATE

```text
1. describe 表并创建 LogicalScan
2. 有 WHERE：绑定谓词并包一层 LogicalFilter
3. 绑定 assignments
4. 用 LogicalUpdate 包住 child
```

重复赋值按契约执行“后者覆盖前者”。Builder 先以列名做 last-write-wins，再按表 Schema 的列序号
排列最终 assignments，确保计划结果稳定。

#### DELETE

```text
1. describe 表并创建 LogicalScan
2. 有 WHERE：绑定谓词并包一层 LogicalFilter
3. 用 LogicalDelete 包住 child
```

#### INSERT

```text
1. describe 表
2. 检查值数量
3. 按目标列逐个检查并规范化类型
4. 构造 LogicalInsert
```

#### CREATE TABLE / DROP TABLE

- CREATE 直接构造 `LogicalCreateTable`。表是否已存在仍由执行阶段调用 Storage 时检查，符合当前契约。
- DROP 可以直接构造 `LogicalDropTable`；表不存在由执行阶段 Storage 报错。

这样不会为了 DDL 建树提前执行有副作用的操作。

## 9. 完整示例

SQL（符合当前 V1）：

```sql
SELECT name, age
FROM users
WHERE age >= 18;
```

假设：

```text
users(id INT, name TEXT, age REAL)
```

AST：

```text
SelectStmt
├── columns = ("name", "age")
├── table = "users"
└── where = Cmp(Column("age"), ">=", Literal(18))
```

绑定结果：

```text
name -> LogicalColumn(table="users", name="name", index=1, type=TEXT)
age  -> LogicalColumn(table="users", name="age",  index=2, type=REAL)
18   -> BoundLiteral(value=18.0, type=REAL)
```

最终 LogicalPlan：

```text
LogicalProjection[users.name@1, users.age@2]
  output: (name TEXT, age REAL)
  -> LogicalFilter[users.age@2 >= REAL(18.0)]
       output: (id INT, name TEXT, age REAL)
       -> LogicalScan[users]
            output: (id INT, name TEXT, age REAL)
```

后续 Executor 的数据流是自底向上，但调用是自顶向下：

```text
Projection.next()
  -> Filter.next()
    -> Scan.next()
      -> Storage.scan("users") 的迭代器
```

这段调用只用于说明后续模块如何消费计划；LogicalPlan 节点自身不包含 `next()`。

## 10. 遍历与 EXPLAIN

在 `explain.py` 中提供纯函数：

```python
def format_plan(plan: LogicalPlan) -> str:
    ...
```

实现方式是递归读取 `plan.children`，输出固定缩进文本。不要直接依赖 dataclass 默认 `repr`，否则字段
调整会导致大量测试无意义变化。

首版只需格式化，不必实现通用 Visitor。该函数可以支持：

- Builder 单元测试的快照断言；
- 后续 REPL 的 `EXPLAIN`；
- 检查优化前后的计划树；
- 调试节点 Schema 传递。

## 11. 不变式

实现时必须在构造或 Builder 阶段保证：

1. 所有节点及其集合字段不可变，集合统一使用 tuple。
2. `LogicalFilter.predicate.terms` 非空。
3. 所有 `BoundColumnRef` 都能在直接 child 的输入 Schema 中按 index 定位，且名称、类型一致。
4. Projection 的输出顺序与 SELECT 书写顺序一致，重复列不去重。
5. Filter 不改变 child 的输出 Schema。
6. UPDATE/DELETE 的目标表必须与其底层 Scan 的表一致。
7. DDL/DML 的 `output_schema` 恒为 `EMPTY_SCHEMA`。
8. 计划节点不持有 Storage、Iterator、Row 或 QueryResult。
9. 建树不产生任何数据写入，也不启动表扫描。
10. 不使用 `None` 表示“空计划”；每条合法 Statement 必须得到一个明确 root。

## 12. 测试方案

建议增加：

```text
tests/runner/logical_plan/
├── test_schema.py
├── test_expression_binding.py
├── test_plan_nodes.py
├── test_builder.py
└── test_explain.py
```

### 12.1 节点测试

- 叶子节点 `children == ()`；
- 一元节点 `children == (child,)`；
- frozen dataclass 不可修改；
- Filter 保持 Schema；
- Projection 正确重排和保留重复列；
- DDL/DML 输出空 Schema。

### 12.2 Builder 正常路径

- `SELECT *` 展开为建表顺序；
- SELECT + WHERE 生成 `Projection(Filter(Scan))`；
- 无 WHERE 不生成 Filter；
- AND 被展平且保持从左到右顺序；
- INSERT 值完成类型绑定；
- UPDATE 重复赋值后者覆盖前者；
- DELETE/UPDATE 的根与 child 结构正确；
- CREATE/DROP 生成叶子计划。

### 12.3 Builder 错误路径

- 表不存在；
- SELECT、WHERE、UPDATE 中列不存在；
- INSERT 值数量不一致；
- WHERE/INSERT/UPDATE 类型不匹配；
- `bool` 被明确拒绝为 INT/REAL。

### 12.4 层间边界测试

使用 SpyStorage 记录调用，断言：

- 规划 SELECT/INSERT/UPDATE/DELETE 时只调用 `describe()`；
- 规划 CREATE/DROP 时不调用任何 Storage 方法；
- Builder 从不调用 `scan/insert/update_row/delete_row/create_table/drop_table`。

### 12.5 与 golden 用例的衔接

现有 `tests/golden_sql.py` 仍是最终集成验收。LogicalPlan 阶段可以把每条成功解析的 Statement 送入
Builder，验证计划或预期语义错误；涉及实际表状态的测试使用 FakeStorage 预置 Schema 即可。

## 13. 实施顺序

1. 在 `base.py` 实现 Schema 和 `LogicalPlan` 抽象。
2. 在 `expressions.py` 实现绑定表达式。
3. 在 `plans.py` 实现八类计划节点及 Schema 传播。
4. 在 `builder.py` 实现 Schema 转换、列绑定和类型检查辅助函数。
5. 按 Statement 类型实现建树。
6. 在 `explain.py` 实现稳定的树格式化。
7. 补齐节点、Builder、错误和 Storage 边界测试。
8. 最后由 Executor Builder 消费 LogicalPlan；不要反向把执行逻辑塞回计划节点。

## 14. 验收标准

LogicalPlan 模型完成时，应同时满足：

- 六种 V1 Statement 都能构造成明确、不可变的计划 root；
- SELECT/UPDATE/DELETE 能形成结构正确的操作树；
- 表达式中的列均已绑定为列序号和类型；
- `SELECT *`、重复投影列、AND、UPDATE 重复赋值等契约细节正确；
- 所有语义错误使用冻结错误码；
- 建树期间没有扫描和写入 Storage；
- `format_plan()` 能稳定展示计划树；
- 后续 Executor 可以只根据计划和注入的 Storage 执行，不需要重新解释 AST。

## 15. 后续扩展边界

V1 完成后，再按契约升级逐步增加：

```text
LogicalPlan
├── LogicalSort / LogicalLimit
├── LogicalJoin
├── LogicalAggregate
└── LogicalValues

BoundExpression 的扩展（算术、OR / NOT、列列比较、NULL 语义）已由
logical-plan-expression-dev-design.md 规划，不再在此维护。
```

物理计划应另建包，例如 `runner/physical_plan/`，将 `LogicalScan` 映射为具体的 SeqScan 或
IndexScan。Optimizer 也只通过构造新 LogicalPlan 改写树。这样可以保持：

```text
AST：用户写了什么
LogicalPlan：需要做哪些关系操作
PhysicalPlan：选择什么物理算法
Executor：真正拉取和处理数据
Storage：如何保存并返回记录
```

五层职责彼此独立。
