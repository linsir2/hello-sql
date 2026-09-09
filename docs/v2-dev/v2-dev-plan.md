# hello-sql V2 开发计划

## 1. 目标

V2 在 V1.1 已完成的单表 SQL、逻辑计划、执行器和页式存储基础上，完成以下六项工作：

1. TUI、多行 SQL、多语句执行和 SQL 文件执行；
2. 将 Catalog 改为通过页式存储系统持久化的内部系统表；
3. 支持 AND、OR、NOT、括号与优先级、列与列比较、表限定列和表别名；
4. 增加 BOOLEAN 数据类型；
5. 支持 INNER JOIN；
6. 增加规则型逻辑优化器。

V2 仍采用三层边界：

```text
SQL 文本或 SQL 文件
  -> A：词法分析、语法分析、parse_script、AST
  -> C：名称绑定、类型检查、初始逻辑计划、逻辑优化
  -> C：执行器树、结果汇总、TUI
  -> B：页缓存、页式 Catalog、表数据持久化
```

## 2. 非目标

V2 不实现以下功能：

- ORDER BY、LIMIT、OFFSET；
- GROUP BY、聚合函数、HAVING；
- LEFT、RIGHT、FULL、CROSS、NATURAL JOIN；
- NULL、三值逻辑、IS NULL；
- 主键、唯一约束、索引；
- 子查询、CTE、视图；
- 事务、WAL、并发控制；
- 代价优化、统计信息、JOIN 重排；
- 独立物理计划层和多种 JOIN 物理算法。

INNER JOIN 在 V2 中统一使用 Nested Loop Join 执行。物理计划、Hash Join 和索引选择留到后续版本。

## 3. V2 共享契约

### 3.1 版本与兼容策略

- `contracts.__version__` 升级为 `2.0`。
- V1.1 的 37 条 Golden SQL 必须继续通过，作为 V2 的回归基线。
- `parse(sql) -> Statement` 和 `Runner.execute(sql) -> QueryResult` 保留。
- 新增 `parse_script`、`execute_script` 和 `execute_file`，单语句调用方不需要修改。
- V2 AST 中显式投影列使用 `Column`，FROM 表使用 `TableRef`。V1 实现迁移完成前可能仍构造字符串字段，该状态只用于短期分支兼容，不属于最终 V2 合规输出。

### 3.2 V2 SQL 子集文法

```text
script       := { stmt ';' } [stmt [';']]

stmt         := CREATE DATABASE id
              | DROP DATABASE id
              | USE id
              | CREATE TABLE id '(' colDef (',' colDef)* ')'
              | DROP TABLE id
              | INSERT INTO id VALUES '(' value (',' value)* ')'
              | SELECT selectList FROM tableRef { joinClause } [WHERE expr]
              | UPDATE id SET assign (',' assign)* [WHERE expr]
              | DELETE FROM id [WHERE expr]

colDef       := id type
type         := INT | TEXT | REAL | BOOLEAN
selectList   := '*' | columnRef (',' columnRef)*
tableRef     := id [AS id | id]
joinClause   := [INNER] JOIN tableRef ON expr
assign       := id '=' value

expr         := orExpr
orExpr       := andExpr { OR andExpr }
andExpr      := notExpr { AND notExpr }
notExpr      := NOT notExpr | predicate
predicate    := '(' expr ')'
              | scalar [comparisonOp scalar]
scalar       := columnRef | value
columnRef    := [id '.'] id
comparisonOp := '=' | '<>' | '<' | '<=' | '>' | '>='
value        := NUMBER | STRING | TRUE | FALSE
```

约束：

