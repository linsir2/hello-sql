"""模块 B：存储层（对外入口就是本文件）。

内部设计规格：.codex/docs/storage/storage_prd.md（D01–D18）。
对外契约：docs/contract-v1.md V1.1 —— 本文件里的 DatabaseServer 与 Storage
是 B 方法契约的唯一代码真相；参数 / 返回 / 错误码以契约第 3 节为准。

内部架构（D01，单向依赖，禁止反向 import）：

    __init__.py（门面：公开 13 方法）
      ├─ catalog.py   每库 schema：内存注册表 ⇄ catalog.json（D12）
      └─ engine.py    行级执行：记录编解码、row_id、页内空间（D07/D08/D15）
             └─ pager.py   页级原语：页 0、alloc/free、页 I/O（D04/D05）
                    └─ cache.py   BufferPool：LRU、pin/dirty、flush（D09–D11/D16–D18）

红线：本目录禁止 import compiler / runner；只允许 import contracts 与标准库。
B 不认识 SQL / AST / 执行计划；C 不知道 B 的文件格式与内部结构。

错误归属（M0 边界）：
- 公开方法第一道闸：库/表名格式校验 → E_BAD_ARG（D13，先于一切存在性检查）；
- 库级：main 保护 E_DATABASE_IN_USE；目录/有效库判定决定 EXISTS/NOT_FOUND；
- “目录在但 catalog 损坏/缺失”属于存储损坏 → E_STORAGE（由 Catalog 抛）。
"""

from __future__ import annotations

import re
import shutil
from pathlib import Path
from typing import Iterator, Sequence

from contracts.ast import ColumnDef, SqlType, Value
from contracts.errors import (
    E_BAD_ARG,
    E_DATABASE_EXISTS,
    E_DATABASE_IN_USE,
    E_DATABASE_NOT_FOUND,
    E_STORAGE,
    E_TABLE_EXISTS,
    E_TYPE_MISMATCH,
    E_VALUE_COUNT,
    SqlError,
)
from contracts.storage import Row, RowId, TableInfo

from storage.cache import BufferPool
from storage.catalog import Catalog
from storage.constants import (
    CATALOG_FILE_NAME,
    DEFAULT_CACHE_CAPACITY,
    TABLE_FILE_SUFFIX,
)
from storage.engine import TableEngine
from storage.pager import create_table_file


_IDENTIFIER_RE = re.compile(r"[a-z_][a-z0-9_]*\Z")


def _validate_identifier(name: str) -> None:
    """库名/表名边界：非空、小写、匹配 [a-z_][a-z0-9_]*，否则 E_BAD_ARG。"""
    if not isinstance(name, str) or not _IDENTIFIER_RE.fullmatch(name):
        raise SqlError(E_BAD_ARG, f"invalid name: {name!r}")


_INT64_MIN = -(2**63)
_INT64_MAX = 2**63 - 1


def _normalize_values(
    columns: Sequence[ColumnDef], values: Sequence[Value]
) -> tuple[Value, ...]:
    """公开方法的值边界检查（§3.2）：个数 → 类型 → REAL 归一化为 float。

    返回归一化后的值元组，供 engine 直接编码；校验失败抛契约错误码。
    """
    if len(values) != len(columns):
        raise SqlError(
            E_VALUE_COUNT,
            f"expected {len(columns)} values, got {len(values)}",
        )
    normalized: list[Value] = []
    for column, value in zip(columns, values):
        if column.type is SqlType.INT:
            if isinstance(value, bool) or type(value) is not int:
                raise SqlError(E_TYPE_MISMATCH, f"INT column {column.name!r} got {value!r}")
            if not _INT64_MIN <= value <= _INT64_MAX:
                raise SqlError(
                    E_TYPE_MISMATCH,
                    f"INT column {column.name!r} out of 64-bit range: {value}",
                )
            normalized.append(value)
        elif column.type is SqlType.REAL:
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise SqlError(
                    E_TYPE_MISMATCH, f"REAL column {column.name!r} got {value!r}"
                )
            normalized.append(float(value))
        else:  # SqlType.TEXT
            if type(value) is not str:
                raise SqlError(
                    E_TYPE_MISMATCH, f"TEXT column {column.name!r} got {value!r}"
                )
            normalized.append(value)
    return tuple(normalized)


