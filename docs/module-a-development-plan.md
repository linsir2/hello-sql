# 模块 A（编译层）开发计划书

## 1. 文档目的与当前范围

本计划书用于指导模块 A 的开发与交接。项目目标是实现一个最小 SQL Demo：用户输入一条 SQL，系统返回查询结果或约定的错误。

模块 A 的唯一职责是：

```text
一条 SQL 文本 -> 词法分析 + 语法分析 -> contracts.ast.Statement
```

模块 A 的工作到 AST 成功产出（或语法错误被报告）即结束。它不执行 SQL，也不访问任何表、数据文件或存储接口。

本计划以 `docs/contract-v1.md`、`contracts/ast.py`、`contracts/errors.py` 与 `tests/golden_sql.py` 为准。图片中的分工也采用同一边界：A 只交付 AST，C 负责语义检查和执行，B 只提供表级存储调用。

## 2. 模块边界与协作关系

### 2.1 数据流

```text
用户 SQL
  |
  v
A: compiler.parse(sql)
  |
  +-- 成功：Statement（AST） ----> C: Runner
  |
  +-- 失败：ParseError(E_SYNTAX) -> REPL / 调用者

C: 依据 AST 做表、列、类型校验和 WHERE 求值
  |
  v
B: Storage 的 create_table / describe / scan / insert / update_row / delete_row
```

### 2.2 A 的职责

- 识别 SQL 中的 token（关键字、标识符、数字、字符串、符号）。
- 按 V1 文法解析六类语句，构建规定的 AST 数据类。
- 将表名、列名、标识符统一转成小写。
- 将字面量转为 Python 原生值：整数为 `int`、小数为 `float`、字符串为 `str`。
- 对所有词法和语法错误抛出 `contracts.errors.ParseError`，并提供准确的行列号。
- 确保一次 `parse()` 只接受一条完整 SQL；末尾最多允许一个分号。

### 2.3 A 明确不做的事情

- 不 import `runner` 或 `storage`，也不调用 `Storage`。
- 不检查表是否存在、列是否存在、列类型是否匹配。
- 不处理 CREATE/DROP/INSERT/SELECT/UPDATE/DELETE 的执行。
- 不计算 WHERE 条件的真假，不筛选行，也不构造 `QueryResult`。
- 不决定数据文件、表文件或 row_id 的实现方式。

以上工作分别属于 C（语义检查、WHERE、执行编排）或 B（数据存储与持久化）。

## 3. 目录与文件计划

模块 A 只修改或新增 `compiler/` 内的文件；不得修改 `contracts/`、`runner/`、`storage/` 或已冻结的 golden 用例。

```text
hello-sql/
├── compiler/                         # A 的实现目录
│   ├── __init__.py                    # 唯一公开入口：parse
│   ├── lexer.py                       # 建议新增：token 与词法器
│   └── parser.py                      # 建议新增：递归下降语法分析器
├── contracts/                         # 共享且冻结：只读取
│   ├── ast.py                         # A 的输出类型
│   └── errors.py                      # ParseError 与 E_SYNTAX
├── runner/                            # C 负责：只读取接口说明
├── storage/                           # B 负责：只读取接口说明
├── tests/
│   └── golden_sql.py                  # 三方共同验收数据：只读取
└── docs/
    └── module-a-development-plan.md   # 本文档
```

建议的内部职责：

| 文件 | 应实现内容 | 对外可见性 |
|---|---|---|
| `compiler/__init__.py` | `parse(sql: str) -> Statement`，转调解析器 | 唯一公开 API |
| `compiler/lexer.py` | `Token`、token 类型、词法扫描、位置记录 | compiler 内部 |
| `compiler/parser.py` | token 游标、expect/peek 辅助方法、六类 statement 解析 | compiler 内部 |

如果实现规模较小，也可将 lexer 与 parser 放在同一实现文件；无论采用何种内部拆分，`compiler.parse` 必须保持为唯一稳定入口。

## 4. 对外接口与错误协议

### 4.1 唯一入口

```python
from contracts.ast import Statement

def parse(sql: str) -> Statement:
    """解析一条 SQL；失败时抛 ParseError。"""
```

C 将把这个函数依赖注入给 `Runner(storage, parse)`。因此 A 不应要求调用方传入 Storage、schema 或其他运行时上下文。

### 4.2 错误处理

所有 A 的可预期失败都必须是：

```python
raise ParseError(line, col, message)
```

这会产生错误码 `E_SYNTAX`。建议错误位置指向最先发现问题的字符或 token 起始位置。

典型语法错误包括：