- 多条语句之间必须使用分号分隔，最后一条语句的分号可省略。
- 字符串中的分号属于字符串内容，不能切分语句。
- 括号只改变 AST 结构和运算优先级，不保留独立括号节点。
- 运算优先级从高到低为：括号、比较、NOT、AND、OR。
- 比较两侧可以是列或字面量，因此允许列与列、列与字面量、字面量与字面量比较。
- WHERE 和 JOIN ON 的最终类型必须为 BOOLEAN。
- BOOLEAN 只允许 `=` 和 `<>` 比较，不允许大小比较。
- INT 与 REAL 比较沿用 INT 向 REAL 提升的规则；其他不同类型之间不做隐式转换。
- 表别名只在 SELECT 的 FROM/JOIN 范围中生效；定义别名后，限定列必须使用别名。
- `SELECT u.*`、列别名和表达式投影不在 V2 范围内。

### 3.3 名称绑定规则

`Column(name, qualifier)` 的 qualifier 是表名或表别名：

- 有 qualifier：只在对应表来源中查找列；限定符不存在抛 `E_TABLE_QUALIFIER_NOT_FOUND`。
- 无 qualifier：在当前输入 Schema 的所有表来源中查找；零个匹配抛 `E_COLUMN_NOT_FOUND`，多个匹配抛 `E_AMBIGUOUS_COLUMN`。
- FROM 和 JOIN 范围中的有效限定符不能重复，否则抛 `E_DUP_TABLE_ALIAS`。
- JOIN 按书写顺序左结合；某个 ON 只能引用其左侧已出现的表和当前右表，不能引用后续表。
- JOIN 输出 Schema 顺序为左子树全部列后接右表全部列。
- 单表 `SELECT *` 的结果表头保持原列名；JOIN 的 `SELECT *` 使用 `限定符.列名` 作为表头，避免重复列名无法辨认。
- 显式限定投影使用 `限定符.列名` 作为表头，未限定投影仍使用原列名。

### 3.4 BOOLEAN 规则

- SQL 字面量 `TRUE`、`FALSE` 分别转换为 Python `True`、`False`。
- BOOLEAN 列只接受 `type(value) is bool` 的值。
- INT 列继续显式拒绝 bool，避免 Python 的 bool/int 继承关系造成误判。
- BOOLEAN 在存储记录中编码为一个字节：`0x00` 表示 FALSE，`0x01` 表示 TRUE；其他编码视为存储损坏。
- BOOLEAN 表达式使用二值逻辑和短路求值。
- 布尔列、布尔字面量和比较表达式可以直接作为 WHERE、ON、AND、OR、NOT 的操作数。
- V2 不允许 NULL，因此不实现 UNKNOWN。

### 3.5 多语句解析与执行结果

A 新增：

```python
def parse_script(sql: str) -> tuple[ParsedStatement, ...]: ...
```

每个 `ParsedStatement` 包含：

- `statement`：该语句 AST；
- `sql`：该语句原始文本；
- `span`：该语句在完整输入中的一基行列范围。

C 新增：

```python
def execute_script(sql: str, *, stop_on_error: bool = True) -> ScriptResult: ...
def execute_file(path: str | Path, *, stop_on_error: bool = True) -> ScriptResult: ...
```

执行语义：

- 各语句按源码顺序串行执行。
- 每条语句独立持久化，整个脚本不具备事务原子性。
- `stop_on_error=True` 时首个错误后停止，`ScriptResult.stopped_early=True`。
- `stop_on_error=False` 时记录错误并继续执行后续语句。
- SQL 文件统一按 UTF-8 读取；打开或解码失败抛 `E_INPUT_FILE`。
- 空输入返回空的 `ScriptResult`，不视为语法错误。

### 3.6 新错误码

| 错误码 | 归属 | 含义 |
|---|---|---|
| `E_AMBIGUOUS_COLUMN` | C | 未限定列在多个输入表中同时存在 |
| `E_TABLE_QUALIFIER_NOT_FOUND` | C | 表名或表别名限定符不存在 |
| `E_DUP_TABLE_ALIAS` | C | FROM/JOIN 范围出现重复有效限定符 |
| `E_BOOLEAN_REQUIRED` | C | WHERE、ON 或逻辑运算的操作数不是 BOOLEAN |
| `E_INPUT_FILE` | C | SQL 文件无法打开或无法按 UTF-8 解码 |

