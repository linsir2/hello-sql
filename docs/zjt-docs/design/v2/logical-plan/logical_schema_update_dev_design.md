# LogicalSchema 升级开发设计（V2）

## 1. 文档目标

本文给出 `runner/logical_plan/` 在 V2 第二阶段对 **Schema 模型与名称/类型绑定** 的升级方案，
覆盖四项契约变化：

| 契约变化 | 本设计对应内容 |
|---|---|
| 新增 BOOLEAN 类型 | 类型推导收敛到 `SqlType`；布尔字面量绑定与规范化；布尔比较规则 |
| 列有所属表（qualifier） | `LogicalColumn.qualifier`；`LogicalSchema.resolve`；歧义检查 |
| 表支持别名 | 限定符取 `alias or table`；范围限定符唯一性；`LogicalScan.alias` |
| 接入 OR / NOT | 绑定分支与展平规则；谓词顶层规范形；`E_BOOLEAN_REQUIRED` 检查点 |

范围边界：

- 本文只改 `runner/logical_plan/`（`base.py`、`expressions.py`、`plans.py`、`builder.py`）；
- `contracts/`、`compiler/`、`storage/` 不改动，只按已冻结的 V2 契约调用；
- JOIN 的执行算法、谓词下推、投影裁剪属于 M2/M3 的独立设计，本文只定义 JOIN 引入的
  **Schema 拼接规则**；
- `runner/executor/` 只描述消费方式，不在本文展开。

## 2. 现状与差距

### 2.1 现状

```text
base.py       LogicalColumn(table, name, index, type)
              LogicalSchema.columns + column(name)          # 仅按列名唯一查找

expressions.py BOOLEAN 内部标记（因为 V1 的 SqlType 没有 BOOLEAN）
              bind_literal / normalize_literal 显式拒绝 bool
              bind_expr 可达分支：Column / Literal / Cmp / And
              eval_expr 已实现 OR 短路、BoundUnaryNot（无输入）

plans.py      LogicalScan(table, schema)
              LogicalFilter 要求 predicate 顶层为 BoundLogical(AND)
              LogicalProjection(columns, child)，输出列名恒为原列名

builder.py    单表路径：表名为 str、投影只接收列名、joins 字段被忽略
```

### 2.2 差距清单

| 编号 | 差距 | 后果 |
|---|---|---|
| G1 | `LogicalColumn` 只有 `table`，无法区分物理表名与别名 | 自连接、限定列解析、表头命名都缺字段 |
| G2 | `LogicalSchema.column(name)` 只按列名查找 | 无法做限定符解析、歧义检查 |
| G3 | BOOLEAN 是表达式层的临时标记，而 `SqlType` 已含 `BOOLEAN` | 类型体系出现两套表示 |
| G4 | `bind_literal` / `normalize_literal` 拒绝 bool | BOOLEAN 列与布尔字面量不可用 |
| G5 | `bind_expr` 无 `Or` / `Not` 分支 | 到达即抛 `E_TYPE_MISMATCH` |
| G6 | `LogicalProjection` 输出列名恒为原列名 | 限定投影与 JOIN 的 `SELECT *` 表头无法表达 |
| G7 | `_check_column` 不比较限定符 | 自连接下同名同型列的校验会误判 |
| G8 | `LogicalScan` 无 alias | EXPLAIN 无法区分自连接两侧 |

## 3. 设计原则

1. **Schema 自描述**：限定符可从列上导出，不额外维护来源表列表；避免两处状态不同步。
2. **绑定期一次完成**：名称解析、歧义、类型检查全部在 Builder 建树时完成；执行期只按
   `index` 取值，不再接触字符串。
3. **不可变**：节点与 Schema 仍是 frozen dataclass + tuple，优化器通过构造新节点改写。
4. **规范形优先**：`LogicalFilter.predicate` 顶层恒为 `BoundLogical(AND, ...)`，每个 term 是一个
   conjunct（可以是 OR / NOT 子树）。优化器按 conjunct 做拆分与下推，不需要先做形状归一。
5. **V1 语义不变**：无别名单表查询的限定符等于表名，未限定解析结果与 V1 一致，37 条
   Golden SQL 行为不变。

## 4. Schema 模型

### 4.1 LogicalColumn

