from __future__ import annotations

from abc import ABC
from dataclasses import dataclass
from enum import Enum
from typing import Literal as TyLiteral, TypeAlias

from contracts.ast import And, Column, Cmp, Expr, Literal, SqlType, Value
from contracts.errors import E_TYPE_MISMATCH, SqlError

from runner.logical_plan.base import LogicalColumn, LogicalSchema


# ---------- 操作符 ----------


class ComparisonOp(Enum):
    EQ = "="
    NE = "<>"
    LT = "<"
    LE = "<="
    GT = ">"
    GE = ">="


class ArithOp(Enum):
    ADD = "+"
    SUB = "-"
    MUL = "*"
    DIV = "/"
    MOD = "%"


class LogicOp(Enum):
    AND = "AND"
    OR = "OR"


# ---------- 类型推导标记 ----------

# 布尔结果在 bound 内部的标记。契约 SqlType 只有 INT/TEXT/REAL，
# BOOLEAN 只用于 deduce_type 的返回值，不落字段、不流向存储层与投影。
BOOLEAN: TyLiteral["BOOLEAN"] = "BOOLEAN"

TypeKind: TypeAlias = SqlType | TyLiteral["BOOLEAN"]


# ---------- 节点 ----------


class BoundExpr(ABC):
    """所有绑定表达式的基类。"""


@dataclass(frozen=True, slots=True)
class BoundColumnRef(BoundExpr):
    """列引用：已绑定表名、列名、行位置与类型。"""

    column: LogicalColumn


@dataclass(frozen=True, slots=True)
class BoundLiteral(BoundExpr):
    """字面量：值与其规范化后的 SQL 类型。"""

    value: Value
    type: SqlType


@dataclass(frozen=True, slots=True)
class BoundComparison(BoundExpr):
    """比较：两侧均为任意 BoundExpr（当前实际输入仅 列 op 字面量）。"""

    left: BoundExpr
    op: ComparisonOp
    right: BoundExpr


@dataclass(frozen=True, slots=True)
class BoundLogical(BoundExpr):
    """逻辑连接：AND/OR。terms 非空；允许嵌套 BoundLogical（当前仅 AND 可达）。"""

    op: LogicOp
    terms: tuple[BoundExpr, ...]


@dataclass(frozen=True, slots=True)
class BoundUnaryNot(BoundExpr):
    """TODO：逻辑非：预留节点，等待 AST 契约升级后接入，本版无输入。"""

    operand: BoundExpr


@dataclass(frozen=True, slots=True)
class BoundArith(BoundExpr):
    """TODO：算术：预留节点，等待 AST 契约升级后接入，本版无输入。"""

    left: BoundExpr
    op: ArithOp
    right: BoundExpr


@dataclass(frozen=True, slots=True)
class BoundCast(BoundExpr):
    """TODO：类型转换：隐式 CAST（INT->REAL）当前可达，显式 CAST 预留。"""

    expr: BoundExpr
    target: SqlType


@dataclass(frozen=True, slots=True)
class BoundAssignment:
    """TODO：UPDATE SET 的一项赋值：绑定后的目标列 + 绑定后的值（V1 值恒为字面量）。"""

    column: LogicalColumn
    value: BoundLiteral


# ---------- 类型推导 ----------


def deduce_type(expr: BoundExpr) -> TypeKind:
    """推导表达式的结果类型。布尔结果返回内部标记 BOOLEAN（不是 SqlType）。"""
    match expr:
        case BoundColumnRef():
            return expr.column.type
        case BoundLiteral():
            return expr.type
        case BoundComparison() | BoundLogical() | BoundUnaryNot():
            return BOOLEAN
        case BoundArith():
            # 算术左右在绑定期已协调为同型（INT 或 REAL），任取一侧即可
            return deduce_type(expr.left)
        case BoundCast():
            return expr.target


def _type_name(kind: TypeKind) -> str:
    """类型名（错误消息用）。"""
    return kind.value if isinstance(kind, SqlType) else kind


def _is_numeric(kind: TypeKind) -> bool:
    return kind is SqlType.INT or kind is SqlType.REAL


# ---------- 字面量绑定 ----------


