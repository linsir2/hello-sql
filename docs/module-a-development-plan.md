# 模块 A（编译层）V2 个人开发计划

> 负责人：A
>
> 依据：`docs/v2-dev/v2-dev-plan.md`、`contracts/ast.py`（Contract 2.0）
>
> 当前状态：A 模块 V2 核心实现与模块测试已完成，等待 C 模块完成 AST 适配和项目级集成
>
> 目标：让编译层支持 V2 SQL，并向模块 C 输出统一、带源码位置的 AST

## 一、当前完成情况

| 项目 | 当前完成情况 | 验收状态 |
|---|---|---|
| V1 SQL 解析 | `parse()` 保持单语句接口，37 条 Golden SQL 均按预期解析 | 已完成 |
| A 模块测试 | BOOLEAN、优先级、别名、JOIN、脚本和位置等共 137 项通过 | 已完成 |
| AST 可视化 | 已展示 `Column`、`TableRef`、`JoinClause`、`Or`、`Not` 等 V2 节点 | 已完成 |
| BOOLEAN | 支持列类型以及 `TRUE`、`FALSE`，字面量转换为 Python `bool` | 已完成 |
| 表限定列 | 支持 `id` 与 `u.id`，输出统一的 `Column` | 已完成 |
| 表别名 | 支持 `users AS u` 和 `users u`，名称统一转为小写 | 已完成 |
| 逻辑表达式 | 支持比较、`NOT`、`AND`、`OR`、括号和规定优先级 | 已完成 |
| INNER JOIN | 支持 `JOIN`、`INNER JOIN` 和按源码顺序保存的连续 JOIN | 已完成 |
| SQL 脚本 | 已新增 `parse_script()`，直接消费完整 Token 流 | 已完成 |
| 源码范围 | 每条语句返回原始 SQL 和全局一基闭区间 `SourceSpan` | 已完成 |
| C 模块适配 | Runner 仍按 V1 字符串字段读取 `SelectStmt` | 集成待办（非 A 实现范围） |

### 当前验收记录

- A 模块命令：`.venv/bin/python -m pytest -q tests/A_tests`
- A 模块结果：`137 passed`。
- 项目级命令：`.venv/bin/python -m pytest -q`
- 项目级结果：`444 passed, 3 failed`。失败均发生在 C 尚未把
  `SelectStmt.table: TableRef` 和 `columns: tuple[Column, ...]` 接入逻辑计划，
  不属于 A 的词法或语法失败。
- 阶段结论：A 已达到“可交接”状态；整个项目只有在 B、C 完成各自 V2 工作并
  通过综合测试后，才能标记为“V2 完成”。

## 二、A 的职责边界

### 需要完成

- 维护词法分析器、语法分析器和编译层公开接口。
- 增加 V2 关键字、布尔字面量、点号和相关语法。
- 按优先级解析 `NOT`、比较、`AND`、`OR` 和括号。
- 解析表限定列、表别名和连续 `INNER JOIN`。
- 新增多语句解析接口，并保留准确的行列信息和原始 SQL。
- 所有标识符和别名统一转为小写。
- 保持 V1 的 `parse()` 和已有语法可用。
- 补充 A 模块单元测试，并向 C 提供 AST 示例和交接说明。

### 不由 A 完成

- 表、列是否真实存在。
- 重名列、未知限定符、重复别名等语义校验。
- 类型推断以及 WHERE、JOIN ON 是否为布尔表达式的检查。
- JOIN 执行、查询优化、存储格式和 TUI。
- BOOLEAN 在磁盘中的编码方式。

上述工作分别由 B、C 负责；A 只保证语法正确时生成符合 Contract 2.0 的 AST。

## 三、实施步骤

每一步完成后单独运行测试并提交，避免一次修改过多内容。

### 步骤一：确认 V2 契约细节

- 确认 `ParsedStatement.sql` 是否保留语句末尾分号和两侧空白。
- 确认 `SourceSpan` 是否覆盖分号，以及结束位置是否为闭区间。
- 确认 `;;`、开头分号和结尾多个分号的处理方式。
- 确认 `parse_script()` 遇到第一条错误后是否立即停止。
- 与 C 明确 `stop_on_error=False` 的结果格式；当前返回类型无法同时返回成功语句和多个错误。

完成标准：把最终约定写入 V2 契约文档，后续测试按该约定编写。

### 步骤二：建立 V2 测试骨架和回归基线

涉及目录：`tests/A_tests/`、`tests/golden/`（如现有结构需要）。