```python
@dataclass(frozen=True, slots=True)
class LogicalColumn:
    """绑定后的列引用：来源表、限定符、列名、行位置与 SQL 类型。

    字段语义：
    - table：物理来源表名，来自 TableRef.name，用于 EXPLAIN 与错误信息；
    - qualifier：绑定与表头使用的限定符，别名优先，否则等于 table；
    - name：列名（已小写）；
    - index：该列在当前输入行值元组中的零基位置，随算子输出重新编号；
    - type：contracts.ast.SqlType。
    """

    table: str
    qualifier: str
    name: str
    index: int
    type: SqlType

    @classmethod
    def of(
        cls,
        table: str,
        name: str,
        index: int,
        type: SqlType,
        alias: str | None = None,
    ) -> "LogicalColumn":
        """按“别名优先，否则表名”的规则构造列，集中维护限定符不变式。"""
        return cls(table=table, qualifier=alias or table, name=name, index=index, type=type)
```

保留 `table` 而非只留 `qualifier`：自连接两侧（`users AS u1 JOIN users AS u2`）物理表相同、
限定符不同，EXPLAIN 与错误信息需要同时呈现两者。构造统一走 `LogicalColumn.of`，
限定符规则只实现一处。

不变式：

- `qualifier == alias or table`；
- 同一 Schema 内 `(qualifier, name)` 唯一（表内列名不重复，限定符在范围内唯一）；
- `index` 是相对当前节点输入行元组的位置，与建表列序无关。

### 4.2 LogicalSchema

```python
@dataclass(frozen=True, slots=True)
class LogicalSchema:
    columns: tuple[LogicalColumn, ...]

    @property
    def qualifiers(self) -> tuple[str, ...]:
        """按首次出现顺序去重后的限定符集合。"""

    def has_qualifier(self, qualifier: str) -> bool: ...

    def resolve(self, name: str, qualifier: str | None = None) -> LogicalColumn:
        """解析列引用；错误码见下表。"""

    def column(self, name: str) -> LogicalColumn:
        """无限定符解析的简写，等价 resolve(name, None)，供单表 DML 使用。"""
```

解析规则：

| 输入 | 匹配情况 | 结果 |
|---|---|---|
| `resolve(name, qualifier)` | 限定符存在且该来源下有该列 | 返回该列 |
| `resolve(name, qualifier)` | 限定符不在 Schema 中 | `E_TABLE_QUALIFIER_NOT_FOUND` |
| `resolve(name, qualifier)` | 限定符存在但该来源下无此列 | `E_COLUMN_NOT_FOUND` |
| `resolve(name, None)` | 恰好一列同名 | 返回该列 |
| `resolve(name, None)` | 无同名列 | `E_COLUMN_NOT_FOUND` |
| `resolve(name, None)` | 多列同名 | `E_AMBIGUOUS_COLUMN` |

检查顺序固定为“先限定符、后列名”：`SELECT x.id FROM users u` 中 `x` 不存在时抛
`E_TABLE_QUALIFIER_NOT_FOUND`，而不是 `E_COLUMN_NOT_FOUND`。

`qualifiers` 由列导出，不单独存储来源表列表。来源表至少有一列（`colDef` 不允许空表），
因此限定符集合不会丢失信息。Schema 规模为查询涉及的列数，线性扫描即可，不引入缓存。

### 4.3 Schema 拼接（JOIN）

```python
def join_schema(left: LogicalSchema, right: LogicalSchema) -> LogicalSchema:
    """按左列在前、右列在后的顺序拼接，并重新编号 index。"""
```

规则：

1. 左右限定符集合必须不相交，否则抛 `E_DUP_TABLE_ALIAS`；
2. 输出列顺序为左子树全部列后接右表全部列（契约 §3.3）；
3. 所有列的 `index` 从 0 重新编号，`table` / `qualifier` / `name` / `type` 原样保留。

拼接是 `LogicalJoin` 的 Schema 契约，也是重复限定符检查的兜底位置：Builder 在建范围时
先查一次给出更贴近 SQL 的错误信息，`LogicalJoin.__post_init__` 再查一次保证不变式。

### 4.4 LogicalScan

```python
@dataclass(frozen=True, slots=True)
class LogicalScan(LogicalPlan):
    table: str                      # 物理表名，传给 Storage.scan
    schema: LogicalSchema           # 列上的 qualifier 已按 alias 写入
    alias: str | None = None        # 仅用于 EXPLAIN 与优化器识别来源

    @property
    def qualifier(self) -> str:
        return self.alias or self.table
```

