from __future__ import annotations

from dataclasses import dataclass

from contracts.storage import BaseDatabaseServer, BaseStorage


@dataclass(slots=True)
class ExecutionContext:
    """
    Executor 共享的执行环境与会话状态。
    """

    server: BaseDatabaseServer
    """DatabaseServer：建库 / 删库 / 连接数据库。"""

    storage: BaseStorage
    """当前数据库的表级操作连接（Storage）。"""

    current_database: str
    """当前数据库名，用于删库时的会话检查。"""