- 保留现有 V1 测试作为回归基线。
- 修正 AST 可视化测试对 V2 `SelectStmt.joins` 字段的预期。
- 分别建立 BOOLEAN、限定列、表达式优先级、别名、JOIN 和脚本解析测试。
- 先写最小失败用例，再按后续步骤逐项实现。

完成标准：清楚区分已有回归失败和等待实现的 V2 测试。

### 步骤三：扩展 Token 定义

主要文件：`compiler/tokens.py`

- 增加 `BOOLEAN`、`TRUE`、`FALSE`、`NOT`、`AS`、`INNER`、`JOIN`、`ON`。
- 确认 `OR` 纳入正式语法支持。
- 增加限定列需要的点号 Token：`.`。
- 为 Token 保留起始位置，并增加计算源码切片所需的结束位置或字符偏移量。
- 关键字匹配不区分大小写。

完成标准：每个新增关键字和符号均能产生正确 Token，且不破坏原有 Token。

### 步骤四：完善 Lexer 和全局源码坐标

主要文件：`compiler/lexer.py`

- 在整个输入脚本中持续维护行号、列号和字符偏移量，不能每条语句重新计数。
- 正确识别点号、分号、字符串、数字、运算符和新增关键字。
- 换行后的 Token 仍使用准确的 1-based 行列号。
- 保留可用于截取每条原始 SQL 的边界信息。

完成标准：多行、多语句输入中的 Token 坐标全部通过测试。

### 步骤五：实现 BOOLEAN 类型和字面量

主要文件：`compiler/parser.py`

- `CREATE TABLE` 类型支持 `BOOLEAN`。
- `INSERT` 值支持 `TRUE` 和 `FALSE`，输出 Python `bool`。
- 表达式中的布尔值输出 `Literal(True/False)`。
- 大小写形式均可解析，例如 `true`、`FALSE`。

完成标准：BOOLEAN 的建表、插入和表达式用例通过，AST 与 Contract 2.0 一致。

### 步骤六：实现标量表达式和表限定列

主要文件：`compiler/parser.py`

- 将列解析为 `Column(name, qualifier=None)`。
- 支持 `users.id`、`u.id`，输出 `Column(name="id", qualifier="u")`。
- 支持列与列比较，例如 `u.id = o.user_id`。
- 支持列与字面量、字面量与列、字面量与字面量比较。
- 单独的列或字面量在语法层允许成为表达式；是否满足布尔上下文由 C 判断。
- 标识符和限定符统一转为小写。

完成标准：各种比较组合均能生成正确的 `Column`、`Literal` 和 `Cmp`。

### 步骤七：重构表达式优先级

主要文件：`compiler/parser.py`

优先级从高到低为：括号、比较、`NOT`、`AND`、`OR`。

建议拆分解析函数：

```text
parse_expr -> parse_or -> parse_and -> parse_not -> parse_predicate -> parse_scalar
```

必须验证：

- `a = 1 OR b = 2 AND c = 3` 按 `a = 1 OR (b = 2 AND c = 3)` 解析。
- `NOT a = 1 AND b = 2` 按 `(NOT (a = 1)) AND b = 2` 解析。
- 括号能够覆盖默认优先级。
- 连续 `NOT NOT ...` 可以递归解析。

完成标准：AST 准确体现优先级，不依赖执行层修正。

### 步骤八：实现 TableRef 和表别名

主要文件：`compiler/parser.py`

- `FROM users` 输出 `TableRef(name="users")`。
- `FROM users AS u` 和 `FROM users u` 均输出别名 `u`。
- 表名和别名统一转为小写。
- SELECT 投影列改为 `tuple[Column, ...]`，不再是字符串列表。
- 保留 `SELECT *`；V2 不扩展 `u.*`、列别名或表达式投影。

完成标准：无别名、显式别名和隐式别名均生成正确 `TableRef`。

### 步骤九：实现 INNER JOIN

主要文件：`compiler/parser.py`

- 支持 `JOIN ... ON ...` 和 `INNER JOIN ... ON ...`。
- `ON` 后复用完整表达式解析器。
- 支持一条 SELECT 连续多个 JOIN，按源码顺序存入 `SelectStmt.joins`。
- 每个 JOIN 生成 `JoinClause(right=TableRef(...), on=..., kind=JoinType.INNER)`。
- 缺失表名、`ON` 或表达式时抛出 `ParseError(E_SYNTAX)`。

完成标准：单 JOIN、多 JOIN、带别名 JOIN 和复杂 ON 表达式全部通过。

### 步骤十：实现 `parse_script()` 和 SourceSpan

