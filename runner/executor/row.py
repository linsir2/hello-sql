from __future__ import annotations

from dataclasses import dataclass

from contracts.ast import Value
from contracts.storage import RowId


@dataclass(frozen=True, slots=True)
class ExecRow:
    """
    一条执行期记录。

    row_id 是 Storage 提供的内部行索引，不属于 SQL 可见列；values 的位置与当前算子输出 Schema 一一对应。
    """

    row_id: RowId
    values: tuple[Value, ...]
