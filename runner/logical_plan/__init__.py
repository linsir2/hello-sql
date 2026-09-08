"""LogicalPlan 包：对外导出稳定类型（base / expressions / plans / builder）。

- base.py：LogicalColumn、LogicalSchema、LogicalPlan 基类；
- expressions.py：绑定表达式（Bound* 节点、bind_expr、求值）；
- plans.py：V1 计划节点；
- builder.py：Statement -> 计划树；
"""

from runner.logical_plan.base import (
    EMPTY_SCHEMA,
    LogicalColumn,
    LogicalPlan,
    LogicalSchema,
)
from runner.logical_plan.builder import DescribeTable, LogicalPlanBuilder
from runner.logical_plan.expressions import (
    BOOLEAN,
    ArithOp,
    BoundArith,
    BoundAssignment,
    BoundCast,
    BoundColumnRef,
    BoundComparison,
    BoundExpr,
    BoundLiteral,
    BoundLogical,
    BoundUnaryNot,
    ComparisonOp,
    LogicOp,
    TypeKind,
    arith_eval,
    bind_conjunction,
    bind_expr,
    bind_literal,
    cast_value,
    cmp_eval,
    coerce,
    deduce_type,
    eval_expr,
    normalize_literal,
)
from runner.logical_plan.plans import (
    LogicalCreateDatabase,
    LogicalCreateTable,
    LogicalDelete,
    LogicalDropDatabase,
    LogicalDropTable,
    LogicalFilter,
    LogicalInsert,
    LogicalProjection,
    LogicalScan,
    LogicalUpdate,
    LogicalUseDatabase,
)

__all__ = [
    # base
    "EMPTY_SCHEMA",
    "LogicalColumn",
    "LogicalPlan",
    "LogicalSchema",
    # builder
    "DescribeTable",
    "LogicalPlanBuilder",
    # expressions
    "BOOLEAN",
    "TypeKind",
    "ArithOp",
    "BoundArith",
    "BoundAssignment",
    "BoundCast",
    "BoundColumnRef",
    "BoundComparison",
    "BoundExpr",
    "BoundLiteral",
    "BoundLogical",
    "BoundUnaryNot",
    "ComparisonOp",
    "LogicOp",
    "arith_eval",
    "bind_conjunction",
    "bind_expr",
    "bind_literal",
    "cast_value",
    "cmp_eval",
    "coerce",
    "deduce_type",
    "eval_expr",
    "normalize_literal",
    # plans
    "LogicalCreateDatabase",
    "LogicalCreateTable",
    "LogicalDelete",
    "LogicalDropDatabase",
    "LogicalDropTable",
    "LogicalFilter",
    "LogicalInsert",
    "LogicalProjection",
    "LogicalScan",
    "LogicalUpdate",
    "LogicalUseDatabase",
]
