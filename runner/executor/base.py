from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Iterator

from contracts.result import QueryResult
from runner.executor.context import ExecutionContext
from runner.executor.row import ExecRow
from runner.logical_plan.base import LogicalSchema


class RowExecutor(ABC):
    """产生行的内部算子。

    算子之间通过 rows() 组成拉取式流水线：上层算子迭代下层算子，逐行获取
    结果，中间算子不物化数据。
    """

    @property
    @abstractmethod
    def output_schema(self) -> LogicalSchema:
        """本算子输出行的 Schema，列位置与 ExecRow.values 一一对应。"""
        raise NotImplementedError

    @abstractmethod
    def rows(self, context: ExecutionContext) -> Iterator[ExecRow]:
        """惰性产出结果行；调用方负责迭代完毕或提前关闭。"""
        raise NotImplementedError


class StatementExecutor(ABC):
    """执行一条完整语句并返回对外结果。

    DQL、DML、DDL 的根执行器统一实现该接口，由 Runner 调用。
    """

    @abstractmethod
    def execute(self, context: ExecutionContext) -> QueryResult:
        raise NotImplementedError
