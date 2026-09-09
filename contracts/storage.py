"""契约 V1.1 —— Storage 共享数据形状与公开接口协议。

BaseDatabaseServer 和 BaseStorage 只约定 B 向 C 暴露的方法；具体实现、
文件格式与内部状态均由 storage/ 维护。

B 的内部（文件格式、目录布局、是否分页）完全自由；契约只约束
“构造方式 + 方法语义 + 持久化结果”。

红线：
- B 不解析 SQL，不知道 SELECT / WHERE 是什么；
- 表名、列名统一小写（由 A 转换），B 不做大小写处理；
- DatabaseServer 接收数据目录并创建默认库，connect() 返回绑定目标库的
  BaseStorage 实现；进程重启后数据必须完整可读。
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
    """表结构：describe 的返回格式，由 B 构造、C 读取。"""

    name: str
    columns: tuple[ColumnDef, ...]   # 建表顺序，只读


class BaseStorage(Protocol):
    """绑定单个数据库的表级存储接口。"""

    def create_table(
        self,
        name: str,
        columns: Sequence[ColumnDef],
    ) -> None: ...

    def drop_table(self, name: str) -> None: ...

    def list_tables(self) -> list[str]: ...

    def describe(self, name: str) -> TableInfo: ...

    def insert(self, name: str, values: Sequence[Value]) -> RowId: ...

    def scan(self, name: str) -> Iterator[Row]: ...

    def update_row(
        self,
        name: str,
        row_id: RowId,
        values: Sequence[Value],
    ) -> None: ...

    def delete_row(self, name: str, row_id: RowId) -> None: ...


class BaseDatabaseServer(Protocol):
    """管理数据库并创建 BaseStorage 连接的库级接口。"""

    def create_database(self, name: str) -> None: ...

    def drop_database(self, name: str) -> None: ...

    def list_databases(self) -> list[str]: ...

    def has_database(self, name: str) -> bool: ...

    def connect(self, name: str) -> BaseStorage: ...