语法中出现 V2 不支持的 JOIN 类型、ORDER BY 或 LIMIT 时，A 仍抛 `E_SYNTAX`。

## 4. A、B、C 工作边界

### 4.1 A：编译模块

负责目录：`compiler/`，维护 `contracts/ast.py` 中由 A 产生的 AST 形状。

主要工作：

1. 增加 `BOOLEAN`、`TRUE`、`FALSE`、`OR`、`NOT`、`AS`、`INNER`、`JOIN`、`ON` 关键字。
2. Lexer 继续输出 Token 类型、词素、全局行列位置。
3. 按优先级重构表达式递归下降解析器。
4. 解析表限定列和列与列比较。
5. 解析 FROM 表别名及链式 INNER JOIN。
6. 实现 `parse_script`，直接消费完整 Token 流，不使用字符串 `split(';')`。
7. 为每条语句生成准确的 `SourceSpan` 和原始 SQL 文本。
8. 保证标识符和别名进入 AST 前统一转为小写。

A 不负责：

- 查询表、列和别名是否存在；
- 推导表达式类型；
- 判断未限定列是否歧义；
- JOIN 执行、逻辑优化和 TUI；
- BOOLEAN 的磁盘编码。

A 交付验收：

- 使用 AST 精确体现 `NOT > AND > OR` 的优先级；
- 括号能覆盖默认优先级；
- `u.id = o.user_id` 两侧均为带限定符的 `Column`；
- 多语句中的错误位置相对于完整输入准确；
- 字符串中的分号不会切分语句；
- V1.1 语法继续可解析。

### 4.2 B：存储模块

负责目录：`storage/`，实现 `contracts/storage.py` 约定的持久化语义。

主要工作：

1. 增加 BOOLEAN 值校验、记录编码和记录解码。
2. 将 Catalog 从 `catalog.json` 迁移为页式内部系统表。
3. 系统表复用 Pager、Buffer Pool、Slotted Page、Row 编解码和 flush 链路。
4. 实现系统目录初始化、自举、加载、注册、注销和异常回滚。
5. 实现 V1 数据目录向 V2 Catalog 的一次性迁移。
6. 保证重启、多数据库、多连接和删除重建场景下的目录一致性。

V2 Catalog 使用两张内部系统表：

```text
__sys_tables(
    table_id INT,
    table_name TEXT,
    file_name TEXT
)

__sys_columns(
    table_id INT,
    ordinal INT,
    column_name TEXT,
    column_type TEXT
)
```

自举规则：

- 两张系统表的 Schema 由 B 以内置常量定义，启动时不查询 Catalog。
- 新数据库先创建并刷盘两张系统表，再对外视为创建成功。
- 重启时直接使用内置 Schema 扫描系统表，重建内存 Catalog。
- `table_id` 在单个数据库内单调递增，重启后不回退。
- `ordinal` 从 0 开始，恢复后必须保持用户列声明顺序。
- `column_type` 只允许 `INT/TEXT/REAL/BOOLEAN`。
- 系统表不出现在 `list_tables()` 中，也不能通过普通 SQL 读写。
- `__sys_` 前缀为 B 的保留表名；用户建表使用该前缀时抛 `E_BAD_ARG`。

迁移规则：

1. 已存在页式系统表时，以系统表为唯一权威目录。
2. 只有 V1 `catalog.json` 时，读取并校验旧目录，创建、填充并刷盘系统表。
3. 系统表完整重读校验成功后，将旧文件重命名为 `catalog.v1.migrated.json`。
4. 迁移失败时保留原 JSON，不把不完整系统表作为有效 Catalog。
5. 新建数据库不再创建 `catalog.json`。

