# LogicalPlan 绑定表达式模型设计（V2）

## 1. 文档目标

本文给出 `runner/logical_plan/expressions.py` 的设计方案：实现**通用表达式树**（节点、绑定、
求值、打印全部就位），并把比较类型泛化为任意表达式参与比较。

**当前需求按 `contracts/ast.py`（V1，冻结）实现**：绑定入口只接收 V1 AST 子集
（`Cmp | And`、左列右字面量、AND 连接），语义与 V1 完全一致。OR / NOT / 算术 / 显式 CAST
等节点本版**先就位**（类、求值、打印已备好），当前仅有统一树形态、无输入；等将来 AST
契约升级后，在 `bind_expr` 增加分支即可接入，节点与求值/打印零改动。

## 2. 现状分析

### 2.1 V1 表达式模型

- AST（`contracts.ast`）：`Expr = Cmp | And`；`Cmp(left: Column, op: str, right: Literal)`；
  `And` 二叉，无括号、无 OR。
- Bound（V1 设计，尚未实现）：`BoundColumnRef` / `BoundLiteral` / `BoundComparison` /
  `BoundConjunction`。
- WHERE 顶层绑定为 `BoundConjunction.terms`，逐项短路求值。

### 2.2 V1 的硬限制

1. 无括号——`a = 1 AND b = 2 OR c = 3` 无法表达正确优先级；
2. 无 OR、NOT；
3. 比较左侧只能列、右侧只能字面量——`a = b`、`a + 1 > 2` 均不支持；
4. 谓词不是值——比较结果不能继续参与计算；
5. 无算术、无 CAST。

### 2.3 为什么现在建统一树（且只建树枝，不动契约）

1. **V1 的 Bound 迟早要实现**。与其按 V1 形状实现一遍（专用 `terms` 短路求值路），
   不如一步到位建统一表达式树：比较、AND/OR/NOT、算术、CAST 全是普通节点，
   绑定、求值、打印逻辑每个只有一份，不存在"两套谓词表示 + 每个消费方都要认识两套"。
2. **节点先行只花类定义成本**。当前输入虽窄，但统一树吃掉 V1 全部现状（AND 展平、短路、
   隐式类型转换），不存在死设计：本版不可达的分支（OR/NOT/算术/显式 CAST）在契约升级后
   是**纯增量接入**，不用重构。
3. **不承诺 V2 能力边界**。V2 目标是"表达式子系统就位、V1 语义不变"，不是"现在就能写
   `a OR b`"——后者等契约升级（见 §4）。

## 3. V2 范围（最小集）

| 项目 | V2 定位 |
|---|---|
| 契约 | **零改动**：`ast.py`、`SqlType`（INT/TEXT/REAL）、`Value` 均保持 V1 |
| 节点 | `BoundColumnRef / BoundLiteral / BoundComparison / BoundLogical(AND/OR) / BoundUnaryNot / BoundArith / BoundCast` 全定义 |
| 输入可达性 | 当前可达：`BoundColumnRef / BoundLiteral / BoundComparison（窄形态）/ BoundLogical(AND) / 隐式 BoundCast`；预留（无输入）：`BoundUnaryNot / BoundArith / 显式 BoundCast / 比较两端宽形态` |
| 比较 | 类型上左右两侧均为任意 `BoundExpr`；当前实际输入仅 (列, 字面量) |
| 兼容 | 类名即 V1 旧名（`BoundColumnRef` 等），无需别名 |
| 谓词使用方 | `LogicalFilter.predicate` 切换为 `BoundExpr`（顶层恒为 `BoundLogical`） |
| 不在 V2 | NULL/三值逻辑、FuncCall、聚合、子查询、SELECT 列表与赋值泛化；OR/NOT/算术/显式 CAST 的**绑定入口**（依赖 AST 契约升级） |

## 4. 契约：本版零改动（V1 保持不变）

`ast.py` 保持冻结，本设计严格遵守现有契约，不做任何契约升级。自动满足/不冲突的约束：

1. `SqlType` 仍为 INT/TEXT/REAL；**BOOLEAN 不进入任何 `SqlType` 字段**——它只是 bound
   内部类型推导函数的返回值标记（见 §5.3），不落字段、不流向存储层与投影；
2. 不新增布尔字面量：`Value = int | str | float` 保持不变，`BoundLiteral` 只承载
   INT/TEXT/REAL 值；
3. 顶层语句不动（`SelectStmt.columns`、`Assignment.value` 等仍为列名/字面量）；
4. 绑定行为语义与 V1 一致：WHERE 顶层恒为 `BoundLogical(op=AND)`，单比较也包一层。

**未来契约升级（三方同意后）的接入方式**（本文已预留通路）：

