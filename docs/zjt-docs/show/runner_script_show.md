# Runner 脚本与文件执行演示

以下命令均在项目根目录执行。每组演示使用独立数据目录，避免已有表
影响演示结果。

## 1. 单行 SQL

```bash
.venv/bin/python main.py --data-dir /tmp/hello-sql-single -e "CREATE TABLE users (id INT, name TEXT);"
.venv/bin/python main.py --data-dir /tmp/hello-sql-single -e "INSERT INTO users VALUES (1, 'alice');"
.venv/bin/python main.py --data-dir /tmp/hello-sql-single -e "SELECT * FROM users;"
```

最后一条命令应输出：

```text
id	name
1	alice
```

## 2. `-e` 多语句脚本

可直接复制执行：

```bash
.venv/bin/python main.py --data-dir /tmp/hello-sql-inline -e "CREATE TABLE flags (id INT, enabled BOOLEAN); INSERT INTO flags VALUES (1, TRUE); INSERT INTO flags VALUES (2, FALSE); SELECT * FROM flags WHERE enabled;"
```

## 3. TUI 多行、多语句输入

启动交互终端：

```bash
.venv/bin/python main.py --data-dir /tmp/hello-sql-tui
```

把下面整段粘贴到 SQL 缓冲区，然后按 Enter 执行：

```sql
CREATE TABLE users (id INT, name TEXT, active BOOLEAN);
CREATE TABLE orders (id INT, user_id INT, paid BOOLEAN);
INSERT INTO users VALUES (1, 'alice', TRUE);
INSERT INTO users VALUES (2, 'bob', FALSE);
INSERT INTO orders VALUES (10, 1, TRUE);
INSERT INTO orders VALUES (11, 2, TRUE);
SELECT u.name, o.id
FROM users u
JOIN orders o ON u.id = o.user_id
WHERE u.active AND o.paid;
```

手工编辑多行 SQL 时，使用 Alt+Enter 插入换行，Enter 提交当前整个
缓冲区。单条 SQL 的末尾分号可以省略；多条 SQL 之间必须使用分号。

## 4. 执行 SQL 文件

直接从命令行执行完整 JOIN 演示：

```bash
.venv/bin/python main.py --data-dir /tmp/hello-sql-file -f docs/zjt-docs/show/v2_runner_demo.sql
```

也可在 TUI 中执行：

```text
/file docs/zjt-docs/show/v2_runner_demo.sql
```

路径包含空格时使用引号：

```text
/file "path with spaces/demo.sql"
```

## 5. 遇错停止与继续

默认遇到第三条的类型错误后停止，后续 INSERT 和 SELECT 不执行：

```bash
.venv/bin/python main.py --data-dir /tmp/hello-sql-stop -f docs/zjt-docs/show/v2_runner_continue_on_error.sql
```

添加 `--continue-on-error` 后，错误会被记录，后续语句继续执行：

```bash
.venv/bin/python main.py --data-dir /tmp/hello-sql-continue --continue-on-error -f docs/zjt-docs/show/v2_runner_continue_on_error.sql
```

TUI 中使用以下命令切换同一行为：

```text
/stop-on-error off
/file docs/zjt-docs/show/v2_runner_continue_on_error.sql
```

恢复默认行为：

```text
/stop-on-error on
```

## 6. 语法错误边界

Runner 会先解析整段脚本，再逐条执行。如果脚本存在语法错误，整段脚本
不会开始执行。`stop-on-error` 只控制成功解析后的名称绑定、类型检查和
执行错误。