V2 不提供进程崩溃时的 DDL 原子性；普通异常必须尽力回滚本次元数据和孤儿表文件。启动时发现目录记录与表文件不一致时抛 `E_STORAGE`，不得静默忽略。

B 不负责：

- SQL、AST、别名和 JOIN 语义；
- WHERE/ON 表达式求值；
- 逻辑计划或逻辑优化；
- TUI 组件。

B 交付验收：

- 数据目录中不存在作为权威目录的 `catalog.json`；
- Catalog 页读写能反映在 Buffer Pool 命中和写回统计中；
- BOOLEAN 跨页、更新、删除、溢出行和重启后均正确；
- V1 数据目录可以迁移并继续查询；
- 系统表损坏、类型非法和目录/表文件不一致统一抛 `E_STORAGE`。

### 4.3 C：运行模块与 TUI

负责目录：`runner/`、TUI 包和根装配入口。

主要工作：

1. 扩展 LogicalSchema，使每列保留来源表名、有效限定符、列名、类型和当前行位置。
2. 实现限定列解析、未限定列歧义检查和别名冲突检查。
3. 实现 BOOLEAN 类型绑定和 AND/OR/NOT 短路求值。
4. 实现列与列比较，并继续处理 INT 到 REAL 的数值提升。
5. 增加 `LogicalJoin` 与 `NestedLoopJoinExecutor`。
6. 实现规则型 `LogicalOptimizer`。
7. 实现 `execute_script`、`execute_file` 和批量结果汇总。
8. 实现 TUI，并只通过 Runner 门面和公开 Storage 协议访问系统。
9. 提供初始计划与优化后计划的树形展示，支持关闭优化器进行结果对照。

C 不负责：

- SQL 字符切分、Token 化和 AST 语法构建；
- 表数据、BOOLEAN 和 Catalog 的磁盘格式；
- Buffer Pool 的淘汰实现；
- 代价估算、物理计划选择和索引维护。

C 交付验收：

- 表限定列、别名和歧义错误符合契约；
- 多个 INNER JOIN 按左结合顺序执行；
- ON 在加入右表后绑定，不能引用后续表；
- WHERE 和 ON 非 BOOLEAN 时抛 `E_BOOLEAN_REQUIRED`；
- 优化器开关前后查询结果和错误行为一致；
- TUI 能编辑多行 SQL、运行脚本文件并逐条展示结果或错误。

## 5. 逻辑计划与优化规则

### 5.1 初始计划

单表查询：

```text
Projection
  -> Filter，可选
    -> Scan
```

JOIN 查询：

```text
Projection
  -> Filter，可选，来自 WHERE
    -> Join，保存 ON 谓词
      -> 左子计划
      -> 右侧 Scan
```

链式 JOIN 按 SQL 书写顺序构造左深树。`LogicalJoin.output_schema` 是左右 Schema 拼接后的新 Schema，所有列位置必须重新编号。

### 5.2 V2 必做优化规则

1. **逻辑表达式常量折叠**
   - 计算字面量之间的比较；
   - 化简 `TRUE AND x`、`FALSE AND x`、`TRUE OR x`、`FALSE OR x` 和 `NOT` 常量。
2. **AND 谓词拆分与 Filter 合并**
   - 将 AND 链展平为独立 conjunct；
   - 合并相邻 Filter，保持可重复优化的规范形态。
3. **INNER JOIN 谓词下推**
   - 仅引用左子树列的 conjunct 下推到左侧；
   - 仅引用右子树列的 conjunct 下推到右侧；
   - 同时引用左右两侧或无法安全拆分的谓词保留在 JOIN 或其上方；
   - OR 跨多个来源时不得拆开下推。
4. **投影裁剪**
   - 从最终投影、WHERE 和各 JOIN ON 反向收集必需列；
   - 在不改变输出和绑定位置的前提下缩减中间 Schema；
   - V2 的 SeqScan 可以仍解码完整物理行，但向上游只输出必需列。
