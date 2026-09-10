# 模块 A（编译层）V2 源码说明与交接指南

## 1. 文档目的与阶段状态

本文说明模块 A 当前真实的 V2 实现、公开接口、AST 输出、错误行为和测试方法，
用于代码检查、答辩讲解以及向模块 C 交接。

当前阶段结论：

- A 的 V2 词法分析、语法分析、脚本解析、源码位置和 AST 可视化已经完成；
- A 模块 137 项测试全部通过；
- 项目级回归仍等待 C 将逻辑计划从 V1 字符串字段适配到 V2 的
  `TableRef`、`Column` 和 `JoinClause`；
- A 不负责名称绑定、类型检查、JOIN 执行、逻辑优化、TUI 或磁盘编码。

## 2. 编译流程与职责边界

```text
完整 SQL 文本
  -> Lexer：字符流转 Token 流
  -> Token：类型、原始词素、全局行列、源码起止偏移
  -> Parser：递归下降语法分析
  -> Statement 或 tuple[ParsedStatement, ...]
  -> C：名称绑定、类型检查、逻辑计划和执行
```

A 只判断 SQL 是否符合 V2 文法并构建 AST。以下问题必须留给 C 或 B：

- 数据库、表、列或限定符是否存在；
- 未限定列是否歧义、表别名是否重复；
- WHERE 和 JOIN ON 是否最终为 BOOLEAN；
- BOOLEAN 是否能写入目标列；
- JOIN、优化器、SQL 文件和 TUI 的运行行为；
- BOOLEAN 与 Catalog 的磁盘格式。

## 3. 目录与公开入口

```text
compiler/
├── __init__.py    # 公开 parse 与 parse_script
├── tokens.py      # TokenType、Token、SourcePosition、KEYWORDS
├── lexer.py       # SQL 文本 -> Token 列表
└── parser.py      # Token 列表 -> AST / Script

contracts/
└── ast.py         # A 产生、C 消费的 V2 AST 契约
```

模块 C 只应从 `compiler` 包调用公开入口：

```python
from compiler import parse, parse_script

statement = parse("SELECT * FROM users;")
script = parse_script("CREATE DATABASE shop; USE shop;")
```

不要从 C 直接调用 `Lexer`、`Parser` 或依赖 Token 内部细节。

## 4. Token 与 Lexer

### 4.1 Token 数据

每个 Token 保存：

| 字段 | 含义 |
|---|---|
| `type` | `TokenType` 种别 |
| `lexeme` | SQL 中的原始文本 |
| `position` | Token 首字符的一基 `line/column` |
| `start_offset` | Token 首字符的零基源码偏移 |
| `end_offset` | Token 末字符之后的零基源码偏移 |

偏移采用左闭右开区间，因此满足：

```python
source[token.start_offset:token.end_offset] == token.lexeme
```

EOF 的两个偏移都等于完整源码长度。行列与偏移始终针对完整脚本，不会在分号
或新语句处重新计数；CRLF 按一个换行处理。

### 4.2 V2 词法扩展

V2 新增或正式启用：

```text
BOOLEAN TRUE FALSE NOT OR AS INNER JOIN ON .
```

关键字不区分大小写。标识符遵守
`[A-Za-z_][A-Za-z0-9_]*`，进入 AST 前统一转换为小写。

点号既可能出现在限定列中，也可能属于实数字面量。Lexer 只在点号后紧跟数字
时把它作为数字的一部分，所以：

```text
u.id  -> IDENTIFIER DOT IDENTIFIER
18.5  -> REAL_LITERAL
```

### 4.3 字面量转换

| SQL | Python 值 |
|---|---|
| `18` | `int(18)` |
| `-0.5` | `float(-0.5)` |
| `'it''s'` | `"it's"` |
| `TRUE` | `True` |
| `FALSE` | `False` |

字符串使用单引号，两个连续单引号表示一个单引号；未闭合或跨行字符串报告
`ParseError(E_SYNTAX)`。

## 5. Parser 与 V2 文法

Parser 在同一个 Token 流上采用递归下降方式构建 `contracts.ast` 中的不可变
数据类。当前支持九类语句：

```text
CREATE DATABASE    DROP DATABASE    USE
CREATE TABLE       DROP TABLE       INSERT
SELECT             UPDATE           DELETE
```