class DatabaseServer:
    """库层：管理 data_dir 下的全部数据库，并持有共享 BufferPool（D09）。

    不变量：
    - 构造时创建 data_dir，并自动创建默认库 main（永存、不可删）；
    - list_databases() 恒包含 main；
    - 每个库 = data_dir/<库名>/ 目录 + catalog.json（D03/D12）；
    - 一个“有效库”= 目录存在且内含 catalog.json；
    - 同一 DatabaseServer 的所有 Storage 共享同一个 BufferPool（D09/D16）。

    M0 已实现：构造/建库/删库/列库/has/connect；表级方法在 M2 填充。
    """

    def __init__(self, data_dir: str | Path) -> None:
        """建 data_dir → BufferPool(64) → 确保 main（目录 + catalog）。"""
        self._data_dir = Path(data_dir)
        try:
            self._data_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise SqlError(
                E_STORAGE, f"cannot create data dir: {self._data_dir}"
            ) from exc
        self._pool = BufferPool(DEFAULT_CACHE_CAPACITY)

        main_dir = self._data_dir / "main"
        main_catalog = main_dir / CATALOG_FILE_NAME
        if main_dir.is_dir():
            # main 目录已存在：catalog 必须存在且可读，损坏/缺失都不得静默重建。
            if not main_catalog.is_file():
                raise SqlError(
                    E_STORAGE,
                    f"main database dir exists but catalog missing: {main_dir}",
                )
            Catalog(main_catalog).load()
        else:
            try:
                main_dir.mkdir()
            except OSError as exc:
                raise SqlError(E_STORAGE, f"cannot create main db dir: {main_dir}") from exc
            Catalog(main_catalog).save()  # 空 catalog

    # ---- 内部辅助 ----

    def _db_path(self, name: str) -> Path:
        return self._data_dir / name

    @staticmethod
    def _is_valid_db_dir(db_dir: Path) -> bool:
        """有效库 = 目录存在且内含 catalog.json（杂目录/杂文件不算库）。"""
        return db_dir.is_dir() and (db_dir / CATALOG_FILE_NAME).is_file()

    # ---- 库级公开方法 ----

    def create_database(self, name: str) -> None:
        """建库：目录 + 空 catalog；失败时尽量回滚已建目录。"""
        _validate_identifier(name)
        db_dir = self._db_path(name)
        if db_dir.is_dir():
            raise SqlError(E_DATABASE_EXISTS, f"database already exists: {name}")
        try:
            db_dir.mkdir()
        except OSError as exc:
            raise SqlError(
                E_STORAGE, f"cannot create database dir: {db_dir}"
            ) from exc
        catalog = Catalog(db_dir / CATALOG_FILE_NAME)
        try:
            catalog.save()
        except SqlError:
            # 目录建了但 catalog 没写成 → 回滚空目录，保持“库=目录+catalog”不变式。
            try:
                db_dir.rmdir()
            except OSError:
                pass
            raise

    def drop_database(self, name: str) -> None:
        """删库：级联删目录；main 由 B 拦 E_DATABASE_IN_USE。

        M0 无缓存帧；M3 起在删目录前先 discard 该库全部帧（D11）。
        """
        _validate_identifier(name)
        if name == "main":
            raise SqlError(E_DATABASE_IN_USE, "cannot drop default database main")
        db_dir = self._db_path(name)
        if not self._is_valid_db_dir(db_dir):
            raise SqlError(E_DATABASE_NOT_FOUND, f"database not found: {name}")
        try:
            self._pool.discard(db_dir)  # 删库前丢弃该库全部缓存帧（D11）
            shutil.rmtree(db_dir)
        except OSError as exc:
            raise SqlError(E_STORAGE, f"cannot drop database dir: {db_dir}") from exc

    @property
    def cache_stats(self) -> dict[str, int | float]:
        """只读命中统计快照（D18；B 内部属性，不进 13 方法契约）。"""
        return self._pool.stats

    def list_databases(self) -> list[str]:
        """返回所有有效库名（排序；契约不承诺顺序，排序只是稳定输出）。"""
        try:
            names = [
                entry.name
                for entry in self._data_dir.iterdir()
                if self._is_valid_db_dir(entry)
            ]
        except OSError as exc:
            raise SqlError(
                E_STORAGE, f"cannot list databases under {self._data_dir}"
            ) from exc
        return sorted(names)

    def has_database(self, name: str) -> bool:
        """库是否存在（先过名称边界，再查有效库目录）。"""
        _validate_identifier(name)
        return self._is_valid_db_dir(self._db_path(name))

    def connect(self, name: str) -> Storage:
        """连接库：Storage 构造时载入该库 catalog（损坏 → E_STORAGE）。"""
        _validate_identifier(name)
        db_dir = self._db_path(name)
        if not self._is_valid_db_dir(db_dir):
            raise SqlError(E_DATABASE_NOT_FOUND, f"database not found: {name}")
        return Storage(db_dir, self._pool)