5. **空结果与冗余节点消除**
   - 恒真 Filter 删除；
   - 恒假谓词替换为 `LogicalEmpty`；
   - 不删除承担最终列顺序和表头语义的 Projection。

每个规则实现为 `LogicalPlan -> LogicalPlan` 的纯转换。优化器不得访问 Storage 数据，不得执行计划，也不得原地修改输入计划。

### 5.3 优化器验证

- 提供初始计划和优化后计划快照测试。
- 对同一查询分别关闭和开启优化器，比较完整 `QueryResult`。
- 使用随机小表验证结果等价性。
- 统计 Scan、Filter、Join 各节点输入/输出行数，用于 TUI 展示优化效果。
- 不以执行时间作为正确性断言，避免测试受运行环境影响。

## 6. TUI 设计

TUI 是 C 的交互层，不直接 import `compiler` 或 `storage` 的内部模块。根装配层负责向 Runner 注入 A 的解析入口和 B 的 DatabaseServer。

建议界面：

```text
+----------------+-----------------------------------+
| 数据库/表结构  | 多行 SQL 编辑器                   |
|                |                                   |
+----------------+-----------------------------------+
| Token / AST / 初始计划 / 优化后计划                |
+----------------------------------------------------+
| 当前语句结果、错误、耗时和影响行数                 |
+----------------------------------------------------+
| 当前数据库 | 执行进度 | 缓存命中率                 |
+----------------------------------------------------+
```

必做交互：

- 多行编辑和当前缓冲区执行；
- 打开并执行 UTF-8 SQL 文件；
- 选择遇错停止或继续；
- 显示每条语句的源码范围、结果、错误和耗时；
- 浏览数据库、用户表和列；
- 查看 Token、AST、初始逻辑计划和优化后逻辑计划；
- 查看当前数据库、缓存统计和脚本执行进度；
- 保留纯 CLI/Runner API，自动化测试不能依赖 TUI。

推荐使用 Textual 实现，但 TUI 框架不属于跨模块契约；采用其他框架时，上述 Runner API 和行为不得改变。

## 7. 实施计划

以下时间按三人并行开发估算，共约 20 至 25 个工作日。

### M0：契约冻结与测试骨架，1 至 2 天

共同完成：

- 审核并冻结 V2 AST、文法、错误码和结果类型；
- 确认系统表 Schema、迁移策略和非目标；
- 将 V1.1 Golden 全部纳入回归；
- 新建 V2 Golden 分组和模块测试空壳。

退出条件：A、B、C 对共享类型和错误归属无未决分歧。

### M1：三条基础链并行，4 至 5 天

- A：BOOLEAN Token/字面量、通用表达式解析、`parse_script` 和 SourceSpan。
- B：BOOLEAN 编解码；设计并实现页式系统表自举。
- C：升级绑定表达式和 LogicalSchema；实现 `execute_script`、`execute_file`；搭建 TUI 外壳。

退出条件：BOOLEAN 单表 SQL 和多语句脚本在无 JOIN、无优化条件下端到端通过。

### M2：限定列、别名与 INNER JOIN，5 至 6 天

- A：TableRef、限定列、别名、INNER JOIN 和 ON 的 AST 构建。
- B：完成页式 Catalog 注册、注销、重启恢复和 V1 迁移。
- C：名称绑定、歧义检查、LogicalJoin、Nested Loop Join Executor。

退出条件：单个和链式 INNER JOIN 正确执行，页式 Catalog 重启测试通过。

### M3：逻辑优化，4 至 5 天

- C 主导实现常量折叠、Filter 规范化、谓词下推、投影裁剪和 LogicalEmpty。
- A 补齐表达式、脚本位置和错误用例。
- B 完成 Catalog 损坏测试、多连接测试和 Buffer Pool 统计验证。

退出条件：全部规则有计划形状测试，优化开关结果等价测试通过。

