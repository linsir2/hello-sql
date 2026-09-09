# 模块 A 源码说明书

## 1. 文档目的与范围

本文档说明模块 A（编译层）当前已经完成的源码结构、关键函数与对接方式，便于代码检查、答辩讲解和后续与模块 C 集成。

本文只覆盖 `compiler/` 下的四个实现文件；`tests/` 中的自动化测试与 AST 可视化演示程序不在本文范围内。

模块 A 的职责边界如下：

```text
SQL 文本
  -> 词法分析（Lexer）
  -> Token 序列（带行列位置）
  -> 语法分析（Parser）
  -> Statement AST
  -> 交给模块 C 做语义检查与执行
```

模块 A 不访问数据库、不检查表或列是否存在、不计算 WHERE 的真假、不调用 Storage，也不构造 QueryResult。

## 2. 编译目录总览

```text
compiler/
├── __init__.py    # 唯一公开入口：parse(sql) -> Statement
├── tokens.py      # Token 类型、源码位置、关键字映射
├── lexer.py       # SQL 文本 -> Token 列表
└── parser.py      # Token 列表 -> contracts.ast.Statement
```

| 文件 | 核心职责 | 对外可见性 |
|---|---|---|
| `tokens.py` | 定义 lexer 和 parser 共用的数据格式 | compiler 内部 |
| `lexer.py` | 识别关键字、标识符、字面量、运算符和位置 | compiler 内部 |
| `parser.py` | 按 V1.1 文法递归下降解析，并构建 AST | compiler 内部 |
| `__init__.py` | 串联 lexer 和 parser，导出 `parse` | 模块 A 的唯一 API |

模块 C 的正确调用方式是：

```python
from compiler import parse

statement = parse(sql)
```

模块 C 不应直接 import `Lexer`、`Parser` 或 `Token`，避免依赖 A 的内部实现细节。

## 3. Token 数据定义：`compiler/tokens.py`

该文件不扫描 SQL，它只定义“词法分析结果长什么样”。一个 Token 在逻辑上对应四元式：

```text
[种别码, 词素, 行号, 列号]
```

例如 SQL 中的 `SELECT` 可表示为：

```text
[KW_SELECT, "SELECT", 1, 1]
```

| 行号 | 类 / 常量 | 作用 |
|---|---|---|
| 23 | `TokenType` | 定义全部 Token 类型：关键字、标识符、整数、小数、字符串、星号、分隔符、比较运算符和 EOF。 |
| 93 | `SourcePosition` | 保存 Token 起始位置 `line`、`column`，均从 1 开始计数。 |
| 109 | `Token` | 保存 `type`、原始文本 `lexeme`、起始位置 `position`。 |
| 132 | `KEYWORDS` | 关键字映射表，例如 `SELECT -> KW_SELECT`，用于实现关键字大小写不敏感。 |

重要规则：

- 保留字不能作为库名、表名或列名；
- 标识符最终进入 AST 时统一转为小写；
- `STAR` 专门对应 `SELECT *`，不是算术乘法；
- `EOF` 不来自用户输入，由 lexer 在全部文本结束后追加。

## 4. 词法分析：`compiler/lexer.py`

Lexer 的输入是一条 SQL 字符串，输出是以 `EOF` 结尾的 `list[Token]`。如果遇到非法字符、字符串未闭合或字符串跨行，会抛出带行列号的 `ParseError(E_SYNTAX)`。

| 行号 | 函数 / 常量 | 功能 |
|---|---|---|
| 24 | `_SINGLE_CHAR_TOKENS` | 定义 `(`、`)`、`,`、`*`、`;`、`=` 等单字符符号的 Token 映射。 |
| 36 | `Lexer` | 保存 SQL 文本、扫描下标、当前行号和列号的有状态词法分析器。 |
| 49 | `Lexer.__init__` | 初始化扫描状态，游标从输入第一个字符开始。 |
| 62 | `Lexer.tokenize` | 核心扫描循环：按字符类别创建 Token，最后添加 EOF。 |
| 116 | `_current_char` | 查看当前字符，不移动游标。 |
| 127 | `_peek_char` | 查看下一个字符，用于识别负数、`<=`、`>=`、`<>`。 |
| 139 | `_advance` | 消费当前字符，同步更新下标、行号、列号；支持 `\n`、`\r\n`、`\r`。 |
| 171 | `_skip_whitespace` | 跳过 SQL Token 之间的空格、制表符和换行。 |
| 187 | `_scan_identifier_or_keyword` | 识别普通标识符或保留字。 |
| 206 | `_scan_number` | 识别整数、小数及前导负号，例如 `18`、`-18`、`18.5`。 |
| 235 | `_scan_string` | 识别单引号字符串和 `''` 转义；拒绝跨行和未闭合字符串。 |
| 277 | `_scan_comparison_operator` | 识别 `<>`、`<=`、`>=`、`<`、`>`。 |
| 301 | `_scan_single_char_token` | 识别括号、逗号、星号、分号、等号等单字符 Token。 |
| 313 | `_is_identifier_start` | 判断字符是否符合标识符首字符规则 `[A-Za-z_]`。 |
| 329 | `_is_identifier_part` | 判断字符是否符合标识符后续规则 `[A-Za-z0-9_]`。 |
| 339 | `_is_ascii_digit` | 严格判断 ASCII 数字 `0-9`，不接受其他 Unicode 数字。 |
| 350 | `tokenize(sql)` | lexer 模块的简化入口：创建 Lexer 后调用其 `tokenize` 方法。 |