- 未知关键字或非法字符；
- 未闭合的单引号字符串；
- 缺少必须的标识符、逗号、括号、`FROM`、`VALUES`、`SET` 等；
- `SELECT FROM users` 这类缺少选择列表的语句；
- 同一输入含两条语句，或分号后还有非空内容；
- 不受支持的 OR、括号表达式、列与列比较等 V1 外语法。

以下不是 A 的语法错误：表不存在、列不存在、INSERT 的值数不对、INSERT/UPDATE 的值类型不匹配。这些必须先正常形成 AST，由 C 或 B 按共享错误码处理。

## 5. 词法规则实现清单

### 5.1 保留字与大小写

关键字大小写不敏感；实现时可先将 token 的关键字比较统一为大写。以下词必须被识别为保留字，不能作为表名或列名：

```text
CREATE TABLE DROP INSERT INTO VALUES SELECT FROM WHERE UPDATE SET DELETE
AND OR INT TEXT REAL
```

普通标识符的形式为：

```text
[A-Za-z_][A-Za-z0-9_]*
```

进入 AST 时，所有表名和列名统一使用小写。例如 `Users`、`USERS` 与 `users` 都输出为 `"users"`。

### 5.2 字面量

| SQL 形式 | AST 中的值 |
|---|---|
| `18` | `int(18)` |
| `-18` | `int(-18)` |
| `18.5` | `float(18.5)` |
| `-0.5` | `float(-0.5)` |
| `'alice'` | `"alice"` |
| `'it''s'` | `"it's"` |

字符串只能使用单引号，两个连续单引号表示一个单引号；字符串不可跨行，未闭合必须报 `E_SYNTAX`。AST 中不得保留带引号的原始 SQL 文本，也不得把数字保留成字符串。

### 5.3 运算符与分隔符

需要识别：

```text
(  )  ,  ;  =  <>  <  <=  >  >=
```

应优先识别双字符运算符 `<>`、`<=`、`>=`，避免将它们错误地拆成两个 token。

## 6. 语法分析与 AST 映射

建议采用递归下降实现。每个解析函数消费它负责的完整结构，最外层 `parse()` 在语句完成后确认只剩 EOF，或一个分号加 EOF。

### 6.1 CREATE TABLE

```text
CREATE TABLE id '(' colDef (',' colDef)* ')'
colDef := id (INT | TEXT | REAL)
```

输出：

```python
CreateTableStmt(
    table="users",
    columns=(
        ColumnDef("id", SqlType.INT),
        ColumnDef("name", SqlType.TEXT),
    ),
)
```

至少应有一个列定义。列定义的顺序必须原样保留，因为它会成为 B 的物理列顺序和 `SELECT *` 的展开顺序。

### 6.2 DROP TABLE

```text
DROP TABLE id
```

输出 `DropTableStmt(table="...")`。

### 6.3 INSERT

```text
INSERT INTO id VALUES '(' value (',' value)* ')'
```

输出 `InsertStmt(table="...", values=(...))`。A 只负责字面量转换和语法结构；值的数量和类型匹配留给 C/B。

### 6.4 SELECT

```text
SELECT ('*' | id (',' id)*) FROM id [WHERE cond]
```

- `SELECT *` 使用 `SelectStmt.columns = None`。
- 显式列表使用按书写顺序的小写列名元组；允许重复列名。
- 有 WHERE 时解析为 Expr；没有 WHERE 时 `where = None`。

### 6.5 UPDATE

```text
UPDATE id SET assign (',' assign)* [WHERE cond]
assign := id '=' value
```

输出 `UpdateStmt`，保留表名、赋值项与可选 WHERE。赋值的列和值按 AST 类型填入 `Assignment`。

### 6.6 DELETE

```text
DELETE FROM id [WHERE cond]
```

输出 `DeleteStmt(table="...", where=...)`。

### 6.7 WHERE 表达式

```text
cond := cmp { AND cmp }
cmp  := id op value
op   := '=' | '<>' | '<' | '<=' | '>' | '>='
```

每个比较生成：

```python
Cmp(left=Column("age"), op=">=", right=Literal(18))
```

多个比较用 `And` 组合。建议固定为左结合：`a AND b AND c` 解析为 `And(And(a, b), c)`。V1 不支持 OR、括号、NOT、函数调用或列与列的比较；遇到它们应报告语法错误。

## 7. 开发顺序