`schema` 中的限定符是绑定的唯一依据；`alias` 字段不参与解析，只让计划树自描述
（自连接的两侧 Scan 打印为 `LogicalScan[users AS u1]` / `LogicalScan[users AS u2]`）。

### 4.5 LogicalJoin（M2 落地，本文只定 Schema 契约）

```python
@dataclass(frozen=True, slots=True)
class LogicalJoin(LogicalPlan):
    left: LogicalPlan
    right: LogicalPlan
    on: BoundExpr                   # 顶层为 BoundLogical(AND)，每个 conjunct 结果为 BOOLEAN
    schema: LogicalSchema           # join_schema(left.output_schema, right.output_schema)
    kind: JoinType = JoinType.INNER
```

`__post_init__` 校验：

- `schema` 与 `join_schema(left.output_schema, right.output_schema)` 一致；
- `on` 中每个 `BoundColumnRef` 都能在 `schema` 中按 `index` 定位（复用 `_check_expr_columns`）；
- `on` 的每个 conjunct 类型为 BOOLEAN。

链式 JOIN 按书写顺序构造左深树，第 i 个 JOIN 的 `on` 绑定在“左侧累积 Schema + 当前右表
Schema”上，因此引用后续表时该限定符尚不存在，自然抛 `E_TABLE_QUALIFIER_NOT_FOUND`。

### 4.6 LogicalProjection 与结果表头

表头规则来自契约 §3.3，需要在计划中显式保存，因为“是否限定”由 SQL 书写决定，无法从列上推导：

| 投影写法 | 表头 |
|---|---|
| 单表 `SELECT *` | 原列名 |
| JOIN 的 `SELECT *` | `限定符.列名` |
| 显式限定投影 `SELECT u.name` | `限定符.列名` |
| 未限定投影 `SELECT name` | 原列名 |

```python
@dataclass(frozen=True, slots=True)
class LogicalProjection(LogicalPlan):
    columns: tuple[BoundColumnRef, ...]
    output_names: tuple[str, ...]   # 结果表头，与 columns 等长且一一对应
    child: LogicalPlan
```

`__post_init__` 校验两者长度一致；`output_schema` 用 `output_names[i]` 作为输出列的 `name`，
其余字段（`table` / `qualifier` / `type`）沿用来源列，`index` 从 0 重新编号。

这样 `SelectExecutor` 现有的 `column.name for column in output_schema.columns` 直接得到正确表头，
执行器零改动。`LogicalProjection.columns` 仍是绑定列引用，优化器的投影裁剪只读它，不受表头影响。

## 5. 表达式与类型系统升级

### 5.1 类型表示收敛

`SqlType` 已含 `BOOLEAN`，删除表达式层的临时标记：

- 删除模块级常量 `BOOLEAN` 与类型别名 `TypeKind`；
- `deduce_type(expr) -> SqlType`，比较 / 逻辑 / NOT 节点返回 `SqlType.BOOLEAN`；
- `_type_name` 直接取 `kind.value`；
- `BoundLiteral.type` 继续为 `SqlType`，现在可以承载布尔字面量。

类型体系只剩一套表示，后续判断（如 `E_BOOLEAN_REQUIRED`）统一写成 `is SqlType.BOOLEAN`。

### 5.2 布尔字面量与规范化

```python
def bind_literal(value: Value) -> BoundLiteral:
    # bool 是 int 子类，必须最先判断
    if type(value) is bool:
        return BoundLiteral(value, SqlType.BOOLEAN)
    if isinstance(value, int):
        return BoundLiteral(value, SqlType.INT)
    ...
```

`normalize_literal(value, target)` 增加 BOOLEAN 分支：仅接受 `type(value) is bool`；INT 分支继续
显式拒绝 bool（`isinstance(value, int) and not isinstance(value, bool)` 的既有写法保持不变）。
`cast_value` 不接受 BOOLEAN 目标，V2 无布尔显式 CAST。

### 5.3 比较规则

`coerce(left, right)` 的数值提升规则不变（INT 与 REAL 比较时 INT 侧提升为 REAL），
在其后增加布尔操作符校验：

| 左 / 右类型 | `=` `<>` | `<` `<=` `>` `>=` |
|---|---|---|
| BOOLEAN / BOOLEAN | 合法 | `E_TYPE_MISMATCH` |
| BOOLEAN / 其他 | `E_TYPE_MISMATCH` | `E_TYPE_MISMATCH` |
| INT / REAL | 合法（数值提升） | 合法 |
| TEXT / TEXT | 合法 | 合法 |
| TEXT / 数值 | `E_TYPE_MISMATCH` | `E_TYPE_MISMATCH` |