- `ast.py` 新增 `Or` / `Not` / `Arith` / `Cast` 节点，`SqlType` 增加 `BOOLEAN`，
  同步 `docs/contract-v1.md` 与 `tests/golden_sql.py`；
- 绑定侧只需在 `bind_expr` 为新增节点各加一个分支（§6 已留 TODO 位置）；
- `expressions.py` 节点定义、求值（§7）、打印（§9）无需改动。

## 5. 类型设计（expressions.py）

### 5.1 枚举

```python
class ComparisonOp(Enum):
    EQ = "="
    NE = "<>"
    LT = "<"
    LE = "<="
    GT = ">"
    GE = ">="


class ArithOp(Enum):
    ADD = "+"
    SUB = "-"
    MUL = "*"
    DIV = "/"
    MOD = "%"


class LogicOp(Enum):
    AND = "AND"
    OR = "OR"
```

### 5.2 节点

子类统一带 `Bound` 前缀（与 V1 已定名的旧名一致，天然兼容）。

```python
class BoundExpr(ABC):
    """所有绑定表达式的基类。"""


@dataclass(frozen=True, slots=True)
class BoundColumnRef(BoundExpr):
    column: LogicalColumn


@dataclass(frozen=True, slots=True)
class BoundLiteral(BoundExpr):
    value: Value
    type: SqlType


@dataclass(frozen=True, slots=True)
class BoundComparison(BoundExpr):
    left: BoundExpr        # 类型宽；当前实际仅左列右字面量
    op: ComparisonOp
    right: BoundExpr


@dataclass(frozen=True, slots=True)
class BoundLogical(BoundExpr):
    op: LogicOp
    terms: tuple[BoundExpr, ...]  # 非空；允许嵌套 BoundLogical（当前仅 AND 可达）


@dataclass(frozen=True, slots=True)
class BoundUnaryNot(BoundExpr):   # 预留：无 AST 输入，等契约升级
    operand: BoundExpr


@dataclass(frozen=True, slots=True)
class BoundArith(BoundExpr):      # 预留：无 AST 输入，等契约升级
    left: BoundExpr
    op: ArithOp
    right: BoundExpr


@dataclass(frozen=True, slots=True)
class BoundCast(BoundExpr):
    expr: BoundExpr
    # 显式 CAST 预留；隐式 CAST（类型协调插入）当前可达
    target: SqlType
```

### 5.3 类型推导约定（结果类型不入字段）

| 节点 | 结果类型 |
|---|---|
| `BoundColumnRef` | `column.type` |
| `BoundLiteral` | `type` 字段 |
| `BoundComparison` / `BoundLogical` / `BoundUnaryNot` | `BOOLEAN`（内部标记，见下） |
| `BoundArith` | 绑定后协调出的类型（INT 或 REAL） |
| `BoundCast` | `target` |

- **BOOLEAN 是 bound 内部类型推导标记，不是 `SqlType` 值**：`SqlType` 冻结无 BOOLEAN，
  因此推导函数对 BOOLEAN 的返回用 bound 内部约定（如模块级常量/内部枚举），只用于
  §8.1 的绑定期一致性校验，不进入任何字段、存储层与投影；与 V1 契约零交集。
- 一致性由绑定（§6）保证，求值不需要类型信息（§7）。BOOLEAN 只由比较/逻辑运算产生，
  不进入 `BoundLiteral`，因此 `Value` 与存储层零变化。

### 5.4 与 V1 的名字对照

| V1 名称 | V2 名称 | 说明 |
|---|---|---|
| `BoundColumnRef` | `BoundColumnRef` | **同名即同物**，无需别名 |
| `BoundLiteral` | `BoundLiteral` | 同上 |
| `BoundComparison` | `BoundComparison` | 同上；字段名保留，字段类型放宽 |
| `BoundAssignment` | 不变 | 值仍为 `BoundLiteral` |

## 6. 绑定规则（bind_expr）

入口：

```python
def bind_expr(node: Expr, schema: LogicalSchema, ctx: BindContext) -> BoundExpr:
```

按节点类型递归绑定，**当前只实现 V1 可达分支**（CTX 顶层 `Expr = Cmp | And`）：

1. `Column` → `schema.column(name)` 查 `LogicalColumn`，构造 `BoundColumnRef`（失败抛
   `E_COLUMN_NOT_FOUND`，index 取自 LogicalColumn）；
2. `Literal` → 按上下文目标类型规范化（见 §6.2），构造 `BoundLiteral`；
3. `Cmp` → 绑定左右，类型协调（见 §6.1），必要时插入隐式 `BoundCast`，构造
   `BoundComparison`；
4. `And` → 递归绑定后**同层展平**（见 §6.3），构造 `BoundLogical(op=AND)`。

**预留分支**（`ast.py` 升级后接入，本版无输入；到达即断言失败/抛内部错误）：

