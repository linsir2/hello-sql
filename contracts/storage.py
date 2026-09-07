"""契约 V1.1 —— Storage 相关的共享数据形状。

这里只放 B 与 C 之间传递的“数据格式”，不包含任何方法定义：
Storage 类（含全部方法签名与实现）由 B 在 storage/ 目录里定义，
它是方法清单的唯一代码真相。
方法清单与语义的会议契约见 docs/contract-v1.md 第 3 节。

B 的内部（文件格式、目录布局、是否分页）完全自由；契约只约束
“构造方式 + 方法语义 + 持久化结果”。

红线：
- B 不解析 SQL，不知道 SELECT / WHERE 是什么；
- 表名、列名统一小写（由 A 转换），B 不做大小写处理；
- 实现类的构造方式必须是 Storage(data_dir: str | Path)，
  data_dir 不存在时自动创建；进程重启后数据必须完整可读。
"""

from __future__ import annotations

from dataclasses import dataclass

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