校验放在协调之后，此时两侧类型已经一致，只需判断“同型为 BOOLEAN 且操作符不是 `=`/`<>`”。
不新增错误码：布尔大小比较属于类型不匹配。

### 5.4 OR / NOT 绑定

`bind_expr` 增加两个分支：

```python
case Or():
    terms = [bind_expr(leaf, schema) for leaf in _flatten(node, Or)]
    for term in terms:
        require_boolean(term, "OR")
    return BoundLogical(LogicOp.OR, tuple(terms))

case Not():
    operand = bind_expr(node.operand, schema)
    require_boolean(operand, "NOT")
    return BoundUnaryNot(operand)
```

- 展平函数由 `_flatten_and` 泛化为 `_flatten(node, node_type)`：只展平同层同类型节点，
  保持从左到右顺序；
- 优先级由 A 在 AST 阶段决定，绑定层不再处理；`a = 1 AND b = 2 OR c = 3` 的 AST 为
  `Or(And(...), Cmp(...))`，绑定结果为 `BoundLogical(OR, (BoundLogical(AND, ...), cmp))`；
- `NOT` 不做德摩根改写，去括号与常量化简属于 M3 优化器；
- `require_boolean(expr, context)` 统一抛 `E_BOOLEAN_REQUIRED`，消息中包含上下文与实得类型。

### 5.5 谓词顶层规范形

```python
def bind_conjunction(node: Expr, schema: LogicalSchema) -> BoundLogical:
    """WHERE / ON 绑定入口：返回顶层为 AND 的合取。"""
    bound = bind_expr(node, schema)
    if not (isinstance(bound, BoundLogical) and bound.op is LogicOp.AND):
        bound = BoundLogical(LogicOp.AND, (bound,))
    for term in bound.terms:
        require_boolean(term, "WHERE/ON")
    return bound
```

规则：

- 顶层是 AND：原样使用，保持展平结果；
- 顶层是 OR / NOT / 比较 / 布尔列 / 布尔字面量：包一层单元素 AND。

顶层统一为 AND 的好处是优化器的 conjunct 列表直接取 `predicate.terms`，
`WHERE a = 1 OR b = 2` 整体作为一个 conjunct，天然满足“OR 跨来源不拆开下推”的约束。
`LogicalFilter.__post_init__` 的不变式同步收紧为“顶层为 `BoundLogical` 且 `op` 为 AND、`terms` 非空”。

### 5.6 E_BOOLEAN_REQUIRED 检查点

| 位置 | 要求 |
|---|---|
| WHERE 的每个 conjunct | 类型为 BOOLEAN |
| JOIN ON 的每个 conjunct | 类型为 BOOLEAN |
| `BoundLogical` 的每个 term | 类型为 BOOLEAN |
| `BoundUnaryNot.operand` | 类型为 BOOLEAN |

比较操作数不要求 BOOLEAN（它们要求类型可比较），因此 `WHERE 1 = 1`、`WHERE TRUE`、`WHERE flag`
合法，`WHERE 1`、`WHERE age`（age 为 INT）抛 `E_BOOLEAN_REQUIRED`。

### 5.7 求值

`eval_expr` 已实现 OR 短路、NOT 取反和布尔列取值，本次升级只需确认不变量：

- 比较与逻辑运算返回 Python `bool`；
- `Value` 已包含 `bool`，无需改变行元组模型；
- `FilterExecutor` 只保留求值为 True 的行；
- V2 无 NULL，保持二值逻辑。

## 6. 名称绑定流程（Builder）

### 6.1 单表与别名

```python
def _build_source(self, ref: TableRef) -> tuple[LogicalSchema, LogicalScan]:
    info = self._describe_table(ref.name)          # 表不存在由 Storage 抛 E_TABLE_NOT_FOUND
    schema = LogicalSchema(tuple(
        LogicalColumn.of(
            table=ref.name,
            name=column.name,
            index=index,
            type=column.type,
            alias=ref.alias,
        )
        for index, column in enumerate(info.columns)
    ))
    return schema, LogicalScan(table=ref.name, schema=schema, alias=ref.alias)
```