5. `Not` → 要求操作数结果为 BOOLEAN（内部标记）；
6. `Arith` → 左右数值类型协调，结果类型 = 协调后类型；
7. `Cast` → `target` 合法类型且禁止 BOOLEAN。

### 6.1 类型协调（隐式转换）

| 左 / 右 | 规则 | 结果 |
|---|---|---|
| INT, INT | 不变 | INT |
| INT, REAL | 左侧插 `BoundCast(INT→REAL)` | REAL |
| REAL, INT | 右侧插 `BoundCast(INT→REAL)` | REAL |
| TEXT, TEXT | 仅比较合法；算术与逻辑报错 | TEXT |
| BOOLEAN, BOOLEAN | 仅逻辑合法（预留分支生效后） | BOOLEAN |
| 其他组合 | `E_TYPE_MISMATCH` | — |

协调语义（**当前可达场景**）：

- 字面量优先按§6.2 规则规范化到目标类型，**不需要**插 `BoundCast`：REAL 列 vs INT
  字面量 `18` → 字面量规范化为 `REAL(18.0)`，比较树中无 Cast；
- 仅**非字面量侧**需要类型转换时才插 Cast：当前唯一真实场景是 **INT 列 vs REAL
  字面量** `18.5` → 左侧插 `BoundCast(INT→REAL)`，比较树为
  `CAST(age AS REAL) = REAL(18.5)`；
- 上述规则表按左右两条泛化书写，为将来表达式参与比较预留（届时列侧换为任意表达式）。

其余约定：

- TEXT 不参与算术/比较混合（`E_TYPE_MISMATCH`）；
- BOOLEAN 不可作为 Cast 目标、不做算术；比较两侧不允许 BOOLEAN（无布尔字面量，
  V1 输入下不存在合法的布尔比较场景）；
- 除法语义（预留分支生效后）：INT 相除截断为 INT（与 SQL 一致，`5 / 2 = 2`）；
  任一侧为 REAL 则结果 REAL。

### 6.2 字面量规范化

沿用 V1 规则表：

| 目标类型 | 合法 Python 值 | 规范化后 |
|---|---|---|
| INT | `int` 且非 `bool` | `int` 原样 |
| TEXT | `str` | 原样 |
| REAL | `int` 或 `float`，非 `bool` | `float` |

（`bool` 是 `int` 子类，因此 INT/REAL 校验必须显式拒绝 bool——延续 V1 规则
"且不是 bool"。BOOLEAN 类型不接受任何字面量，本版不存在。）

### 6.3 展平与嵌套

- AST 二叉递归展平为 `BoundLogical(AND, terms)`（**当前仅 AND**；OR 展平等契约升级后
  同法：`BoundLogical(OR, terms)`）；
- 仅同层展平：`a=1 AND b=2 AND c=2`
  → `BoundLogical(AND, (cmp_a, cmp_b, cmp_c))`；
- WHERE 顶层恒为 `BoundLogical(AND, terms)`（单比较也包一层
  `BoundLogical(AND, (cmp,))`）。

### 6.4 错误码

全部沿用现有 `SqlError` 码：`E_COLUMN_NOT_FOUND`、`E_TYPE_MISMATCH`，V2 不新增错误码。

## 7. 求值模型（Executor 侧）

```python
def eval_expr(expr: BoundExpr, row: tuple) -> Value:
    match expr:
        case BoundColumnRef():
            return row[expr.column.index]
        case BoundLiteral():
            return expr.value
        case BoundComparison(l, op, r):
            return cmp_eval(op, eval_expr(l, row), eval_expr(r, row))
        case BoundLogical(op, terms):
            # AND：遇 FALSE 立即返回 False；OR：遇 TRUE 立即返回 True —— 短路
            # 当前仅 AND 有输入；OR 分支按同法实现，等契约升级
        case BoundUnaryNot(operand):   # 预留：当前无输入
            return not eval_expr(operand, row)
        case BoundArith(l, op, r):     # 预留：当前无输入
            return arith_eval(op, eval_expr(l, row), eval_expr(r, row))
        case BoundCast(inner, target): # 隐式 CAST 当前可达；显式等契约升级
            return cast_value(eval_expr(inner, row), target)
```

- 求值只需要 `row`：列已绑定 index（`row[column.index]`），不再查 Schema，
  与 V1 契约"values[column_index] 直接取值"一致；
- 比较结果统一为 Python `bool`；
- **V2 无 NULL**：`Value` 不含 None，比较/逻辑为两值逻辑，Filter 只保留结果为 True 的行。
  V3 引入 NULL 时再改为 Kleene 三值逻辑（实现处留注释）。

## 8. 消费方变化

### 8.1 LogicalFilter

