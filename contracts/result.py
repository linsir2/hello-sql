"""契约 V1.1 —— 执行结果（模块 C 输出，REPL 与测试消费）。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TypeAlias

from contracts.ast import Value


@dataclass
class QueryResult:
    """一次执行的结果。三种形态互斥：
    - CREATE / DROP：affected_rows = 0，其余为 None；
    - INSERT / UPDATE / DELETE：affected_rows = 实际影响行数（>=0）；
    - SELECT：columns + rows，affected_rows = None。
    """

    columns: tuple[str, ...] | None = None
    rows: tuple[tuple[Value, ...], ...] | None = None
    affected_rows: int | None = None