INSERT / UPDATE / DELETE 的 `table` 是 `str`（契约未引入别名），走 `ref = TableRef(table)` 的
等价路径，限定符等于表名。

### 6.2 范围限定符唯一性

Builder 在把右表加入 JOIN 范围时，先比较限定符集合，冲突抛 `E_DUP_TABLE_ALIAS`：

```python
if set(left_schema.qualifiers) & set(right_schema.qualifiers):
    raise SqlError(E_DUP_TABLE_ALIAS, f"duplicate table alias: {...}")
```

无别名的自连接（`FROM users JOIN users`）两侧限定符都是 `users`，同样被拒绝。

### 6.3 ON 的绑定范围

左深构造顺序：

```text
plan = Scan(FROM 表)
for join in statement.joins:
    right_schema, right_scan = _build_source(join.right)
    merged = join_schema(plan.output_schema, right_schema)
    on = bind_conjunction(join.on, merged)
    plan = LogicalJoin(left=plan, right=right_scan, on=on, schema=merged)
```

`on` 只绑定在 `merged`（左侧全部 + 当前右表）上，因此：

- 引用后续表 → `E_TABLE_QUALIFIER_NOT_FOUND`；
- 未限定列在左右两侧同名 → `E_AMBIGUOUS_COLUMN`。

### 6.4 SELECT 列表与表头

```python
def _bind_projection(columns, input_schema, *, star_qualified):
    if columns is None:                       # SELECT *
        refs = tuple(BoundColumnRef(c) for c in input_schema.columns)
        names = tuple(
            f"{c.qualifier}.{c.name}" if star_qualified else c.name
            for c in input_schema.columns
        )
    else:
        refs, names = [], []
        for item in columns:
            col = input_schema.resolve(item.name, item.qualifier)
            refs.append(BoundColumnRef(col))
            names.append(f"{col.qualifier}.{col.name}" if item.qualifier else col.name)
    return tuple(refs), tuple(names)
```

`star_qualified` 取“查询中存在 JOIN”，即 `bool(statement.joins)`：单表 `SELECT *` 保持原列名，
JOIN 的 `SELECT *` 使用 `限定符.列名`。

### 6.5 UPDATE / DELETE / INSERT

- UPDATE / DELETE 的 WHERE 走 `bind_conjunction`，Schema 为单表（限定符等于表名），
  未限定列解析与 V1 一致；
- UPDATE 的赋值列仍是无限定符字符串，使用 `schema.column(name)`；
- INSERT 按 `schema.columns` 顺序与值列表 zip 后规范化，BOOLEAN 列接受 `True` / `False`；
- 三者的 Scan 不设 alias。

## 7. 错误码与触发点

| 错误码 | 触发点 |
|---|---|
| `E_TABLE_QUALIFIER_NOT_FOUND` | `resolve` 中限定符不在当前 Schema；ON 引用后续表 |
| `E_AMBIGUOUS_COLUMN` | 未限定列在多个来源中同名 |
| `E_COLUMN_NOT_FOUND` | 限定符存在但该来源无此列；未限定列零匹配 |
| `E_DUP_TABLE_ALIAS` | JOIN 范围限定符冲突；`join_schema` 兜底校验 |
| `E_BOOLEAN_REQUIRED` | WHERE / ON / AND / OR / NOT 的操作数不是 BOOLEAN |
| `E_TYPE_MISMATCH` | BOOLEAN 参与大小比较；BOOLEAN 与数值比较；其他跨类型比较 |
| `E_TABLE_NOT_FOUND` | 沿用 `Storage.describe` |

消息统一用英文短句，包含限定符 / 列名 / 上下文，保证测试断言稳定。

## 8. 对下游的影响

| 位置 | 变化 |
|---|---|
| `plans.py::_check_column` | 比较字段加入 `qualifier`，避免自连接下同名同型列误判 |
| `plans.py::LogicalFilter.__post_init__` | 增加 `predicate.op is LogicOp.AND` 断言 |
| `plans.py::LogicalProjection` | 新增 `output_names`，`output_schema` 用表头作为列名 |
| `plans.py::LogicalScan` | 新增 `alias` |
| `executor/dql.py` | 零改动：`SelectExecutor` 从 `output_schema` 取表头，投影按 `index` 取值 |
| `executor/dml.py` | 零改动：赋值按绑定列 `index` 写入 |
| `explain.py` | 列打印为 `限定符.列名@index`，Scan 打印 `表名 AS 别名` |
| M3 优化器 | 常量折叠、AND 拆分直接消费顶层 conjunct；下推按 `BoundColumnRef.qualifier` 与子树 Schema 的限定符集合判断归属 |

