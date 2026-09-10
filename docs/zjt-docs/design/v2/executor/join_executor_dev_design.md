# Nested Loop Join Executor 开发设计

## 1. 目标

本设计在不引入逻辑优化器和独立物理计划层的前提下，完成 V2 `INNER JOIN`
的执行链路：

```text
SELECT SQL
  -> JOIN AST
  -> LogicalJoin
  -> NestedLoopJoinExecutor
  -> QueryResult
```

实现后应支持：

- 单个 `INNER JOIN`；
- 按 SQL 书写顺序构造的链式左深 JOIN；
- `ON` 中的列与列比较、BOOLEAN 列、`AND` / `OR` / `NOT`；
- JOIN 之上的 `WHERE` 和最终投影；
- 别名、限定列和 JOIN 结果表头。

## 2. 范围

本次修改范围：

```text
runner/executor/dql.py
runner/executor/__init__.py
tests/test_integration_join_flow.py
```

本次不实现：

- JOIN 顺序重排、谓词下推和投影裁剪；
- Hash Join、Merge Join 或基于索引的 JOIN；
- LEFT / RIGHT / FULL / CROSS JOIN；
- 代价估算、统计信息和物理算子选择；
- UPDATE / DELETE 中的 JOIN。

## 3. 已有上游契约

### 3.1 LogicalJoin

`LogicalPlanBuilder` 已按书写顺序构造左深计划。每个 `LogicalJoin` 提供：

- `left`：已加入范围的左侧子计划；
- `right`：当前右表的 `LogicalScan`；
- `on`：已完成名称绑定和类型检查的布尔表达式；
- `schema`：按“左列在前、右列在后”拼接并重新编号的 Schema。

`LogicalJoin` 构造时已校验：

1. 输出 Schema 等于 `join_schema(left.output_schema, right.output_schema)`；
2. `on` 顶层为非空 `BoundLogical(AND)`；
3. `on` 中每个列引用都能按 index 定位到拼接 Schema；
4. `on` 的每个 conjunct 类型为 BOOLEAN。

因此执行器不再进行名称解析或类型推导，只按 index 读取拼接行。

### 3.2 行值与 Schema

`ExecRow.values` 与当前算子的 `output_schema.columns` 一一对应。JOIN 输出值固定为：

```python
joined_values = left_row.values + right_row.values
```

该顺序与 `join_schema` 一致，所以 `ON` 中的 `BoundColumnRef.index` 可直接对
`joined_values` 求值。执行期不应再计算列偏移或根据表名查列。

## 4. 执行器设计

### 4.1 结构

```python
@dataclass(frozen=True, slots=True)
class NestedLoopJoinExecutor(RowExecutor):
    left: RowExecutor
    right: RowExecutor
    on: BoundExpr
    schema: LogicalSchema
```

`output_schema` 直接返回 `schema`。`schema` 来自已校验的 `LogicalJoin.output_schema`，
执行器不重复拼接 Schema。

### 4.2 算法

```python
def rows(self, context: ExecutionContext) -> Iterator[ExecRow]:
    right_rows = tuple(self.right.rows(context))
    for left_row in self.left.rows(context):
        for right_row in right_rows:
            values = left_row.values + right_row.values
            if eval_expr(self.on, values):
                yield ExecRow(row_id=left_row.row_id, values=values)
```

执行步骤：

1. 调用右子执行器并物化其全部输出；
2. 从左子执行器逐行拉取；
3. 为每个左行遍历已物化的右行；
4. 按 Schema 顺序拼接 values；
5. 调用 `eval_expr(on, values)`；
6. 仅当结果为 `True` 时向上游产出拼接行。

空集合语义由循环结构自然保证：任一侧为空时，INNER JOIN 不产生结果行。

### 4.3 右侧物化策略

V2 没有统计信息和 JOIN 物理算子选择，因此采用固定策略：

- 左侧保持拉取式，不整体物化；
- 右侧在每次 `rows()` 调用中物化一次；
- 不为每个左行重新执行右侧 Storage Scan。

设左右输入行数分别为 `L` 和 `R`：

- 时间复杂度：`O(L × R)`；
- 额外空间复杂度：`O(R)`。

这与 V2 固定使用 Nested Loop Join 的范围一致。后续若引入独立物理计划层，
可在不改变 `LogicalJoin` 契约的前提下替换为其他 JOIN 算法。

### 4.4 row_id 语义

Storage 的 `row_id` 只能标识单表物理行，JOIN 结果不存在单一有效的物理
`row_id`。本实现让 JOIN 输出沿用 `left_row.row_id`，原因是：

- `ExecRow` 是现有统一行容器，`row_id` 当前为必填 `int`；
- V2 语法中 JOIN 只能进入 SELECT 计划；
- `SelectExecutor` 只读取 `values`，不对外返回 `row_id`；
- UPDATE / DELETE 的子计划仍只能是 Scan / Filter，不会消费 JOIN 行。