V2 关键文法为：

```ebnf
script      := { stmt ';' } [stmt [';']]
selectStmt  := SELECT selectList FROM tableRef { joinClause } [WHERE expr]
type        := INT | TEXT | REAL | BOOLEAN
selectList  := '*' | columnRef (',' columnRef)*
tableRef    := identifier [AS identifier | identifier]
joinClause  := [INNER] JOIN tableRef ON expr
expr        := orExpr
orExpr      := andExpr { OR andExpr }
andExpr     := notExpr { AND notExpr }
notExpr     := NOT notExpr | predicate
predicate   := '(' expr ')' | scalar [comparisonOp scalar]
scalar      := columnRef | value
columnRef   := [identifier '.'] identifier
value       := NUMBER | STRING | TRUE | FALSE
```

## 6. AST 映射规则

### 6.1 BOOLEAN

```sql
CREATE TABLE flags (enabled BOOLEAN);
INSERT INTO flags VALUES (TRUE);
```

分别生成 `SqlType.BOOLEAN` 与 Python `True`。表达式中的布尔值包装为
`Literal(True)` 或 `Literal(False)`。

### 6.2 列与表引用

```sql
SELECT id, u.name FROM Users AS U;
```

关键字段为：

```python
SelectStmt(
    columns=(
        Column(name="id", qualifier=None),
        Column(name="name", qualifier="u"),
    ),
    table=TableRef(name="users", alias="u"),
    where=None,
    joins=(),
)
```

`SELECT *` 仍使用 `columns=None`，不会生成虚假的星号列节点。V2 不支持
`u.*`、列别名或表达式投影。

### 6.3 通用比较操作数

比较两侧均使用 `ScalarExpr`，因此语法层允许：

```sql
u.id = o.user_id
age >= 18
18 <= age
TRUE <> FALSE
```

A 只构建 `Cmp(left, op, right)`；类型是否允许由 C 判断。

### 6.4 表达式优先级

优先级从高到低固定为：

```text
括号 > 比较 > NOT > AND > OR
```

例如：

```sql
a = 1 OR b = 2 AND c = 3
```

生成：

```python
Or(
    left=Cmp(Column("a"), "=", Literal(1)),
    right=And(
        left=Cmp(Column("b"), "=", Literal(2)),
        right=Cmp(Column("c"), "=", Literal(3)),
    ),
)
```

括号不保留为单独 AST 节点，只通过树形结构改变结合顺序。连续 NOT 使用嵌套
`Not` 节点表示。

### 6.5 表别名与 INNER JOIN

以下两种表别名写法等价：

```sql
FROM users u
FROM users AS u
```

以下两种 JOIN 写法也等价，类型均为 `JoinType.INNER`：

```sql
JOIN orders o ON u.id = o.user_id
INNER JOIN orders o ON u.id = o.user_id
```

多个 JOIN 按源码顺序保存到 `SelectStmt.joins`，A 不检查重复别名、未知限定符
或 ON 的 BOOLEAN 类型。

## 7. `parse()` 与 `parse_script()`

### 7.1 单语句接口

```python
def parse(sql: str) -> Statement: ...
```

`parse()` 只接受一条语句；末尾分号可省略，但连续分号、第二条语句和尾随 Token
均报告 `E_SYNTAX`。V1.1 的 37 条 Golden SQL 已保留为语法回归基线。

### 7.2 多语句接口

```python
def parse_script(sql: str) -> tuple[ParsedStatement, ...]: ...
```

`parse_script()` 只进行一次词法扫描并直接续消费完整 Token 流，不使用
`source.split(";")`。因此字符串中的分号不会切分语句：

```sql
INSERT INTO logs VALUES ('a;b'); USE shop;
```

脚本规则：

- 空白输入返回空元组；
- 语句之间必须有分号；
- 最后一条语句的分号可以省略；
- 开头分号和连续分号属于非法空语句；
- 遇到首个词法或语法错误立即抛出 `ParseError`。

### 7.3 原文与 SourceSpan

每条脚本解析结果为：

```python
ParsedStatement(
    statement=...,
    sql=...,
    span=SourceSpan(start_line, start_col, end_line, end_col),
)
```

约定如下：