## 9. 兼容性

- 无别名单表查询：`qualifier == table`，`resolve(name, None)` 唯一匹配，行为与 V1 一致；
- `SELECT *` 单表表头仍为原列名；
- V1 的 AND 展平、隐式 INT→REAL 提升、`bool` 被拒绝为 INT/REAL 的规则全部保留；
- `LogicalColumn` 新增必填字段，所有构造点（Builder、Projection 输出、JOIN 拼接）同步更新；
- 37 条 Golden SQL 作为回归基线，必须在升级后全部通过。

## 10. 测试方案

**Schema 解析**

- 限定符命中、限定符不存在、限定符存在但列不存在；
- 未限定唯一命中、零匹配、多来源同名歧义；
- `qualifiers` 顺序与去重；`has_qualifier`；
- `join_schema` 的列顺序、index 重编号、重复限定符报错。

**BOOLEAN**

- 布尔字面量绑定为 `SqlType.BOOLEAN`；
- BOOLEAN 列 INSERT / UPDATE 接受 `True` / `False`，拒绝 int / str；
- INT / REAL 列继续拒绝 bool；
- `flag = TRUE`、`flag <> FALSE` 合法；`flag > TRUE` 抛 `E_TYPE_MISMATCH`；
- `flag = 1` 抛 `E_TYPE_MISMATCH`。

**OR / NOT**

- `NOT > AND > OR` 的 AST 优先级在绑定结果中保持；
- 同层展平：`a AND b AND c` 三个 term，`a OR b OR c` 三个 term；
- 嵌套：`AND` 内出现 `OR`、`NOT`，顶层规范形为单元素 AND；
- `WHERE 1`、`WHERE age`、`NOT age` 抛 `E_BOOLEAN_REQUIRED`；
- 短路求值：OR 遇 True 提前返回，NOT 取反。

**限定列与别名**

- 单表别名：`SELECT u.name FROM users AS u` 表头为 `u.name`；
- 自连接：同表两个别名各自解析成功，未限定同名列抛 `E_AMBIGUOUS_COLUMN`；
- `FROM users JOIN users` 抛 `E_DUP_TABLE_ALIAS`；
- ON 引用后续表抛 `E_TABLE_QUALIFIER_NOT_FOUND`；
- `SELECT *` 单表 / JOIN 的表头规则。

**回归**

- 37 条 Golden SQL 全部通过；
- `explain` 快照覆盖别名、限定列、OR / NOT 打印。

## 11. 实施顺序

1. `base.py`：`LogicalColumn` 加 `qualifier` 与 `of` 工厂；`LogicalSchema` 实现
   `qualifiers` / `has_qualifier` / `resolve` / `column`；新增 `join_schema`。
2. `expressions.py`：删除 BOOLEAN 标记与 `TypeKind`；布尔字面量绑定与规范化；
   布尔比较操作符校验；`require_boolean`；`Or` / `Not` 绑定分支；`_flatten` 泛化；
   `bind_conjunction` 顶层规范形。
3. `plans.py`：`_check_column` 加限定符比较；`LogicalFilter` 顶层断言；
   `LogicalProjection.output_names`；`LogicalScan.alias`；`LogicalJoin`（M2）。
4. `builder.py`：`_build_source` 支持 `TableRef` 与别名；JOIN 左深构造与限定符唯一性；
   投影绑定与表头生成；DML 路径适配新字段。
5. 更新 `explain.py` 打印；补齐第 10 节测试；跑通 37 条 Golden SQL 回归。
6. 优化器接入前，先以 Schema 与绑定层的计划快照测试锁定规范形。

## 12. 验收标准

- 限定列、别名、自连接、歧义与重复别名的行为与契约 §3.3 完全一致；
- BOOLEAN 在字面量、列、比较、WHERE / ON、AND / OR / NOT 全链路可用，非 BOOLEAN 操作数抛
  `E_BOOLEAN_REQUIRED`；
- OR / NOT 的绑定结果保持 A 提供的优先级，谓词顶层恒为 AND 合取；
- 结果表头满足契约四种写法；
- 执行器与存储层无需改动即可消费升级后的计划；
- V1.1 的 37 条 Golden SQL 与全部既有测试通过。