因此该字段在 JOIN 路径中只是内部流水线元数据，不得解释为 JOIN 结果
可写回的行标识。如果未来支持 `UPDATE ... JOIN` 或需要跟踪多表行来源，应将
`ExecRow` 升级为显式的多来源 provenance 结构，不应沿用当前方案。

## 5. 链式 JOIN

对于：

```sql
SELECT c.name, o.id, i.sku
FROM customers c
JOIN orders o ON c.id = o.customer_id
JOIN items i ON o.id = i.order_id;
```

逻辑计划为：

```text
Projection
└── Join2 ON o.id = i.order_id
    ├── Join1 ON c.id = o.customer_id
    │   ├── Scan(customers AS c)
    │   └── Scan(orders AS o)
    └── Scan(items AS i)
```

`build_row_executor()` 递归构造：

```text
ProjectionExecutor
└── NestedLoopJoinExecutor(Join2)
    ├── NestedLoopJoinExecutor(Join1)
    │   ├── SeqScanExecutor(customers)
    │   └── SeqScanExecutor(orders)
    └── SeqScanExecutor(items)
```

Join1 输出 `(customers columns..., orders columns...)`，Join2 再在末尾追加
`items` 列。每层都与自己的 `LogicalJoin.schema` 一致，因此第二个 ON 可以
按累积 Schema 的 index 正确访问前一层的任意列。

## 6. 执行器树接入

`build_row_executor()` 增加 `LogicalJoin` 分支：

```python
case LogicalJoin():
    return NestedLoopJoinExecutor(
        left=build_row_executor(plan.left),
        right=build_row_executor(plan.right),
        on=plan.on,
        schema=plan.output_schema,
    )
```

该分支必须在递归构造时保留原计划的左右顺序，否则：

- 输出列顺序会改变；
- `BoundColumnRef.index` 会指向错误位置；
- `SELECT *` 的表头与行值将不一致。

`NestedLoopJoinExecutor` 同时由 `runner.executor` 包导出，便于后续执行器单元测试
和调试使用。

## 7. 错误边界

JOIN 执行器不新增 SQL 错误码。错误归属保持为：

| 问题 | 发现阶段 |
|---|---|
| 表不存在 | LogicalPlanBuilder 调用 `Storage.describe` |
| 限定符不存在 | 名称绑定 |
| 未限定列歧义 | 名称绑定 |
| 重复有效限定符 | Schema 拼接 |
| ON 不是 BOOLEAN | 表达式绑定 / LogicalJoin 校验 |
| 表文件读取失败 | Storage Scan |

执行器信任已绑定计划的不变式，不把上述语义错误延迟到两层循环中。

## 8. 测试方案

综合测试使用真实的：

```text
compiler.parse
runner.Runner
storage.DatabaseServer
```

不手工构造 AST、LogicalPlan 或伪造 Storage，以确保测试覆盖三个模块的
真实边界。

### 8.1 单 JOIN

数据同时包含：

- 一个左行匹配多个右行；
- 左侧无匹配行；
- 右侧无匹配行；
- BOOLEAN 列为真和为假的行。

断言内容：

- INNER JOIN 只输出 ON 匹配组合；
- 一对多匹配不丢行；
- `WHERE active AND NOT cancelled` 正确作用在 JOIN 结果上；
- 显式限定投影的表头为 `限定符.列名`；
- INT 和 REAL 值保持存储层规范化结果。

### 8.2 链式 JOIN

使用三表左深 JOIN，第二个 ON 同时包含：

- 对左侧累积 Schema 中列的引用；
- 对当前右表列的引用；
- BOOLEAN conjunct。

断言链式 JOIN 的列位置、ON 过滤、WHERE 过滤、投影和表头均正确。

SQL 未定义无 `ORDER BY` 时的结果顺序，因此综合测试使用多重集合比较，
不把当前扫描顺序固化为 SQL 契约。

### 8.3 回归

实现完成后运行：

```bash
.venv/bin/python -m pytest -q tests/test_integration_join_flow.py
.venv/bin/python -m pytest -q
```

全量回归用于确认 `build_row_executor()` 的新分支不影响单表 SELECT 和
UPDATE / DELETE 复用的 Scan / Filter 路径。

## 9. 实施顺序

1. 在 `dql.py` 实现 `NestedLoopJoinExecutor`；
2. 在 `build_row_executor()` 中接入 `LogicalJoin`；
3. 在 `runner.executor` 中导出新执行器；
4. 添加基于真实存储的单 JOIN 综合测试；
5. 添加基于真实存储的链式 JOIN 综合测试；
6. 运行 JOIN 专项测试和全量回归。

## 10. 验收标准

- 单 JOIN 可从真实 SQL 执行到 `QueryResult`；
- 链式 JOIN 按左深计划执行，累积 Schema 列索引正确；
- ON、WHERE 中的 BOOLEAN、AND、NOT 正确求值；
- 无匹配、一对多和孤立行行为符合 INNER JOIN 语义；
- 投影行值与限定表头一致；
- 现有单表 DQL、DML、DDL 和存储测试全部通过；
- 实现不依赖逻辑优化器。
