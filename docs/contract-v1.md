# 契约 V1.0（冻结稿）

目标：做一个能运行的 SQL demo。用户输入一条 SQL，返回结果表格或错误。
全项目只有一条交接链：

```text
SQL 文本 ->(A parse)-> AST ->(C 语义检查 + WHERE 求值)-> 表级调用 ->(B CRUD)-> 行数据 ->(C 打印)
```

三个模块：A=编译（compiler/）、B=存储（storage/）、C=运行（runner/）。

## 0. 开工前必须画勾的默认决策

- [ ] 类型只有 INT / TEXT / REAL，不加 NULL、主键、事务、ORDER BY、JOIN。
- [ ] 表 / 列名统一转小写（A 负责）；关键字大小写不敏感。
- [ ] 元数据（schema）归 B，随数据持久化。
- [ ] WHERE 求值归 C；B 只提供整行扫描 / 整行替换。
- [ ] V1 表达式只有 `列 op 字面量`，用 AND 连接；无 OR、无括号、无列比列。
- [ ] UPDATE 同列重复赋值 = 后者覆盖前者。
- [ ] REAL 列收 int 或 float，B 内部统一存 float。
- [ ] row_id 只在“本次运行、scan 之后、update/delete 之前”有效。
- [ ] UPDATE/DELETE 时 C 必须先收集全部命中 row_id，再逐个修改。
- [ ] Python >= 3.10；单进程单线程；REPL 一行一条 SQL。
- [ ] SQL 层支持 CREATE / DROP / USE DATABASE（一个库 = 根目录下一个小写库名目录）。
- [ ] 默认库 `main` 启动自动创建、永远存在、不可删除；老用例无需先建库。
- [ ] 不能 DROP 当前正在使用的库（先 USE 别的库）。
- [ ] 库名与表名共用标识符规则：小写、非空、不能是保留字。

## 1. SQL 子集文法

```text
stmt       := CREATE DATABASE id
            | DROP DATABASE id
            | USE id
            | CREATE TABLE id '(' colDef (',' colDef)* ')'
            | DROP TABLE id
            | INSERT INTO id VALUES '(' value (',' value)* ')'
            | SELECT selectList FROM id [WHERE cond]
            | UPDATE id SET assign (',' assign)* [WHERE cond]
            | DELETE FROM id [WHERE cond]

colDef     := id type
type       := INT | TEXT | REAL
selectList := '*' | id (',' id)*
assign     := id '=' value
cond       := cmp { AND cmp }          # 没有 OR / 括号
cmp        := id op value              # 左边必须是列，右边必须是字面量
op         := '=' | '<>' | '<' | '<=' | '>' | '>='
value      := NUMBER | STRING
```

词法规则：

- 保留字：`CREATE DATABASE DROP USE TABLE INSERT INTO VALUES SELECT FROM WHERE UPDATE SET DELETE AND OR INT TEXT REAL`，不可当库名 / 表名 / 列名。
- 标识符：`[A-Za-z_][A-Za-z0-9_]*`，A 统一转小写后进 AST。
- NUMBER：`18` -> int；`18.5` -> float；允许前导负号 `-18`。
- STRING：单引号，`''` 转义单引号，不允许跨行，未闭合报语法错。
- 语句末尾允许至多一个分号；一次 parse 只收一条语句。

## 2. contracts/ 只放共享数据格式（不放任何人的方法）

原则：跨模块传递的“消息格式”放 contracts/（单一代码真相）；
任何模块的方法（parse、Storage 类、Runner 类）定义在各自包的
`__init__.py` 里，不在 contracts/ 重复。方法清单的文字版只在本文档。

| 文件 | 内容 | 谁写 | 谁读 |
|---|---|---|---|
| contracts/ast.py | AST 全部类型与不变式 | A 负责维护 | A 产出、C 消费 |
| contracts/storage.py | Storage 相关共享数据形状（Row/TableInfo） | 只读 | B 产出、C 读取 |
| contracts/errors.py | 12 个错误码与异常 | 三方共用 | 三方 + REPL |
| contracts/result.py | QueryResult | C 维护 | C 产出、测试消费 |

方法签名不写进共享代码：由 B 在 storage/__init__.py 里定义
`class DatabaseServer` 与 `class Storage` 并对外暴露
（别人 `from storage import DatabaseServer` 使用），
作为方法清单的唯一代码真相；本文档第 3 节的方法表是三方对语义的会议契约。

## 3. B 交付物：DatabaseServer（库层）+ Storage（表层）

B 分两层：

- `DatabaseServer(data_dir)`：管理数据库；data_dir 不存在则创建，
  构造时自动创建默认库 `main`（永远存在、不可删除）；
- `Storage`：`server.connect(库名)` 返回，负责单个库内的表。

目录布局：`data/<库名>/<表文件>`。进程重启后，已建的库和表数据必须
完整可读。

### 3.0 表级函数（Storage，原 8 个方法不变）