class Storage:
    """表层：一个实例 = 绑定某个库的连接（D03/D12）。

    已实现：M0 库级目录 + M2 全部 8 个表级方法 + M3 缓存接入（改数据方法
    末 flush、drop 前 discard、DatabaseServer.cache_stats）。

    不变量（对外可见行为）：
    - 表级方法的 name 一律是“当前库下的小写表名”；
    - A 负责把标识符转小写，B 边界仍按 [a-z_][a-z0-9_]* 校验（D13）；
    - 每个“会改数据”的公开方法返回前，本方法涉及的脏页已 flush（D11）；
    - row_id 只在“本次运行、scan 之后、update/delete 之前”有效（契约）；
    - 每张表一个 TableEngine（含 rid→页 映射），drop_table 时丢弃（D08）。
    """

    def __init__(self, db_path: Path, pool: BufferPool) -> None:
        """绑定库目录与共享池，并载入本库 catalog（缺失/损坏 → E_STORAGE）。"""
        self._db_path = Path(db_path)
        self._pool = pool
        self._catalog = Catalog(self._db_path / CATALOG_FILE_NAME)
        self._catalog.load()
        self._engines: dict[str, TableEngine] = {}

    # ---- 内部辅助 ----

    def _table_file_path(self, name: str) -> Path:
        return self._db_path / f"{name}{TABLE_FILE_SUFFIX}"

    def _engine_for(self, name: str, columns: Sequence[ColumnDef]) -> TableEngine:
        """惰性取得/创建该表的引擎；drop_table 会把它从字典移除。"""
        engine = self._engines.get(name)
        if engine is None:
            engine = TableEngine(self._table_file_path(name), columns, self._pool)
            self._engines[name] = engine
        return engine

    def create_table(self, name: str, columns: Sequence[ColumnDef]) -> None:
        """建表：E_BAD_ARG → 存在性预检 → 建文件+页0 → register → save（§9.4）。

        存在性预检必须在建文件之前，防止覆盖既有表数据；
        中途失败留下的孤儿表文件本期容忍（catalog 是权威）。
        """
        _validate_identifier(name)
        if name in self._catalog.tables:
            raise SqlError(E_TABLE_EXISTS, f"table already exists: {name}")
        create_table_file(self._table_file_path(name))
        self._catalog.register(name, columns)  # 空列/重复列 → E_DUP_COLUMN
        try:
            self._catalog.save()
        except SqlError:
            self._catalog.tables.pop(name, None)  # 回滚内存，保持与磁盘一致
            raise

    def drop_table(self, name: str) -> None:
        """删表：摘牌并 save → 删表文件；目录先摘牌杜绝“有目录没文件”（§9.4）。"""
        _validate_identifier(name)
        columns = self._catalog.get(name)  # 缺表 → E_TABLE_NOT_FOUND
        self._engines.pop(name, None)
        self._catalog.unregister(name)
        try:
            self._catalog.save()
        except SqlError:
            self._catalog.tables[name] = columns  # 回滚内存注册
            raise
        self._pool.discard(self._table_file_path(name))  # 删文件前丢帧（D11）
        try:
            self._table_file_path(name).unlink()
        except FileNotFoundError:
            pass  # 文件缺失视为可清理孤儿，drop 成功
        except OSError as exc:
            raise SqlError(
                E_STORAGE, f"cannot delete table file: {self._table_file_path(name)}"
            ) from exc

    def list_tables(self) -> list[str]:
        """只读 catalog，返回本库表名（排序稳定，契约不承诺顺序）。"""
        return self._catalog.names()

    def describe(self, name: str) -> TableInfo:
        """只读 catalog 返回 TableInfo（C 语义检查的唯一入口，§9.1）。"""
        _validate_identifier(name)
        return TableInfo(name=name, columns=self._catalog.get(name))

    def insert(self, name: str, values: Sequence[Value]) -> RowId:
        """追加一行：边界校验 → engine 落页 → 返回新 row_id（§5.3）。"""
        _validate_identifier(name)
        columns = self._catalog.get(name)
        normalized = _normalize_values(columns, values)
        engine = self._engine_for(name, columns)
        row_id = engine.insert(normalized)
        self._pool.flush(self._table_file_path(name))  # 方法末 flush（D11）
        return row_id

    def scan(self, name: str) -> Iterator[Row]:
        """整表行迭代器：调用时立刻校验；行错误在迭代时抛（§10）。"""
        _validate_identifier(name)
        columns = self._catalog.get(name)
        engine = self._engine_for(name, columns)
        return engine.scan()

    def update_row(self, name: str, row_id: RowId, values: Sequence[Value]) -> None:
        """整行替换：值边界检查 → engine 定位并更新（D08/D15）。"""
        _validate_identifier(name)
        columns = self._catalog.get(name)
        normalized = _normalize_values(columns, values)
        engine = self._engine_for(name, columns)
        engine.update(row_id, normalized)
        self._pool.flush(self._table_file_path(name))  # 方法末 flush（D11）

    def delete_row(self, name: str, row_id: RowId) -> None:
        """删除一行：engine 定位 → 移除槽 → 页内紧凑（D15）。"""
        _validate_identifier(name)
        columns = self._catalog.get(name)
        engine = self._engine_for(name, columns)
        engine.delete(row_id)
        self._pool.flush(self._table_file_path(name))  # 方法末 flush（D11）
