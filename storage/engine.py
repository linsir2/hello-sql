"""行级执行层（PRD §8；D07/D08/D14/D15）。

职责：公开方法拿到的是“Python 值元组 / row_id”，本层负责把它们
翻译成页内的记录字节并反向解码；页分配细节交给 pager。

不变量（D07/D15，页内布局）：
- 数据页：页头 8 B（u16 slot_count + u16 flags + u32 free_ptr），
  记录从 offset 8 起向后写，槽（8 B = offset + len）从页尾向前长；
- 任何时刻：活记录连在页头之后、槽连在页尾之前，中间是单块连续空闲区；
  空闲区 = (PAGE_SIZE - 8 * slot_count) - free_ptr；
- 删除 / 整行更新后立即页内紧凑（D15）；整页空 → free_page（D05）；
- 记录编码：u64 row_id + 按列序的值（INT 8 B / REAL 8 B / TEXT 4 B 长 + UTF-8），
  不带类型标签；解码按同一份 ColumnDef；解码失败 → E_STORAGE。

row_id 不变量（D08）：
- 每表单调递增、永久不复用，计数器持久化在页 0（next_row_id）；
- 运行期维护 rid → 页号 内存映射；页内按记录头 row_id 定位；
- 映射缺失时允许退化全表找；找不到 → E_ROW_NOT_FOUND。

超长行（D14）：编码长度 > INLINE_RECORD_LIMIT 时走溢出页链；M5 实现。

实现阶段：M2（行存取），M5（溢出页链）。
"""

from __future__ import annotations

from typing import Iterator, Sequence

from contracts.ast import ColumnDef, Value
from contracts.storage import Row, RowId

from storage.cache import BufferPool


def encode_record(
    row_id: RowId, columns: Sequence[ColumnDef], values: Sequence[Value]
) -> bytes:
    """把一行值按列序编码成记录字节（开头带 u64 row_id）。M2 实现。"""
    raise NotImplementedError("M2：encode_record")


def decode_record(record: bytes, columns: Sequence[ColumnDef]) -> Row:
    """按同一份 ColumnDef 反向解码记录 → (row_id, values 元组)。M2 实现。"""
    raise NotImplementedError("M2：decode_record")


class TableEngine:
    """单表行级执行入口（M2 时定稿方法面；这里是骨架占位）。

    计划职责：该表的 insert / scan / update / delete 行语义；
    维护 rid → 页映射与页内 slot 管理；整页空了通知 pager 回收。
    """

    def __init__(self, table_path: str, pool: BufferPool) -> None:
        """M0 骨架：构造参数为草案，M2 会结合 Catalog 的列定义定稿。"""
        raise NotImplementedError("M0：TableEngine.__init__")

    def insert(self, values: Sequence[Value]) -> RowId:
        raise NotImplementedError("M2：TableEngine.insert")

    def scan(self) -> Iterator[Row]:
        raise NotImplementedError("M2：TableEngine.scan")

    def update(self, row_id: RowId, values: Sequence[Value]) -> None:
        raise NotImplementedError("M2：TableEngine.update")

    def delete(self, row_id: RowId) -> None:
        raise NotImplementedError("M2：TableEngine.delete")