def bind_literal(value: Value) -> BoundLiteral:
    """TODO：无目标类型上下文时，按 Python 值自然推断字面量类型（bool 明确拒绝）。"""
    if isinstance(value, bool):
        # TODO：BOOLEAN 类型在 V1 暂时不支持，后续需扩展
        raise SqlError(E_TYPE_MISMATCH, "bool is not a valid INT/REAL value")
    if isinstance(value, int):
        return BoundLiteral(value, SqlType.INT)
    if isinstance(value, float):
        return BoundLiteral(value, SqlType.REAL)
    if isinstance(value, str):
        return BoundLiteral(value, SqlType.TEXT)
    # TODO：后续扩展对其他 Python 类型的支持
    raise SqlError(E_TYPE_MISMATCH, f"unsupported literal: {value!r}")


def normalize_literal(value: Value, target: SqlType) -> BoundLiteral:
    """按目标类型校验并规范化字面量（INSERT/UPDATE 值与比较字面量通用）。

    V1 规则表：INT 收 int（拒绝 bool）；TEXT 收 str；REAL 收 int/float 并
    存 float。
    """
    # TODO：BOOLEAN 类型在 V1 暂时不支持，后续需扩展
    if isinstance(value, bool):
        raise SqlError(E_TYPE_MISMATCH, f"bool is not a valid {target.value} value")
    if target == SqlType.INT:
        if isinstance(value, int):
            return BoundLiteral(value, target)
    elif target == SqlType.TEXT:
        if isinstance(value, str):
            return BoundLiteral(value, target)
    elif target == SqlType.REAL:
        if isinstance(value, (int, float)):
            return BoundLiteral(float(value), target)
    raise SqlError(
        E_TYPE_MISMATCH, f"value {value!r} is not a valid {target.value} value"
    )


# ---------- 类型协调（隐式转换） ----------


def _coerce_to(expr: BoundExpr, target: SqlType) -> BoundExpr:
    """把表达式转换为 target 类型：字面量直接重塑，其余表达式插 BoundCast。

    ## Rules：
    - 字面量重塑**不需要 CAST**，直接在 Python 运行时完成类型转换。
    - 非字面量表达式需要插入 **BoundCast** 节点以完成类型转换。

    ## Example：
    - WHERE age > 18，age 为 REAL 而 18 为 INT，则 INT 字面量会被直接提升为 REAL，不需要经过 CAST。
    - WHERE price > 18.5，price 为 INT 而 18.5 为 REAL，则 price 非字面量会通过 CAST 提升为 REAL。
    """
    if isinstance(expr, BoundLiteral):
        return BoundLiteral(cast_value(expr.value, target), target)
    return BoundCast(expr, target)


def coerce(left: BoundExpr, right: BoundExpr) -> tuple[BoundExpr, BoundExpr]:
    """比较两侧类型，在必要时进行类型转换，保证两侧类型一致。

    TODO：目前仅支持 INT 与 REAL 的数值类型协调。
    ## Rules：
    - 同型：原样返回（TEXT/TEXT、INT/INT 与 BOOLEAN/BOOLEAN）；
    - 一侧 INT 一侧 REAL：INT 侧提升为 REAL（数值提升）；
    - 其余组合：E_TYPE_MISMATCH（如 TEXT 与数值比较）。
    """
    lt, rt = deduce_type(left), deduce_type(right)
    if lt == rt:
        return left, right
    if _is_numeric(lt) and _is_numeric(rt):
        # 数值提升：INT -> REAL
        if lt is SqlType.REAL:
            return left, _coerce_to(right, SqlType.REAL)
        return _coerce_to(left, SqlType.REAL), right
    raise SqlError(E_TYPE_MISMATCH, f"type mismatch: {_type_name(lt)} vs {_type_name(rt)}")


# ---------- AST 表达式绑定 ----------


def bind_expr(node: Expr, schema: LogicalSchema) -> BoundExpr:
    """把 AST 表达式绑定到输入 Schema，完成列解析与类型协调。

    当前可达分支：Column / Literal / Cmp（列 op 字面量）/ And（展平）。
    TODO：OR / NOT / 算术 / 显式 CAST 的分支等待 AST 契约升级后在此接入；
    到达即抛 E_TYPE_MISMATCH，不作静默降级。
    """
    match node:
        case Column():
            return BoundColumnRef(schema.column(node.name))
        case Literal():
            return bind_literal(node.value)
        case Cmp():
            left = bind_expr(node.left, schema)
            right = bind_expr(node.right, schema)
            left, right = coerce(left, right)
            return BoundComparison(left, ComparisonOp(node.op), right)
        case And():
            terms = [bind_expr(leaf, schema) for leaf in _flatten_and(node)]
            return BoundLogical(LogicOp.AND, tuple(terms))
        case _:
            # TODO：预留分支未接入。
            raise SqlError(
                E_TYPE_MISMATCH, f"unsupported expression: {type(node).__name__}"
            )