1. **确认契约**：阅读 `contracts/ast.py`、`contracts/errors.py` 与本计划书第 9 节的待确认项；不修改共享契约。
2. **建立 token 模型**：实现 token 类型、原始词素、起始行列号与 EOF token。
3. **实现词法器**：先覆盖空白、标识符/关键字、数值、字符串、符号与未知字符错误。
4. **实现解析基础设施**：提供 `peek`、`advance`、`expect`、`parse_identifier`、`parse_value` 等小函数。
5. **依次实现语句**：建议 CREATE/DROP → INSERT → SELECT → WHERE → UPDATE → DELETE。
6. **实现完整输入检查**：处理可选分号，拒绝多语句与尾随垃圾字符。
7. **执行 A 的验收用例**：逐条验证 golden SQL 的可解析性、AST 类型和关键字段；确保 g19–g21 抛 `ParseError`。
8. **向 C 交接**：说明公开 import 方式、返回 AST 类型、已知待确认的契约差异，并提供若干 AST 样例。

## 8. 验收标准

### 8.1 正确输出

- `parse("CREATE TABLE users (id INT, name TEXT);")` 返回 `CreateTableStmt`，表名/列名为小写，类型分别为 `SqlType.INT`、`SqlType.TEXT`。
- `parse("INSERT INTO users VALUES (1, 'alice', -0.5)")` 返回原生 `int`、`str`、`float` 值。
- `parse("SELECT name, age FROM users WHERE age >= 18 AND name <> 'bob'")` 返回显式投影和由 `Cmp`/`And` 构成的 WHERE AST。
- `parse("SELECT * FROM users")` 的 `columns` 必须为 `None`，而非 `("*",)`。
- 不同大小写的关键字和标识符都能正确处理；AST 中标识符全部小写。

### 8.2 错误输出

- `SELEC * FROM users;` 抛 `ParseError`，错误码为 `E_SYNTAX`。
- `SELECT FROM users;` 抛 `ParseError`。
- `INSERT INTO users VALUES (1, 'abc);` 抛 `ParseError`。
- 非法字符、未闭合括号、缺少逗号、错误操作符、两个分号或第二条语句都抛 `ParseError`。

### 8.3 与集成测试的关系

`tests/golden_sql.py` 中的 21 条按顺序执行，属于最终端到端验收。对 A 而言：

- g01–g18 都应当成功解析成 AST，即使其最终可能在 C/B 阶段报业务错误；
- g19–g21 必须在 A 阶段报 `E_SYNTAX`；
- A 不需要也不能直接断言 g02、g05、g06、g10、g11、g17、g18 的最终业务错误码。

## 9. 集成前必须和组员确认的问题

### 9.1 存储目录命名不一致

仓库实际目录为 `storage/`，但 `README.md` 与 `docs/contract-v1.md` 多处写作 `storage_engine/`。B/C 在实现和 main.py 集成前必须统一为真实的包名；A 不应依赖任一存储目录。

### 9.2 重复列的责任存在文字冲突

`contracts/ast.py` 的 `CreateTableStmt.columns` 注释写着“列名不重复”，但：

- 契约第 3 节明确规定 `create_table` 对重复列抛 `E_DUP_COLUMN`；
- golden 的 g18 是 `CREATE TABLE users (id INT, id TEXT);`，期望最终得到 `E_DUP_COLUMN`；
- 图片中的分工也表示 CREATE 由 C 调 B 创建表。

因此，当前最符合集成测试的实现是：**A 将重复列定义视为语法合法并输出 AST，C 调 B 后由 B 报 `E_DUP_COLUMN`。** 该结论应先同步给全组；不要仅为消除注释冲突而私自修改 `contracts/ast.py`。

### 9.3 UPDATE 同列重复赋值的归属需要明确

契约约定“同列重复赋值 = 后者覆盖前者”，但没有明确这是 A 构造 AST 时去重，还是 C 执行时覆盖。建议保持 A 的职责最小化：按 SQL 书写顺序输出全部 `Assignment`，由 C 依据顺序实现“后者覆盖”。如果团队希望 A 去重，必须先走契约变更/确认流程，因为这改变了 C 看到的 AST 形态。

## 10. 交接清单

完成后，A 应向 C 提供：

- 稳定入口：`from compiler import parse`；
- 输入/输出说明：`str -> Statement`，失败抛 `ParseError`；
- 六类语句与 WHERE 的 AST 样例；
- 标识符已小写、字面量已转 Python 原生类型的保证；
- A 不做任何 schema/类型/WHERE 执行校验的说明；
- 第 9 节两个待团队确认的契约问题。

这样 C 可以在没有了解 A 内部 lexer/parser 设计的前提下，直接消费 AST 并完成语义检查与执行流程。
