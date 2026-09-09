"""Executor 树统一构建入口。"""

from __future__ import annotations

from runner.executor.base import StatementExecutor
from runner.executor.ddl import build_ddl_executor
from runner.executor.dml import build_dml_executor
from runner.executor.dql import build_select_executor
from runner.logical_plan.base import LogicalPlan
from runner.logical_plan.plans import (
    LogicalCreateDatabase,
    LogicalCreateTable,
    LogicalDelete,
    LogicalDropDatabase,
    LogicalDropTable,
    LogicalInsert,
    LogicalProjection,
    LogicalUpdate,
    LogicalUseDatabase,
)


class ExecutorTreeBuilder:
    """把语句级 LogicalPlan 根节点转换为 StatementExecutor。

    本类只负责识别**根节点**所属的语句类别，具体 Executor 的构造由各执行模块负责。
    """

    def build(self, plan: LogicalPlan) -> StatementExecutor:
        """构建一棵可执行的 Executor 树。"""
        match plan:
            case LogicalProjection():
                return build_select_executor(plan)
            case LogicalInsert() | LogicalUpdate() | LogicalDelete():
                return build_dml_executor(plan)
            case (
                LogicalCreateDatabase()
                | LogicalDropDatabase()
                | LogicalUseDatabase()
                | LogicalCreateTable()
                | LogicalDropTable()
            ):
                return build_ddl_executor(plan)
            case _:
                raise TypeError(
                    "unsupported statement plan: "
                    f"{type(plan).__name__}"
                )