```python
@dataclass(frozen=True, slots=True)
class LogicalFilter(LogicalPlan):
    predicate: BoundExpr   # 顶层恒为 BoundLogical(AND)
    child: LogicalPlan
```

不变式更新（对应旧 plan 设计 §11 第 2 条）：

- `LogicalFilter.predicate` 顶层恒为 `BoundLogical` 且 `terms` 非空；
- 顶层 `BoundLogical` 的每个 term 结果类型必须为 BOOLEAN（§5.3 内部标记；
  `BoundComparison` / `BoundLogical` / `BoundUnaryNot`）；
- 每个 `BoundColumnRef` 均能按 `index` 在直接 child 输入 Schema 中定位（原有不变式不变）。

### 8.2 其他消费方（V2 不动）

- `LogicalProjection.columns: tuple[BoundColumnRef, ...]` 保持 V1（AST `columns` 仍是列名）；
- `BoundAssignment.value: BoundLiteral` 保持 V1（AST 赋值值仍是字面量）；
- 存储层、`LogicalColumn` / `LogicalSchema`、行元组切换模型零变化。

### 8.3 SELECT 列表 / 赋值泛化

`SELECT a + 1 FROM t`、`SET a = a + 1` 需要 AST 层先支持表达式列/赋值（`columns`、
`Assignment.value` 泛化为 `Expr`），属于后续契约升级，V2 只覆盖 WHERE 谓词。

## 9. 打印约定（explain.py）

格式化时对表达式递归打印，沿用 V1 风格"表名.列名@index"：

```text
LogicalFilter[AND((users.age@2 >= REAL(18.0)), (users.name@1 = TEXT("alice")))]
LogicalFilter[AND((users.score@3 >= REAL(18.0)))]
LogicalFilter[AND((CAST(users.age@2 AS REAL) = REAL(18.5)))]
```

规则：

- `BoundLiteral` → `INT(1)` / `REAL(18.0)` / `TEXT("alice")`；
- `BoundColumnRef` → `users.age@2`；
- `BoundComparison` → `left op right`；`BoundLogical` → `AND(...)` / `OR(...)`；
- `BoundUnaryNot` → `NOT(x)`；`BoundArith` → `(l op r)`；`BoundCast` → `CAST(x AS REAL)`。

其中 `OR(...)` / `NOT(x)` / `(l op r)` 输出格式本版定义好、无快照用例，
随契约升级一并验证（§10）。

## 10. 测试方案

建议新增/扩展 `tests/runner/logical_plan/`：

**当前（V1 输入）可达**：

- 绑定正常路径：AND 展平、嵌套 And、隐式 Cast 插入位置（INT 列 vs REAL 字面量 →
  Cast 包在 INT 列侧）、REAL 列 vs INT 字面量 → 无 Cast（字面量规范化）、TEXT 比较合法、
  bool 作为 INT/REAL 字面量拒绝；
- 绑定错误路径：TEXT 列 vs INT 字面量比较（`E_TYPE_MISMATCH`）；
- 求值：短路（AND：先 FALSE 的 term 后 term 不求值）、隐式 Cast（INT→REAL）行为、
  布尔结果过滤语义；
- 兼容层：V1 旧类型名（`BoundColumnRef` / `BoundLiteral` / `BoundComparison`）正常导入构造，
  WHERE 顶层为 `BoundLogical(AND, ...)`；
- explain 快照：§9 前三行稳定打印断言。

**契约升级后补**（本版先不写用例）：OR 展平、NOT、列列比较、显式 CAST、算术
（含 INT 除截断）、`OR(...)` / `NOT(x)` / `(l op r)` 打印快照。

## 11. 实施顺序

1. `expressions.py`：新模型（枚举 + Bound* 类），无任何契约改动；
2. 实现 `bind_expr`（仅 §6 的 V1 可达分支；预留分支以显式断言/TODO 落位），
   与 `LogicalPlanBuilder` 绑定辅助函数整合；
3. `LogicalFilter.predicate` 切换为 `BoundExpr`，Builder 的 WHERE 绑定改走 `bind_expr`；
4. `explain.py` 表达式打印；
5. 补齐 §10 当前可达的测试；
6. 同步更新 `logical-plan-dev-design.md` 中 §7.2 / §9 / §11 的类型名引用。

## 12. 后续扩展

- **契约升级**（三方确认后，见 §4）：`ast.py` 增 `Or` / `Not` / `Arith` / `Cast` 节点、
  `SqlType.BOOLEAN`；绑定层仅加 `bind_expr` 分支；
- **V2.5**：`FuncCall` + 函数注册表（签名、返回类型、隐式转换），谓词可为函数调用；
- **V3**：NULL 与三值逻辑；聚合函数；标量子查询；SELECT 列表与赋值泛化（再次契约升级）。