### M4：TUI 集成与综合验收，4 至 5 天

- 接入 SQL 编辑、文件执行、结果表格和错误定位；
- 接入 Token、AST、优化前后计划视图；
- 接入数据库结构、缓存统计和节点行数；
- 执行大量数据、重启、多数据库、多 JOIN 和错误恢复测试；
- 准备演示 SQL、截图和报告数据。

退出条件：从全新数据目录和 V1 数据目录启动时均能完成完整演示。

## 8. 依赖与集成顺序

```text
V2 契约冻结
  |-- A：表达式 + BOOLEAN + parse_script
  |     `-- A：TableRef + JOIN AST -----------+
  |                                            |
  |-- B：BOOLEAN 编解码                        |
  |     `-- B：页式 Catalog + V1 迁移          |
  |                                            v
  `-- C：Schema/表达式绑定 -> JOIN -> 逻辑优化 -> TUI 完整集成
          `-- execute_script/file -> TUI 批量执行
```

集成顺序：

1. A、C 先打通 BOOLEAN 和表达式；
2. B、C 打通 BOOLEAN 持久化；
3. A、C 打通限定列、别名和 JOIN；
4. B 完成页式 Catalog 后进行重启集成；
5. C 在稳定的 JOIN 计划上接入优化器；
6. 最后统一接入 TUI，避免界面成为核心逻辑的调试入口。

## 9. 测试计划

### 9.1 A 测试

- Token：新增关键字、布尔字面量、点号和跨行位置；
- 优先级：`NOT`、`AND`、`OR` 和多层括号；
- 比较：列与列、列与字面量、字面量与字面量；
- 表引用：有无 AS 的别名、限定列、链式 JOIN；
- 脚本：空脚本、多语句、末尾无分号、字符串内分号、第二条语句错误位置。

### 9.2 B 测试

- BOOLEAN 正常值、错误 Python 类型和非法磁盘字节；
- 新库系统表自举和用户表注册；
- 建表、删表、重建、重启、多连接一致性；
- V1 JSON 迁移成功、迁移中断恢复和迁移后重启；
- 系统表页经过 Buffer Pool 并计入统计；
- 系统表损坏、重复元数据和孤儿文件检测。

### 9.3 C 测试

- 限定列成功、限定符不存在和未限定列歧义；
- BOOLEAN 类型检查及 AND/OR/NOT 短路；
- JOIN 无匹配、一对一、一对多、多对多和链式 JOIN；
- ON 与 WHERE 的绑定范围；
- 每条优化规则的计划快照；
- 优化开关的结果等价性；
- SQL 文件读取失败和 stop_on_error 两种模式。

### 9.4 综合测试

- V1.1 的 37 条 Golden SQL 全部通过；
- 一份 SQL 文件完成建库、建表、BOOLEAN 插入、JOIN 查询、更新、删除和重启查询；
- 1000 行以上数据的 JOIN 与谓词下推结果正确；
- 重启后数据、BOOLEAN 和 Catalog 均保持一致；
- TUI 和直接 Runner API 对相同脚本返回一致结果。

## 10. 完成定义

满足以下条件后 V2 才算完成：

- `contracts.__version__ == "2.0"`，实现与 V2 契约一致；
- V1.1 回归和全部 V2 测试通过；
- 新数据库的权威 Catalog 只通过页式系统表持久化；
- 多行、多语句和 UTF-8 SQL 文件可以执行并准确定位错误；
- BOOLEAN 在解析、绑定、执行、存储和重启链路中完整可用；
- AND、OR、NOT、括号、列与列比较、限定列和别名行为符合文法；
- 单个与链式 INNER JOIN 正确执行；
- 逻辑优化器具备独立开关，优化前后结果一致；
- TUI 能展示 SQL 输入、逐语句结果、错误、AST 和优化前后计划；
- 文档和演示不包含 ORDER BY、LIMIT 等 V2 非目标功能。
