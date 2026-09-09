"""SQL 语句运行入口与交互式命令行。"""

from __future__ import annotations

from collections.abc import Callable

from contracts.ast import Statement
from contracts.errors import SqlError
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

    def repl(self) -> None:
        """启动交互式命令行；EOF 时正常退出。"""
        while True:
            try:
                sql = input("sql> ")
            except EOFError:
                return

            if not sql.strip():
                continue

            try:
                result = self.execute(sql)
            except SqlError as error:
                print(f"[{error.code}] {error.message}")
                continue

            self._print_result(result)

    @staticmethod
    def _print_result(result: QueryResult) -> None:
        """以简单的制表符格式展示 QueryResult，不改变结果对象。"""
        if result.columns is not None and result.rows is not None:
            print("\t".join(result.columns))
            for row in result.rows:
                print("\t".join(str(value) for value in row))
            return

        print(f"{result.affected_rows} row(s) affected")
