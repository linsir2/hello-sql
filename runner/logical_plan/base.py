from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass

from contracts.ast import SqlType
from contracts.errors import (
    E_AMBIGUOUS_COLUMN,
    E_COLUMN_NOT_FOUND,
    E_DUP_TABLE_ALIAS,
    E_TABLE_QUALIFIER_NOT_FOUND,
    SqlError,
)


@dataclass(frozen=True, slots=True)
class LogicalColumn:
    """绑定后的列引用：来源表 + 限定符 + 列名 + 行元组位置 + SQL 类型。

    字段语义：
    - table：物理来源表名，来自 TableRef.name；
    - qualifier：绑定与表头使用的限定符，**别名优先**，否则等于 table；
    - name：列名；
    - index：该列在当前输入行值元组中的零基位置（注意不是数据库表中的列所在位置，因为表的列位置经过算子计算后会发生改变）；
    - type：contracts.ast.SqlType。
    """

    table: str
    qualifier: str
    name: str
    index: int
    type: SqlType

    @classmethod
    def of(
        cls,
        table: str,
        name: str,
        index: int,
        type: SqlType,
        alias: str | None = None,
    ) -> "LogicalColumn":
        """按“别名优先，否则表名”的规则构造列，集中维护限定符不变式。"""
        return cls(
            table=table,
            qualifier=alias or table,
            name=name,
            index=index,
            type=type,
        )


@dataclass(frozen=True, slots=True)
class LogicalSchema:
    """关系节点输出 Schema：SQL 可见的列集合，按序排列。

    来源范围由列上的 qualifier 自描述，不额外维护来源表列表，避免两处状态不同步。
    """

    columns: tuple[LogicalColumn, ...]

    @property
    def qualifiers(self) -> tuple[str, ...]:
        """按首次出现顺序去重后的限定符集合。"""
        return tuple(dict.fromkeys(col.qualifier for col in self.columns))

    def has_qualifier(self, qualifier: str) -> bool:
        """限定符是否属于当前 Schema 的来源范围。"""
        return any(col.qualifier == qualifier for col in self.columns)

    def resolve(self, name: str, qualifier: str | None = None) -> LogicalColumn:
        """解析列引用，检查顺序固定为“**先限定符、后列名**”。

        ## Rules:
        - 带限定符：
            - 限定符不在 Schema 中抛 E_TABLE_QUALIFIER_NOT_FOUND；
            - 限定符存在但该来源下无此列抛 E_COLUMN_NOT_FOUND；
            - 限定符存在且该来源下有此列返回该列；
        - 不带限定符：
            - 恰好一列同名返回该列；
            - 零匹配抛 E_COLUMN_NOT_FOUND；
            - 多列同名抛 E_AMBIGUOUS_COLUMN。
        """
        if qualifier is not None:
            # 不存在该限定符，抛 E_TABLE_QUALIFIER_NOT_FOUND
            if not self.has_qualifier(qualifier):
                raise SqlError(
                    E_TABLE_QUALIFIER_NOT_FOUND,
                    f"table qualifier not found: {qualifier}",
                )
            for col in self.columns:
                # 限定符存在，检查该来源下是否有匹配的列
                if col.qualifier == qualifier and col.name == name:
                    return col

            # 限定符存在但该来源下无此列，抛 E_COLUMN_NOT_FOUND
            raise SqlError(
                E_COLUMN_NOT_FOUND,
                f"column not found: {qualifier}.{name}",
            )

        # 无限定符，按列名匹配
        matched = [col for col in self.columns if col.name == name]

        # 只有一个匹配的列，直接返回
        if len(matched) == 1:
            return matched[0]

        # 没有匹配的列，抛 E_COLUMN_NOT_FOUND；
        if not matched:
            raise SqlError(E_COLUMN_NOT_FOUND, f"column not found: {name}")
        sources = ", ".join(dict.fromkeys(col.qualifier for col in matched))

        # 多列同名，抛 E_AMBIGUOUS_COLUMN
        raise SqlError(
            E_AMBIGUOUS_COLUMN,
            f"ambiguous column: {name} appears in {sources}",
        )

    def column(self, name: str) -> LogicalColumn:
        """无限定符解析的简写，等价 resolve(name, None)，供单表 DML 使用。"""
        return self.resolve(name)


def join_schema(left: LogicalSchema, right: LogicalSchema) -> LogicalSchema:
    """拼接左右子树的 Schema：左列在前、右列在后，并重新编号 index。

    限定符相交时抛 E_DUP_TABLE_ALIAS
    """
    overlap = set(left.qualifiers) & set(right.qualifiers)
    if overlap:
        raise SqlError(
            E_DUP_TABLE_ALIAS,
            f"duplicate table alias: {', '.join(sorted(overlap))}",
        )
    # 直接构造以保留 qualifier 原值：别名无法从列上反推，走 of() 会被表名覆盖。
    return LogicalSchema(
        tuple(
            LogicalColumn(
                table=col.table,
                qualifier=col.qualifier,
                name=col.name,
                index=index,
                type=col.type,
            )
            for index, col in enumerate(left.columns + right.columns)
        )
    )


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