主要文件：`compiler/parser.py` 和编译层公开入口。

- 新增 `parse_script(source)`，返回契约规定的语句集合。
- 直接消费同一个 Token 流，不使用 `source.split(';')`。
- 正确处理字符串内部的分号。
- 每条结果包含 AST、原始 SQL 和 1-based `SourceSpan`。
- 空输入返回空结果。
- 支持最后一条语句没有分号。
- 错误信息定位到整个脚本中的真实行列。

完成标准：多行脚本、字符串分号、末尾无分号和空输入均通过测试。

### 步骤十一：保持单语句接口兼容

- 保留原有 `parse(sql)` 调用方式。
- `parse()` 可复用 `parse_script()`，但必须只接受一条有效语句。
- 多条语句传给 `parse()` 时返回明确语法错误。
- 检查 AST 字段升级为 `Column`、`TableRef` 后对现有调用方的影响。
- 与 C 协调合并顺序，避免 AST 输出变化导致主分支长时间不可用。

完成标准：V1 语法行为稳定，公开 API 有明确测试。

### 步骤十二：测试、文档和交接

- 运行全部 A 模块测试和项目级回归测试。
- 更新 AST 可视化器对 `Column`、`Or`、`Not`、`TableRef` 和 `JoinClause` 的展示。
- 实现完成后更新 `docs/module-a-source-guide.md`，使其描述真实代码。
- 向 C 提供典型 SQL、预期 AST、错误行为和源码范围示例。
- 将未解决的契约问题记录到 V2 计划书，不在代码中自行猜测。

完成标准：测试通过、文档与实现一致，C 可直接基于 A 的 AST 开始语义分析。

当前状态：A 模块测试、AST 可视化、源码说明和 C 交接示例已完成。项目级
回归仍等待 C 适配 V2 AST；该外部集成项保留为未完成，不影响 A 模块交接。

## 四、必须覆盖的语法

```ebnf
script      := { stmt ';' } [stmt [';']]
selectStmt  := SELECT selectList FROM tableRef { joinClause } [WHERE expr]
type        := INT | TEXT | REAL | BOOLEAN
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

V2 明确不支持：`LEFT/RIGHT/FULL/CROSS JOIN`、子查询、聚合、排序、分组、列别名、表达式投影和 `table.*`。

## 五、核心验收样例

```sql
CREATE TABLE users (
  id INT,
  active BOOLEAN
);

SELECT u.id, o.user_id
FROM users AS u
INNER JOIN orders o ON u.id = o.user_id AND NOT o.deleted
WHERE u.active = TRUE OR o.total > 100;
```

验收时确认：

- 名称和别名均已规范化为小写。
- `u.id` 和 `o.user_id` 的 qualifier 正确。
- JOIN 位于 `SelectStmt.joins`，顺序与源码一致。
- `NOT`、`AND`、`OR` 的 AST 层级符合优先级。
- 两条语句都有准确的原始 SQL 和 `SourceSpan`。

## 六、建议提交顺序

1. `test(a): 建立 V2 编译层测试骨架`
2. `feat(a): 扩展 token 与 lexer 源码位置`
3. `feat(a): 支持 boolean 和限定列`
4. `refactor(a): 实现 V2 表达式优先级`
5. `feat(a): 支持表别名和 inner join`
6. `feat(a): 增加 parse_script 与 source span`
7. `docs(a): 更新编译层说明与 C 交接示例`

## 七、最终完成标准

- [x] V2 契约细节已写入团队 V2 计划书，并由测试固定边界行为。
- [x] 新增关键字、点号、Token 偏移和全局源码坐标工作正常。
- [x] BOOLEAN 类型及布尔字面量解析正确。
- [x] 表限定列、列间比较和标量表达式解析正确。
- [x] `NOT`、`AND`、`OR` 及括号优先级正确。
- [x] 表别名和连续 INNER JOIN 解析正确。
- [x] `parse_script()` 不依赖字符串分割，并生成准确的 SQL 与 SourceSpan。
- [x] `parse()` 和全部 V1 Golden SQL 保持语法层兼容。
- [x] 所有数据库名、表名、列名、限定符及别名均已规范化为小写。
- [x] A 的词法和语法错误统一使用 `ParseError(E_SYNTAX)`，位置准确。
- [x] A 模块 137 项测试全部通过。
- [x] 已完成 AST 示例、源码说明和对 C 的交接材料。
- [ ] 项目级回归全部通过：当前由 C 模块尚未适配 V2 `TableRef/Column` 阻塞。
