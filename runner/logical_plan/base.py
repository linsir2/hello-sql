from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass

from contracts.ast import SqlType
from contracts.errors import E_COLUMN_NOT_FOUND, SqlError


@dataclass(frozen=True, slots=True)
class LogicalColumn:
    """绑定后的列引用：表名 + 列名 + 行元组位置 + SQL 类型。

    字段语义：
    - table：列所属表；
    - name：列名；
    - index：该列在当前输入行值元组中的零基位置（注意不是数据库表中的列所在位置，因为表的列位置经过算子计算后会发生改变）；
    - type：contracts.ast.SqlType。
    """

    table: str
    name: str
    index: int
    type: SqlType


@dataclass(frozen=True, slots=True)
class LogicalSchema:
    """关系节点输出 Schema：SQL 可见的列集合，按序排列。
    """

    columns: tuple[LogicalColumn, ...]

    def column(self, name: str) -> LogicalColumn:
        """按名称解析列；找不到时抛 E_COLUMN_NOT_FOUND。
        """
        for col in self.columns:
            if col.name == name:
                return col
        raise SqlError(E_COLUMN_NOT_FOUND, f"column not found: {name}")


# DDL / DML 节点共享的空输出 Schema。
EMPTY_SCHEMA = LogicalSchema(())


class LogicalPlan(ABC):
    """LogicalPlan 抽象基类。

    - children：子节点元组（叶子为 ()）；
    - output_schema：SQL 可见输出 Schema。
    """

    @property
    @abstractmethod
    def children(self) -> tuple[LogicalPlan, ...]:
        raise NotImplementedError

    @property
    @abstractmethod
    def output_schema(self) -> LogicalSchema:
        raise NotImplementedError
