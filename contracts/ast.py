"""契约 V1.1 —— AST（编译模块 A 的唯一输出，运行模块 C 的输入）。

冻结规则：字段与语义一经确认即冻结；任何改动需三方同意，并同步
docs/contract-v1.md 与 tests/golden_sql.py。

不变式（A 必须保证，C 可以信任）：
- database / table / column 名一律已小写、非空；
- 值已转成 Python 原生类型，AST 里不允许出现字符串形态的数字；
- 表达式只有 列 op 字面量，用 AND 连接，没有括号、没有 OR。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import TypeAlias


class SqlType(Enum):
    """V1 只支持三种类型。"""

    INT = "INT"
    TEXT = "TEXT"
    REAL = "REAL"


Value: TypeAlias = int | str | float
"""字面量在 Python 中的表示：INT->int；TEXT->str；REAL->int 或 float。"""


@dataclass(frozen=True)
class ColumnDef:
    """CREATE TABLE 里的一个列定义。"""

    name: str       # 已小写、非空
    type: SqlType


@dataclass(frozen=True)
class Assignment:
    """UPDATE ... SET 里的一项。同列重复赋值 = 后者覆盖前者。"""

    column: str     # 已小写、非空
    value: Value


# ---------- 表达式（V1 最小集） ----------


@dataclass(frozen=True)
class Column:
    """WHERE 里的列引用。"""

    name: str


@dataclass(frozen=True)
class Literal:
    """WHERE 里的字面量。"""

    value: Value


@dataclass(frozen=True)
class Cmp:
    """比较：左边必须是列，右边必须是字面量。"""

    left: Column
    op: str            # 只允许 '=' '<>' '<' '<=' '>' '>='
    right: Literal


@dataclass(frozen=True)
class And:
    """AND 连接。V1 没有 OR、没有括号。"""

    left: Cmp | And
    right: Cmp | And


Expr: TypeAlias = Cmp | And


# ---------- 语句 ----------


# ---------- 数据库语句 ----------


@dataclass(frozen=True)
class CreateDatabaseStmt:
    name: str      # 已小写、非空


@dataclass(frozen=True)
class DropDatabaseStmt:
    name: str      # 已小写、非空


@dataclass(frozen=True)
class UseDatabaseStmt:
    name: str      # 已小写、非空


# ---------- 表语句 ----------


@dataclass(frozen=True)
class CreateTableStmt:
    table: str
    columns: tuple[ColumnDef, ...]   # 非空、列名不重复；顺序 = 物理存储顺序


@dataclass(frozen=True)
class DropTableStmt:
    table: str


@dataclass(frozen=True)
class InsertStmt:
    table: str
    values: tuple[Value, ...]        # 与表列数相同、按建表列顺序


@dataclass(frozen=True)
class SelectStmt:
    columns: tuple[str, ...] | None  # None 表示 SELECT *，展开顺序 = 建表顺序
    table: str
    where: Expr | None


@dataclass(frozen=True)
class UpdateStmt:
    table: str
    assignments: tuple[Assignment, ...]
    where: Expr | None


@dataclass(frozen=True)
class DeleteStmt:
    table: str
    where: Expr | None


Statement: TypeAlias = (
    CreateDatabaseStmt
    | DropDatabaseStmt
    | UseDatabaseStmt
    | CreateTableStmt
    | DropTableStmt
    | InsertStmt
    | SelectStmt
    | UpdateStmt
    | DeleteStmt
)
