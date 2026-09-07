"""Golden SQL 样例集 —— 三方的共同语言。

用法：列表按顺序执行才有意义（后面的语句依赖前面的表状态）。
每个用例字段：
- id：编号；
- sql：要执行的 SQL；
- expect："ok" 或 "error"；
- code：expect=error 时的错误码；
- affected：DML 期望的影响行数；
- header / row_count / rows_any_order：SELECT 的期望
  （行顺序不保证，所以比较时用集合，不比较顺序）。

REAL 列内部存 float，例如插入 18 再查出来是 18.0；
Python 里 18 == 18.0，所以期望值写成整数也能比较通过。
"""

from typing import Any

GOLDEN_SQL: list[dict[str, Any]] = [
    # ---- 建表与重复建表 ----
    {
        "id": "g01",
        "sql": "CREATE TABLE users (id INT, name TEXT, age REAL);",
        "expect": "ok",
        "affected": 0,
        "note": "建表成功",
    },
    {
        "id": "g02",
        "sql": "CREATE TABLE users (id INT, name TEXT, age REAL);",
        "expect": "error",
        "code": "E_TABLE_EXISTS",
        "note": "同名表已存在",
    },
    # ---- INSERT：正常 / 类型错 / 个数错 ----
    {
        "id": "g03",
        "sql": "INSERT INTO users VALUES (1, 'alice', 18);",
        "expect": "ok",
        "affected": 1,
        "note": "int 进 REAL 列合法，B 内部存 18.0",
    },
    {
        "id": "g04",
        "sql": "INSERT INTO users VALUES (2, 'bob', 5.0);",
        "expect": "ok",
        "affected": 1,
        "note": "正常插入第二行",
    },
    {
        "id": "g05",
        "sql": "INSERT INTO users VALUES ('x', 'alice', 18);",
        "expect": "error",
        "code": "E_TYPE_MISMATCH",
        "note": "字符串进 INT 列",
    },
    {
        "id": "g06",
        "sql": "INSERT INTO users VALUES (3, 'alice');",
        "expect": "error",
        "code": "E_VALUE_COUNT",
        "note": "值个数与列数不符",
    },
    # ---- SELECT ----
    {
        "id": "g07",
        "sql": "SELECT * FROM users;",
        "expect": "ok",
        "header": ("id", "name", "age"),
        "row_count": 2,
        "rows_any_order": [(1, "alice", 18), (2, "bob", 5)],
        "note": "全表两行，顺序不保证",
    },
    {
        "id": "g08",
        "sql": "SELECT name, age FROM users WHERE age >= 18;",
        "expect": "ok",
        "header": ("name", "age"),
        "row_count": 1,
        "rows_any_order": [("alice", 18)],
        "note": "投影 + WHERE 过滤",
    },
    {
        "id": "g09",
        "sql": "SELECT name FROM users WHERE id = 99;",
        "expect": "ok",
        "header": ("name",),
        "row_count": 0,
        "rows_any_order": [],
        "note": "无命中：有表头、零行",
    },
    {
        "id": "g10",
        "sql": "SELECT nope FROM users;",
        "expect": "error",
        "code": "E_COLUMN_NOT_FOUND",
        "note": "列不存在",
    },
    {
        "id": "g11",
        "sql": "SELECT name FROM user;",
        "expect": "error",
        "code": "E_TABLE_NOT_FOUND",
        "note": "表不存在",
    },
    # ---- UPDATE / DELETE ----
    {
        "id": "g12",
        "sql": "UPDATE users SET age = 19 WHERE name = 'alice';",
        "expect": "ok",
        "affected": 1,
        "note": "按条件更新一行",
    },
    {
        "id": "g13",
        "sql": "UPDATE users SET age = 19 WHERE name = 'nobody';",
        "expect": "ok",
        "affected": 0,
        "note": "无命中：影响 0 行",
    },
    {
        "id": "g14",
        "sql": "DELETE FROM users WHERE id = 2;",
        "expect": "ok",
        "affected": 1,
        "note": "删除 bob",
    },
    {
        "id": "g15",
        "sql": "SELECT * FROM users;",
        "expect": "ok",
        "header": ("id", "name", "age"),
        "row_count": 1,
        "rows_any_order": [(1, "alice", 19)],
        "note": "更新与删除后的状态",
    },
    # ---- DROP 与重建 ----
    {
        "id": "g16",
        "sql": "DROP TABLE users;",
        "expect": "ok",
        "affected": 0,
        "note": "删除表",
    },
    {
        "id": "g17",
        "sql": "DROP TABLE users;",
        "expect": "error",
        "code": "E_TABLE_NOT_FOUND",
        "note": "重复删表报错",
    },
    {
        "id": "g18",
        "sql": "CREATE TABLE users (id INT, id TEXT);",
        "expect": "error",
        "code": "E_DUP_COLUMN",
        "note": "重复列名（B 负责检查）",
    },
    # ---- 语法错误（A 负责，parse 阶段即失败） ----
    {
        "id": "g19",
        "sql": "SELEC * FROM users;",
        "expect": "error",
        "code": "E_SYNTAX",
        "note": "关键字拼写错误",
    },
    {
        "id": "g20",
        "sql": "SELECT FROM users;",
        "expect": "error",
        "code": "E_SYNTAX",
        "note": "SELECT 后缺列表",
    },
    {
        "id": "g21",
        "sql": "INSERT INTO users VALUES (1, 'abc);",
        "expect": "error",
        "code": "E_SYNTAX",
        "note": "字符串未闭合",
    },
    # ---- 数据库层（默认库 main 始终存在；前面 g01-g21 都在 main 中执行） ----
    {
        "id": "g22",
        "sql": "CREATE DATABASE shop;",
        "expect": "ok",
        "affected": 0,
        "note": "建库成功",
    },
    {
        "id": "g23",
        "sql": "CREATE DATABASE shop;",
        "expect": "error",
        "code": "E_DATABASE_EXISTS",
        "note": "重复建库报错",
    },
    {
        "id": "g24",
        "sql": "USE shop;",
        "expect": "ok",
        "affected": 0,
        "note": "切到 shop，后续表语句作用于 shop",
    },
    {
        "id": "g25",
        "sql": "CREATE TABLE orders (id INT, item TEXT);",
        "expect": "ok",
        "affected": 0,
        "note": "在 shop 里建表",
    },
    {
        "id": "g26",
        "sql": "DROP DATABASE shop;",
        "expect": "error",
        "code": "E_DATABASE_IN_USE",
        "note": "不能删除当前正在使用的库（C 拦截）",
    },
    {
        "id": "g27",
        "sql": "USE main;",
        "expect": "ok",
        "affected": 0,
        "note": "切回默认库 main",
    },
    {
        "id": "g28",
        "sql": "DROP DATABASE shop;",
        "expect": "ok",
        "affected": 0,
        "note": "当前不在 shop，可删除，级联删掉 orders 表",
    },
    {
        "id": "g29",
        "sql": "USE shop;",
        "expect": "error",
        "code": "E_DATABASE_NOT_FOUND",
        "note": "已删除的库不能连接",
    },
    {
        "id": "g30",
        "sql": "DROP DATABASE shop;",
        "expect": "error",
        "code": "E_DATABASE_NOT_FOUND",
        "note": "重复删库报错",
    },
    {
        "id": "g31",
        "sql": "DROP DATABASE main;",
        "expect": "error",
        "code": "E_DATABASE_IN_USE",
        "note": "默认库 main 不可删除（B 拦截）",
    },
    # ---- 跨库隔离（不同库里的表互不可见） ----
    {
        "id": "g32",
        "sql": "CREATE DATABASE shop2;",
        "expect": "ok",
        "affected": 0,
        "note": "再建一个库 shop2",
    },
    {
        "id": "g33",
        "sql": "USE shop2;",
        "expect": "ok",
        "affected": 0,
        "note": "切到 shop2",
    },
    {
        "id": "g34",
        "sql": "CREATE TABLE orders (id INT, item TEXT);",
        "expect": "ok",
        "affected": 0,
        "note": "在 shop2 里建 orders 表",
    },
    {
        "id": "g35",
        "sql": "USE main;",
        "expect": "ok",
        "affected": 0,
        "note": "切回 main",
    },
    {
        "id": "g36",
        "sql": "SELECT * FROM orders;",
        "expect": "error",
        "code": "E_TABLE_NOT_FOUND",
        "note": "跨库隔离：main 里看不到 shop2 的 orders 表",
    },
    {
        "id": "g37",
        "sql": "DROP DATABASE shop2;",
        "expect": "ok",
        "affected": 0,
        "note": "清理 shop2（当前在 main，允许删除）",
    },
]