def bind_conjunction(node: Expr, schema: LogicalSchema) -> BoundLogical:
    """WHERE 绑定入口"""
    bound = bind_expr(node, schema)
    if isinstance(bound, BoundLogical):
        return bound
    # 如果绑定结果不是 BoundLogical，则说明为单层条件（不含 AND/OR 组合），也需要包一层 BoundLogical，且为 AND 类型。
    return BoundLogical(LogicOp.AND, (bound,))


def _flatten_and(node: Expr) -> list[Expr]:
    """把 AST 的 And 二叉树展平为叶子列表，保持从左到右顺序。"""
    if isinstance(node, And):
        return _flatten_and(node.left) + _flatten_and(node.right)
    return [node]


# ---------- 求值 ----------


def eval_expr(expr: BoundExpr, row: tuple[Value, ...]) -> Value:
    """对一行求值。行元组为 SQL 可见值（不含 row_id），列已绑定 index。

    无 NULL：比较/逻辑为两值逻辑，结果统一为 Python bool（bool 是 int
    子类，兼容 Value 类型标注）；V3 引入 NULL 时再改为 Kleene 三值逻辑。
    """
    match expr:
        case BoundColumnRef():
            return row[expr.column.index]
        case BoundLiteral():
            return expr.value
        case BoundComparison():
            return cmp_eval(
                expr.op, eval_expr(expr.left, row), eval_expr(expr.right, row)
            )
        case BoundLogical(op=LogicOp.AND):
            # 短路：遇 False 立即返回
            for term in expr.terms:
                if not eval_expr(term, row):
                    return False
            return True
        case BoundLogical(op=LogicOp.OR):
            # TODO：短路：遇 True 立即返回（当前无输入，等契约升级）
            for term in expr.terms:
                if eval_expr(term, row):
                    return True
            return False
        case BoundUnaryNot():
            return not eval_expr(expr.operand, row)
        case BoundArith():
            return arith_eval(
                expr.op, eval_expr(expr.left, row), eval_expr(expr.right, row)
            )
        case BoundCast():
            return cast_value(eval_expr(expr.expr, row), expr.target)


def cmp_eval(op: ComparisonOp, left: Value, right: Value) -> bool:
    """比较求值：绑定已保证两侧类型一致且可比较（如 TEXT 不参与数值比较）。"""
    match op:
        case ComparisonOp.EQ:
            return left == right
        case ComparisonOp.NE:
            return left != right
        case ComparisonOp.LT:
            return left < right
        case ComparisonOp.LE:
            return left <= right
        case ComparisonOp.GT:
            return left > right
        case ComparisonOp.GE:
            return left >= right


def arith_eval(op: ArithOp, left: Value, right: Value) -> Value:
    """算术求值（预留：当前无 AST 输入）。

    除法语义与 SQL 一致：INT/INT 截断为 INT（5 / 2 = 2，负数向零截断），
    任一侧为 REAL 则结果 REAL。
    """
    match op:
        case ArithOp.ADD:
            return left + right
        case ArithOp.SUB:
            return left - right
        case ArithOp.MUL:
            return left * right
        case ArithOp.DIV:
            if isinstance(left, int) and isinstance(right, int):
                return int(left / right)  # 向零截断，避免//的向下取整语义
            return left / right
        case ArithOp.MOD:
            return left % right


def cast_value(value: Value, target: SqlType) -> Value:
    """类型转换：当前隐式 CAST 仅 INT->REAL，其余目标是通用实现。

    float -> INT 取截断；INT/REAL -> TEXT 用 Python 字符串形式（显式
    CAST 接入时再按 SQL 文本规格调整）。
    """
    if target == SqlType.INT:
        return int(value)
    if target == SqlType.REAL:
        return float(value)
    if target == SqlType.TEXT:
        return str(value)
    raise SqlError(E_TYPE_MISMATCH, f"cannot cast to {target.value}")
