"""SQL 语句运行入口与交互式命令行。"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from contracts.ast import Statement
from contracts.result import QueryResult
from contracts.storage import BaseDatabaseServer, TableInfo
from runner.executor.builder import ExecutorTreeBuilder
from runner.executor.context import ExecutionContext
from runner.logical_plan.builder import LogicalPlanBuilder


DEFAULT_DATABASE = "main"
ParseSql = Callable[[str], Statement]


class Runner:
    """串联 SQL 各执行阶段，并维护单个会话状态。"""

    def __init__(
        self,
        server: BaseDatabaseServer,
        parse: ParseSql,
        current_database: str = DEFAULT_DATABASE,
    ) -> None:
        # 先完成连接；连接失败时不创建半初始化的会话上下文。
        storage = server.connect(current_database)
        self._parse = parse
        self._context = ExecutionContext(
            server=server,
            storage=storage,
            current_database=current_database,
        )
        self._logical_plan_builder = LogicalPlanBuilder(
            self._describe_current_table
        )
        self._executor_tree_builder = ExecutorTreeBuilder()

    @property
    def current_database(self) -> str:
        """返回当前会话所连接的数据库名。"""
        return self._context.current_database

    def _describe_current_table(self, table: str) -> TableInfo:
        """动态读取当前 Storage 的表结构，保证 USE 后访问新数据库。"""
        return self._context.storage.describe(table)

    def execute(self, sql: str) -> QueryResult:
        """执行一条 SQL，并原样返回执行器产生的结果。"""
        statement = self._parse(sql)
        plan = self._logical_plan_builder.build(statement)
        executor = self._executor_tree_builder.build(plan)
        return executor.execute(self._context)

    def list_databases(self) -> list[str]:
        """向终端提供库名，终端不接触存储内部结构。"""
        return self._context.server.list_databases()

    def list_tables(self) -> list[str]:
        return self._context.storage.list_tables()

    def describe_table(self, name: str) -> TableInfo:
        return self._describe_current_table(name)

    def repl(
        self, *, data_dir: Path | None = None, plain: bool = False,
        history: bool = True,
    ) -> int:
        """进入终端会话；非 TTY 自动使用纯文本，返回会话退出码。"""
        from runner.terminal.session import TerminalSession

        return TerminalSession(self, data_dir=data_dir, plain=plain, history=history).run()

    @staticmethod
    def _print_result(result: QueryResult) -> None:
        """以简单的制表符格式展示 QueryResult，不改变结果对象。"""
        from runner.terminal.render import safe_text

        if result.columns is not None and result.rows is not None:
            print("\t".join(safe_text(column) for column in result.columns))
            for row in result.rows:
                print("\t".join(safe_text(value) for value in row))
            return

        print(f"{result.affected_rows} row(s) affected")
