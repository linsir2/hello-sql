"""DDL 执行器：消费五种命令计划，成功时统一返回 affected_rows=0。

- 数据库级：建库 / 删库 / 切换库，作用于 context.server；
- 表级：建表 / 删表，作用于 context.storage（即当前数据库）。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TypeAlias, assert_never

from contracts.ast import ColumnDef
from contracts.errors import E_DATABASE_IN_USE, SqlError
from contracts.result import QueryResult
from runner.executor.base import StatementExecutor
from runner.executor.context import ExecutionContext
from runner.logical_plan.plans import (
    LogicalCreateDatabase,
    LogicalCreateTable,
    LogicalDropDatabase,
    LogicalDropTable,
    LogicalUseDatabase,
)


def _ddl_success() -> QueryResult:
    """DDL 成功结果：每次新建实例，QueryResult 是可变 dataclass，不复用单例。"""
    return QueryResult(affected_rows=0)


# ---------- 表级 DDL ----------


@dataclass(frozen=True, slots=True)
class CreateTableExecutor(StatementExecutor):
    """建表：列定义按计划原顺序交给 Storage。"""

    table: str
    columns: tuple[ColumnDef, ...]

    def execute(self, context: ExecutionContext) -> QueryResult:
        context.storage.create_table(self.table, self.columns)
        return _ddl_success()


@dataclass(frozen=True, slots=True)
class DropTableExecutor(StatementExecutor):
    """删表：作用域为当前数据库。"""

    table: str

    def execute(self, context: ExecutionContext) -> QueryResult:
        context.storage.drop_table(self.table)
        return _ddl_success()


# ---------- 数据库级 DDL ----------


@dataclass(frozen=True, slots=True)
class CreateDatabaseExecutor(StatementExecutor):
    """建库：存在性由 DatabaseServer 校验。"""

    name: str

    def execute(self, context: ExecutionContext) -> QueryResult:
        context.server.create_database(self.name)
        return _ddl_success()


@dataclass(frozen=True, slots=True)
class DropDatabaseExecutor(StatementExecutor):
    """删库：会话检查先于 Server 调用，当前库不允许删除。"""

    name: str

    def execute(self, context: ExecutionContext) -> QueryResult:
        if self.name == context.current_database:
            raise SqlError(
                E_DATABASE_IN_USE,
                f"database in use: {self.name}",
            )

        context.server.drop_database(self.name)
        return _ddl_success()


@dataclass(frozen=True, slots=True)
class UseDatabaseExecutor(StatementExecutor):
    """切换当前库：连接成功后才更新会话状态，失败时原状态保持不变。"""

    name: str

    def execute(self, context: ExecutionContext) -> QueryResult:
        new_storage = context.server.connect(self.name)
        context.storage = new_storage
        context.current_database = self.name
        return _ddl_success()


# ---------- 构建 ----------


DdlPlan: TypeAlias = (
    LogicalCreateDatabase
    | LogicalDropDatabase
    | LogicalUseDatabase
    | LogicalCreateTable
    | LogicalDropTable
)


def build_ddl_executor(plan: DdlPlan) -> StatementExecutor:
    """把 DDL 逻辑计划转换为语句级执行器。"""
    match plan:
        case LogicalCreateDatabase():
            return CreateDatabaseExecutor(plan.name)
        case LogicalDropDatabase():
            return DropDatabaseExecutor(plan.name)
        case LogicalUseDatabase():
            return UseDatabaseExecutor(plan.name)
        case LogicalCreateTable():
            return CreateTableExecutor(plan.table, plan.columns)
        case LogicalDropTable():
            return DropTableExecutor(plan.table)
        case _:
            assert_never(plan)