Lexer 只识别文本边界，不判断 SQL 结构。例如它会把 `SELECT FROM users` 正常切成 Token；“SELECT 后缺少列名”由 Parser 报语法错误。

## 5. 语法分析与 AST 构建：`compiler/parser.py`

Parser 读取 lexer 生成的 Token 序列，使用递归下降方式按 V1.1 文法构建 `contracts.ast` 中定义的不可变数据类。它不会检查表、列或数据库在运行期是否存在。

### 5.1 映射常量与 Parser 基础方法

| 行号 | 函数 / 常量 | 功能 |
|---|---|---|
| 44 | `_SQL_TYPE_TOKENS` | 将 `KW_INT/KW_TEXT/KW_REAL` 转换为 `SqlType.INT/TEXT/REAL`。 |
| 53 | `_COMPARISON_OPERATORS` | 将比较 Token 转为 AST 中的 `= <> < <= > >=` 字符串。 |
| 63 | `Parser` | 维护 Token 游标、提供公共解析方法、分派完整 SQL 语句。 |
| 77 | `Parser.__init__` | 接收 Token 序列并确认最后一个 Token 是 EOF。 |
| 95 | `peek` | 查看当前或后续 Token，不消费 Token。 |
| 117 | `advance` | 消费当前 Token；遇到 EOF 时不越界。 |
| 130 | `expect` | 断言当前 Token 必须属于指定类型；失败时抛带位置的 ParseError。 |
| 171 | `parse_identifier` | 只接受普通标识符，并将其转换为小写。 |
| 188 | `parse_value` | 将整数、小数、字符串 Token 转为 Python 的 `int`、`float`、`str`。 |
| 227 | `parse` | 解析完整输入：调用语句解析、允许最多一个分号、最后必须到达 EOF。 |
| 249 | `parse_statement` | 根据首关键字分派到九类 SQL 语句的具体解析函数。 |

### 5.2 数据库与表定义语句

| 行号 | 函数 | 支持语法 | 生成的 AST |
|---|---|---|---|
| 286 | `_parse_create_statement` | `CREATE DATABASE` 或 `CREATE TABLE` | 按第二个关键字继续分派 |
| 313 | `_parse_drop_statement` | `DROP DATABASE` 或 `DROP TABLE` | 按第二个关键字继续分派 |
| 339 | `_parse_create_database_statement` | `CREATE DATABASE id` | `CreateDatabaseStmt` |
| 350 | `_parse_drop_database_statement` | `DROP DATABASE id` | `DropDatabaseStmt` |
| 361 | `_parse_use_database_statement` | `USE id` | `UseDatabaseStmt` |
| 372 | `_parse_create_table_statement` | `CREATE TABLE id (...)` | `CreateTableStmt` |
| 385 | `_parse_drop_table_statement` | `DROP TABLE id` | `DropTableStmt` |
| 648 | `_parse_column_definitions` | `(colDef, colDef, ...)` | 有序的 `tuple[ColumnDef, ...]` |
| 669 | `_parse_column_definition` | `id INT/TEXT/REAL` | 单个 `ColumnDef` |
| 681 | `_parse_sql_type` | `INT` / `TEXT` / `REAL` | `SqlType` 枚举成员 |

说明：重复列名在语法上合法。A 会保留它们并输出 AST，最终由 B 的 `create_table` 报 `E_DUP_COLUMN`。

### 5.3 INSERT 与 SELECT

