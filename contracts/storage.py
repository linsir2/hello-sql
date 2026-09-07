"""契约 V1.0 —— 存储接口（模块 B 实现，模块 C 调用）。

B 的内部（文件格式、目录布局、是否分页）完全自由；契约只约束
“构造方式 + 方法签名 + 语义 + 持久化结果”。

红线：
- B 不解析 SQL，不知道 SELECT / WHERE 是什么；
- 表名、列名统一小写（由 A 转换），B 不做大小写处理；
- 实现类的构造方式必须是 Storage(data_dir: str | Path)，
  data_dir 不存在时自动创建；进程重启后数据必须完整可读。
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from typing import Protocol

from contracts.ast import ColumnDef, Value


RowId = int
"""B 返回的行把手：只保证“本次运行内、从 scan 拿到后、
到 update_row / delete_row 调用前”有效；重启后以重新 scan 为准。"""


Row = tuple[RowId, tuple[Value, ...]]
"""一行 = (row_id, 按建表顺序的值元组)。"""


@dataclass(frozen=True)
class TableInfo:
    name: str
    columns: tuple[ColumnDef, ...]   # 建表顺序，只读


class Storage(Protocol):
    """B 必须实现的方法集合。所有可预期失败抛 contracts.errors 里的错误码。"""

    def create_table(self, name: str, columns: Sequence[ColumnDef]) -> None:
        """建表并持久化元数据。
        - 表已存在 -> E_TABLE_EXISTS
        - columns 为空 / 列名重复 -> E_DUP_COLUMN
        - 列顺序即永久存储顺序
        """

    def drop_table(self, name: str) -> None:
        """删除表及其全部数据。表不存在 -> E_TABLE_NOT_FOUND。"""

    def list_tables(self) -> list[str]:
        """返回当前所有表名。顺序不保证。"""

    def describe(self, name: str) -> TableInfo:
        """返回表结构。表不存在 -> E_TABLE_NOT_FOUND。"""

    def insert(self, name: str, values: Sequence[Value]) -> RowId:
        """追加一行（values 按建表列顺序），返回新 row_id。
        - 表不存在 -> E_TABLE_NOT_FOUND
        - 长度与列数不匹配 -> E_VALUE_COUNT
        - 值类型不匹配 -> E_TYPE_MISMATCH
        - 类型规则：INT 只收 int（显式拒绝 bool）；TEXT 只收 str；
          REAL 收 int 或 float，内部统一存 float。
        """

    def scan(self, name: str) -> Iterator[Row]:
        """返回全表行迭代器。表不存在 -> E_TABLE_NOT_FOUND。
        行顺序不保证；迭代期间调用方不得同时写该表
        （执行层必须先收集 row_id 再逐个 update/delete）。
        """

    def update_row(self, name: str, row_id: RowId, values: Sequence[Value]) -> None:
        """整行替换：values 为完整新行，按建表列顺序。
        - 表不存在 -> E_TABLE_NOT_FOUND
        - row_id 不存在 -> E_ROW_NOT_FOUND
        - 长度 / 类型校验规则同 insert
        """

    def delete_row(self, name: str, row_id: RowId) -> None:
        """删除一行。
        - 表不存在 -> E_TABLE_NOT_FOUND
        - row_id 不存在 -> E_ROW_NOT_FOUND
        """

