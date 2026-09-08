from __future__ import annotations

from dataclasses import dataclass


@dataclass(slots=True)
class ExecutionContext:
    """
    Executor 共享的执行环境。
    """
    storage: object