- `sql` 保留语句内部原始大小写和空白；
- 不包含语句前后的分隔空白；
- 原输入包含结束分号时，`sql` 和 `span` 都包含分号；
- `SourceSpan` 是相对于完整脚本的一基闭区间；
- 后续语句和错误不会把行列位置重置为第一行。

## 8. 错误行为与非目标

A 所有用户可触发的词法和语法失败统一抛出：

```python
ParseError(line, col, message)
```

其错误码固定为 `E_SYNTAX`，行列指向完整输入中最早发现问题的 Token 或字符。

以下 V2 非目标应由 A 作为语法错误拒绝：

```text
LEFT/RIGHT/FULL/CROSS/NATURAL JOIN
ORDER BY、LIMIT、OFFSET、GROUP BY、聚合、子查询
NULL、IS NULL、列别名、表达式投影、table.*
```

以下不是 A 的错误：表不存在、列不存在、限定符不存在、列歧义、别名重复、
值类型不匹配、WHERE/ON 不是 BOOLEAN。这些 SQL 应先形成 AST，再由 C/B 使用
各自的 V2 错误码处理。

## 9. 向 C 的交接示例

### 9.1 典型 SQL

```sql
SELECT u.id, o.user_id
FROM Users AS U
INNER JOIN Orders O ON u.id = o.user_id AND NOT o.deleted
WHERE u.active = TRUE OR o.total > 100;
```

### 9.2 关键 AST 形状

```python
SelectStmt(
    columns=(Column("id", "u"), Column("user_id", "o")),
    table=TableRef("users", "u"),
    where=Or(
        Cmp(Column("active", "u"), "=", Literal(True)),
        Cmp(Column("total", "o"), ">", Literal(100)),
    ),
    joins=(
        JoinClause(
            right=TableRef("orders", "o"),
            on=And(
                Cmp(Column("id", "u"), "=", Column("user_id", "o")),
                Not(Column("deleted", "o")),
            ),
            kind=JoinType.INNER,
        ),
    ),
)
```

C 适配时必须注意：

1. `SelectStmt.table` 已从字符串变为 `TableRef`，Storage 仍应接收
   `statement.table.name`，不能直接接收整个 `TableRef`；
2. 显式投影项已从字符串变为 `Column`，绑定时必须同时使用 `name` 和
   `qualifier`；
3. 应按 `statement.joins` 的顺序构造左深 JOIN 计划；
4. ON 在加入当前右表后绑定，不能引用后续表；
5. WHERE、ON、AND、OR、NOT 的 BOOLEAN 检查属于 C；
6. `parse_script()` 只负责解析，`execute_script()` 和 `execute_file()` 由 C 提供。

### 9.3 错误和位置示例

```python
source = "USE shop;\nSELECT u. FROM users u;"
```

A 会在完整脚本第 2 行第 11 列抛出 `ParseError(E_SYNTAX)`。C 不应重新切分 SQL
后再次调用 `parse()`，否则会丢失全局位置；应直接消费 `parse_script()` 返回的
`ParsedStatement`。

## 10. 测试与 AST 可视化

运行 A 模块测试：

```bash
.venv/bin/python -m pytest -q tests/A_tests
```

当前验收结果：

```text
137 passed
```

测试覆盖：

- V1.1 的 37 条 Golden SQL；
- V2 关键字、点号、混合换行和源码偏移；
- BOOLEAN、限定列、四种比较操作数组合；
- NOT/AND/OR、括号和优先级；
- TableRef、表别名、单个及链式 INNER JOIN；
- 空脚本、多语句、字符串分号、末尾无分号；
- 原始 SQL、SourceSpan 和后续语句全局错误位置；
- V2 SelectStmt 的 AST 可视化结构。

启动 AST 可视化演示：

```bash
python3 -m tests.A_tests.ast_visualizer
```

默认示例会展示 `SelectStmt`、`Column`、`TableRef`、`JoinClause`、`Cmp`、
`And`、`Or`、`Not` 和 BOOLEAN 字面量。

## 11. 当前交接结论

A 模块已经满足 V2 编译层交付标准，可以冻结公开 AST 输出并交给 C 适配。
当前项目级失败是 C 仍把 `TableRef` 和 `Column` 当作 V1 字符串处理；在 C 完成
适配、B 完成 BOOLEAN 与页式 Catalog、综合测试全部通过之前，只能称为
“A 阶段完成”，不能称为“整个项目 V2 完成”。