| 函数 | 成功 | 失败（错误码） |
|---|---|---|
| `create_table(name, columns)` | 建表并持久化元数据；列顺序即永久顺序 | `E_TABLE_EXISTS`；空列/重复列 `E_DUP_COLUMN` |
| `drop_table(name)` | 删表 + 删数据 | `E_TABLE_NOT_FOUND` |
| `list_tables()` | 返回表名列表 | 基本不失败 |
| `describe(name)` | 返回 TableInfo | `E_TABLE_NOT_FOUND` |
| `insert(name, values)` | 追加一行，返回 row_id | 表不存在 / `E_VALUE_COUNT` / `E_TYPE_MISMATCH` |
| `scan(name)` | 返回行迭代器，顺序不保证 | `E_TABLE_NOT_FOUND` |
| `update_row(name, row_id, values)` | 整行替换 | 表不存在 / `E_ROW_NOT_FOUND` / 个数或类型错 |
| `delete_row(name, row_id)` | 删除一行 | 表不存在 / `E_ROW_NOT_FOUND` |

类型规则（B 是唯一校验点，C 在语义检查时用同一张表预检）：

| SQL 类型 | 接受的 Python 值 |
|---|---|
| INT | int（显式拒绝 bool） |
| TEXT | str |
| REAL | int 或 float，内部统一存 float |

文件格式、是否分页都是 B 的自由；契约只要求上面的语义和“重启可读”。

### 3.1 库级函数（DatabaseServer）

| 函数 | 成功 | 失败（错误码） |
|---|---|---|
| `create_database(name)` | 建库（创建 `<data_dir>/<name>/`） | 已存在 `E_DATABASE_EXISTS` |
| `drop_database(name)` | 删库，级联删除其中所有表 | 不存在 `E_DATABASE_NOT_FOUND`；库名为 main `E_DATABASE_IN_USE` |
| `list_databases()` | 返回库名列表（含 main），顺序不保证 | 基本不失败 |
| `has_database(name)` | 返回 bool | 基本不失败 |
| `connect(name)` | 返回绑定该库的 Storage | 不存在 `E_DATABASE_NOT_FOUND` |

“当前库”属于 C（Runner）的会话状态：USE 成功后 C 换一个
`connect()` 得到的 Storage 连接；B 不保存“当前库”。
DROP DATABASE 时由 C 先拦“当前正在使用的库”（E_DATABASE_IN_USE），
B 再拦默认库 main。

## 4. 错误码（12 个）

```text
E_SYNTAX           A 抛（ParseError，带行列号）
E_TABLE_NOT_FOUND  表不存在（C 语义检查抛；B 防御性检查同码）
E_TABLE_EXISTS     建表冲突
E_COLUMN_NOT_FOUND 列不存在（C 抛）
E_DUP_COLUMN       建表时列名重复（B 抛）
E_VALUE_COUNT      值个数与列数不符（C 预检；B 边界同码）
E_TYPE_MISMATCH    值类型不符（C 预检；B 边界同码）
E_ROW_NOT_FOUND    row_id 不存在（B 抛）
E_STORAGE          B 内部错误：文件损坏 / IO 失败（B 抛）
E_DATABASE_NOT_FOUND  库不存在（B 的 DatabaseServer 抛）
E_DATABASE_EXISTS     建库冲突（B 的 DatabaseServer 抛）
E_DATABASE_IN_USE     正在使用的库不可删（C 抛）或默认库 main 不可删（B 抛）
```

## 5. 执行结果

```python
@dataclass
class QueryResult:
    columns: tuple[str, ...] | None = None      # 仅 SELECT
    rows: tuple[tuple[Value, ...], ...] | None = None  # 仅 SELECT
    affected_rows: int | None = None            # DDL=0；DML=实际行数
```

SELECT 表头规则：显式列按书写顺序；`*` 按建表列顺序展开；允许重复列。
行顺序不保证（没有 ORDER BY）。
USE DATABASE 与建库/删库同属 DDL：affected_rows = 0；REPL 对 USE 可打印
“Database changed” 提示。

## 6. 模块红线

- `compiler`、`storage`、`runner` 之间禁止互相 import；各自只能
  import `contracts`。只有根目录的 main.py 允许同时 import 三家。
- A 不知道表存不存在；B 不知道 SQL 语法；C 不知道 B 的文件格式和 A 的实现。
- 谁都不许改别人目录里的文件；契约要改走第 8 节流程。

## 7. 交接与验收

- 交接链：`parse(sql) -> Statement` 交给 C；`DatabaseServer(data_dir)`
  交给 C；`Runner(server, parse).execute(sql) -> QueryResult` 交给 REPL / 测试。
- C 不等 A：用“SQL -> 手写 AST”的假 parse 开发。
- C 不等 B：自己写 FakeDatabaseServer / FakeStorage（实现同一套方法）开发。
- 各自交付验收：A=golden 中所有语句能解析成正确 AST / 正确报错；
  B=建表-插入-扫描-更新-删除 + 重启后数据仍在；C=用 FakeStorage 跑通
  语义检查、WHERE 求值、结果格式。
- 集成日：main.py 把真 parse + 真 Storage 注入 Runner，按顺序跑
  tests/golden_sql.py 全部 31 条，一条不差即完成。

## 8. 契约变更流程

1. 提议人写清“现状 -> 问题 -> 新条文”，并同步改 golden。
2. AST 变更至少 A+C 同意；Storage 变更至少 B+C 同意；文法 / 类型 / 错误码
   需要三方同意。
3. 同意后升版本号（V1.1），禁止悄悄改共享文件。
