"""页式系统表自举模块（V2 D20/D21；M1 只做自举，M2 才接入 Catalog 权威）。

本模块只依赖 pager / engine / cache 这些既有基础设施，不反向依赖门面。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from contracts.ast import ColumnDef, SqlType

from storage.cache import BufferPool
from storage.constants import SYS_COLUMNS_FILE_NAME, SYS_TABLES_FILE_NAME
from storage.engine import TableEngine
from storage.pager import create_table_file


# 内置系统表 Schema（D20）：启动时不查询 Catalog，直接按本常量打开系统表。
SYS_TABLES_COLUMNS: tuple[ColumnDef, ...] = (
    ColumnDef("table_id", SqlType.INT),
    ColumnDef("table_name", SqlType.TEXT),
    ColumnDef("file_name", SqlType.TEXT),
)

SYS_COLUMNS_COLUMNS: tuple[ColumnDef, ...] = (
    ColumnDef("table_id", SqlType.INT),
    ColumnDef("ordinal", SqlType.INT),
    ColumnDef("column_name", SqlType.TEXT),
    ColumnDef("column_type", SqlType.TEXT),
)


@dataclass(frozen=True)
class SystemTables:
    """两张系统表的行级访问器：tables 对应 __sys_tables，columns 对应 __sys_columns。"""

    tables: TableEngine
    columns: TableEngine


def system_table_paths(db_dir: str | Path) -> tuple[Path, Path]:
    """返回 (__sys_tables 文件路径, __sys_columns 文件路径)。"""
    root = Path(db_dir)
    return root / SYS_TABLES_FILE_NAME, root / SYS_COLUMNS_FILE_NAME


def create_empty_system_catalog(db_dir: str | Path) -> None:
    """在空库目录中创建两张页式系统表文件（各自只有页 0）。"""
    for path in system_table_paths(db_dir):
        create_table_file(path)


def open_system_tables(db_dir: str | Path, pool: BufferPool) -> SystemTables:
    """按内置 Schema 打开两张系统表，返回可扫描的表引擎集合。"""
    tables_path, columns_path = system_table_paths(db_dir)
    return SystemTables(
        tables=TableEngine(tables_path, SYS_TABLES_COLUMNS, pool),
        columns=TableEngine(columns_path, SYS_COLUMNS_COLUMNS, pool),
    )