| 行号 | 函数 | 支持语法 | 生成的 AST |
|---|---|---|---|
| 395 | `_parse_insert_statement` | `INSERT INTO id VALUES (...)` | `InsertStmt` |
| 418 | `_parse_insert_values` | `value (, value)*` | 按书写顺序的原生值元组 |
| 439 | `_parse_select_statement` | `SELECT selectList FROM id [WHERE cond]` | `SelectStmt` |
| 462 | `_parse_select_list` | `*` 或 `id (, id)*` | `None` 或小写列名元组 |

特别规则：`SELECT *` 在 AST 中必须表示为：

```python
SelectStmt(columns=None, table="users", where=None)
```

不能表示为 `columns=("*",)`。

### 5.4 WHERE、UPDATE 与 DELETE

| 行号 | 函数 | 功能 |
|---|---|---|
| 487 | `_parse_optional_where` | 当前 Token 是 WHERE 时解析条件，否则返回 `None`。 |
| 503 | `_parse_where_expression` | 解析多个 AND 条件，构建左结合的 `And` 树。 |
| 525 | `_parse_comparison` | 解析 `列 op 字面量`，构建 `Cmp(Column, op, Literal)`。 |
| 544 | `_parse_comparison_operator` | 验证并读取六种合法比较运算符。 |
| 569 | `_parse_update_statement` | 解析 `UPDATE id SET ... [WHERE ...]`，构建 `UpdateStmt`。 |
| 591 | `_parse_assignments` | 解析一个或多个逗号分隔的赋值项。 |
| 610 | `_parse_assignment` | 解析 `列 = 字面量`，构建 `Assignment`。 |
| 628 | `_parse_delete_statement` | 解析 `DELETE FROM id [WHERE ...]`，构建 `DeleteStmt`。 |

WHERE 示例：

```sql
WHERE age >= 18 AND name <> 'bob'
```

对应 AST：

```python
And(
    left=Cmp(Column("age"), ">=", Literal(18)),
    right=Cmp(Column("name"), "<>", Literal("bob")),
)
```

`a AND b AND c` 采用左结合结构：`And(And(a, b), c)`。

### 5.5 错误信息辅助方法

| 行号 | 函数 | 功能 |
|---|---|---|
| 706 | `_decode_string_literal` | 去掉字符串外层引号，并将 `''` 还原为单引号。 |
| 733 | `_format_expected_types` | 格式化错误消息中的“期望 Token 类型”。 |
| 744 | `_format_actual_token` | 格式化错误消息中的“实际 Token”。 |
| 756 | `_format_token_type` | 将内部 TokenType 转为可读的 SQL 风格名称。 |

## 6. 唯一公开入口：`compiler/__init__.py`

`__init__.py` 是 C、main.py 和测试代码唯一应该直接调用的编译层文件。

| 行号 | 内容 | 功能 |
|---|---|---|
| 25 | `parse(sql: str) -> Statement` | 调用 `lexer.tokenize` 得到 Token 列表，再调用 `Parser(tokens).parse()` 返回 AST。 |
| 54 | `__all__ = ["parse"]` | 明确只向外公开 parse，保护 Lexer/Parser 的内部实现。 |

`parse` 的边界如下：

```python
statement = parse(sql)   # 成功：返回 Statement AST
```

```python
parse("SELEC * FROM users;")  # 失败：抛 ParseError，code 为 E_SYNTAX
```

## 7. A 与 C 的交接说明

A 向 C 交付的是 `contracts.ast.Statement`，共九种可能的根节点：

```text
CreateDatabaseStmt    DropDatabaseStmt    UseDatabaseStmt
CreateTableStmt       DropTableStmt
InsertStmt            SelectStmt          UpdateStmt          DeleteStmt
```

C 的典型分派方式：

```python
from compiler import parse
from contracts.ast import SelectStmt

statement = parse(sql)
if isinstance(statement, SelectStmt):
    # C 在这里做表/列校验、WHERE 求值、scan、投影和 QueryResult 组装。
    pass
```

注意：C 必须从同一份 `contracts.ast` import 这些类型，不能在 runner 中重新定义同名的 `SelectStmt` 等类，否则 Python 会把它们视为不同类型，`isinstance` 对接会失败。

## 8. 答辩简要说明

可以用下面的顺序介绍模块 A：

1. `tokens.py` 定义 Token 四元式和位置结构；
2. `lexer.py` 将 SQL 字符流识别为 Token 流，并在词法错误时定位行列；
3. `parser.py` 使用递归下降方法消费 Token，检查文法并直接构建 AST；
4. `compiler.parse` 将两步封装为唯一入口，交付 Statement 给模块 C；
5. 模块 C 负责语义检查和执行，模块 B 只负责存储，A 不越过模块边界。

这样既能说明完整的 SQL 编译流程，也能说明三人协作时各模块的职责隔离。
