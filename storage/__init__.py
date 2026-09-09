"""模块 B：存储层（对外入口就是本文件）。

内部设计规格：.codex/docs/storage/storage_prd.md（D01–D18）。
对外契约：docs/contract-v1.md V1.1 —— 本文件里的 DatabaseServer 与 Storage
是 B 方法契约的唯一代码真相；参数 / 返回 / 错误码以契约第 3 节为准。

内部架构（D01/D12/A 方案，单向依赖，禁止反向 import）：

    DatabaseServer（库级：目录 + 每库共享 catalog 注册表 + 每表共享 engine）
      │    Storage 是 connect() 返回的薄视图，不持有独立内存快照
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

import math
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


def _materialize_columns(columns: Sequence[ColumnDef]) -> tuple[ColumnDef, ...]:
    """把 columns 物化成元组：调用方传一次性迭代器也能正确取两遍（防御）。"""
    try:
        return tuple(columns)
    except TypeError:
        raise SqlError(E_BAD_ARG, "columns must be an iterable of ColumnDef") from None


def _validate_columns(columns: Sequence[ColumnDef]) -> None:
    """列定义自身边界（拍板：一律 E_BAD_ARG，不新增错误码）。

    列名与库名/表名同属标识符，格式非法归 E_BAD_ARG；type 不是 SqlType
    属于“参数本身非法”，不是入库值放错列（不能用 E_TYPE_MISMATCH）。
    空列/重复列仍由 Catalog.register 抛 E_DUP_COLUMN（契约固定）。
    """
    for column in columns:
        if not isinstance(column, ColumnDef):
            raise SqlError(
                E_BAD_ARG,
                f"invalid column definition: {type(column).__name__}",
            )
        if not isinstance(column.name, str) or not _IDENTIFIER_RE.fullmatch(
            column.name
        ):
            raise SqlError(E_BAD_ARG, f"invalid column name: {column.name!r}")
        if not isinstance(column.type, SqlType):
            raise SqlError(
                E_BAD_ARG,
                f"invalid type for column {column.name!r}: "
                f"{type(column.type).__name__}",
            )


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
                    f"INT column {column.name!r} out of 64-bit range",
                )
            normalized.append(value)
        elif column.type is SqlType.REAL:
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise SqlError(
                    E_TYPE_MISMATCH, f"REAL column {column.name!r} got {value!r}"
                )
            try:
                real_value = float(value)
            except OverflowError:
                # 巨 int（如 2**1024）转 double 会抛 OverflowError；
                # 表示不了的数值按范围不符拒绝，不许把裸异常漏给调用方。
                raise SqlError(
                    E_TYPE_MISMATCH,
                    f"REAL column {column.name!r} out of double range",
                ) from None
            if not math.isfinite(real_value):
                raise SqlError(
                    E_TYPE_MISMATCH,
                    f"REAL column {column.name!r} must be finite, got {value!r}",
                )
            normalized.append(real_value)
        else:  # SqlType.TEXT
            if type(value) is not str:
                raise SqlError(
                    E_TYPE_MISMATCH, f"TEXT column {column.name!r} got {value!r}"
                )
            try:
                value.encode("utf-8")
            except UnicodeEncodeError:
                raise SqlError(
                    E_TYPE_MISMATCH,
                    f"TEXT column {column.name!r} is not utf-8 encodable",
                ) from None
            normalized.append(value)
    return tuple(normalized)


class DatabaseServer:
    """库层：管理 data_dir 下的全部数据库，并持有进程级共享状态（D09/A 方案）。

    不变量：
    - 构造时创建 data_dir，并自动创建默认库 main（永存、不可删）；
    - list_databases() 恒包含 main；
    - 每个库 = data_dir/<库名>/ 目录 + catalog.json（D03/D12）；
    - 一个“有效库”= 目录存在且内含 catalog.json；
    - 一个库只保留一份内存 Catalog、一张表只保留一份 TableEngine：
      connect() 返回的 Storage 是薄视图，不持有独立快照（方案 A 拍板）；
    - 同一 DatabaseServer 的所有 Storage 共享 BufferPool（D09/D16）。
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
        self._catalogs: dict[Path, Catalog] = {}  # 库目录(绝对) → Catalog
        self._engines: dict[tuple[Path, str], TableEngine] = {}  # (库,表) → engine

        main_dir = self._data_dir / "main"
        main_catalog = main_dir / CATALOG_FILE_NAME
        if main_dir.is_dir():
            # main 目录已存在：catalog 必须存在且可读，损坏/缺失都不得静默重建。
            if not main_catalog.is_file():
                raise SqlError(
                    E_STORAGE,
                    f"main database dir exists but catalog missing: {main_dir}",
                )
            catalog = Catalog(main_catalog)
            catalog.load()
        else:
            try:
                main_dir.mkdir()
            except OSError as exc:
                raise SqlError(E_STORAGE, f"cannot create main db dir: {main_dir}") from exc
            catalog = Catalog(main_catalog)
            catalog.save()  # 空 catalog
        self._catalogs[main_dir.absolute()] = catalog

    # ---- 内部辅助 ----

    def _db_path(self, name: str) -> Path:
        return self._data_dir / name

    @staticmethod
    def _is_valid_db_dir(db_dir: Path) -> bool:
        """有效库 = 目录存在且内含 catalog.json（杂目录/杂文件不算库）。"""
        return db_dir.is_dir() and (db_dir / CATALOG_FILE_NAME).is_file()

    def _get_catalog(self, db_dir: Path) -> Catalog:
        """返回该库的内存 Catalog；第一次接触时从磁盘载入并缓存。"""
        db_key = db_dir.absolute()
        catalog = self._catalogs.get(db_key)
        if catalog is None:
            catalog = Catalog(db_dir / CATALOG_FILE_NAME)
            catalog.load()
            self._catalogs[db_key] = catalog
        return catalog

    def _purge_db_state(self, db_dir: Path) -> None:
        """删库时摘掉该库的 catalog 与全部 engine（共享注册表保持一致）。"""
        db_key = db_dir.absolute()
        self._catalogs.pop(db_key, None)
        stale_engines = [key for key in self._engines if key[0] == db_key]
        for key in stale_engines:
            del self._engines[key]

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
        self._catalogs[db_dir.absolute()] = catalog

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
            self._purge_db_state(db_dir)  # 摘掉共享 catalog/engine，旧句柄随之失效
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
        """连接库：校验磁盘 catalog 仍合法，返回共享该库内存真相的薄 Storage。"""
        _validate_identifier(name)
        db_dir = self._db_path(name)
        if not self._is_valid_db_dir(db_dir):
            raise SqlError(E_DATABASE_NOT_FOUND, f"database not found: {name}")
        # 每次 connect 都重新解析一遍 catalog.json：外部损坏不许被内存缓存
        # 掩盖（M0 验收）；解析结果不替换共享对象——本 server 内存真相优先。
        Catalog(db_dir / CATALOG_FILE_NAME).load()
        catalog = self._get_catalog(db_dir)
        return Storage(self, db_dir.absolute(), catalog)


class Storage:
    """表层：一个实例 = 绑定某个库的薄连接视图（方案 A 拍板）。

    catalog 与 TableEngine 的内存真相都在 DatabaseServer 的共享注册表里，
    connect() 只是取引用；同库多句柄看到的是同一份状态，drop/重建后
    新 schema 立即可见，旧 rid→页 映射不会残留在别的句柄上。
    drop_database 会把共享状态摘除，旧句柄再调用任何方法都会 E_STORAGE。

    不变量（对外可见行为）：
    - 表级方法的 name 一律是“当前库下的小写表名”；
    - A 负责把标识符转小写，B 边界仍按 [a-z_][a-z0-9_]* 校验（D13）；
    - create_table 的列定义自身非法 → E_BAD_ARG（先于建文件，不留孤儿）；
    - 每个“会改数据”的公开方法返回前，本方法涉及的脏页已 flush（D11）；
    - row_id 只在“本次运行、scan 之后、update/delete 之前”有效（契约）；
    - 每张表一个共享 TableEngine（含 rid→页 映射），drop_table 时丢弃（D08）。
    """

    def __init__(self, server: DatabaseServer, db_key: Path, catalog: Catalog) -> None:
        """绑定所属 DatabaseServer、库目录（绝对）与共享 Catalog 引用。"""
        self._server = server
        self._db_path = db_key
        self._catalog = catalog

    @property
    def _pool(self) -> BufferPool:
        """缓存池始终取 server 当前的（库对象共享，不另存引用）。"""
        return self._server._pool

    # ---- 内部辅助 ----

    def _live_catalog(self) -> Catalog:
        """本句柄绑定的 Catalog 必须仍是 server 注册表里的当前真相。"""
        current = self._server._catalogs.get(self._db_path)
        if current is not self._catalog:
            raise SqlError(
                E_STORAGE,
                f"storage handle is stale: database {self._db_path.name} "
                "was dropped or recreated",
            )
        return current

    def _table_file_path(self, name: str) -> Path:
        return self._db_path / f"{name}{TABLE_FILE_SUFFIX}"

    def _engine_for(self, name: str, columns: Sequence[ColumnDef]) -> TableEngine:
        """惰性取得/创建该表的共享引擎；drop_table 会把它从注册表移除。"""
        engine_key = (self._db_path, name)
        engine = self._server._engines.get(engine_key)
        if engine is None:
            engine = TableEngine(self._table_file_path(name), columns, self._pool)
            self._server._engines[engine_key] = engine
        return engine

    def create_table(self, name: str, columns: Sequence[ColumnDef]) -> None:
        """建表：E_BAD_ARG（表名/列定义）→ 存在性预检 → 建文件+页0 →
        register → save（§9.4）。

        存在性预检必须在建文件之前，防止覆盖既有表数据；
        中途失败留下的孤儿表文件本期容忍（catalog 是权威）。
        """
        _validate_identifier(name)
        catalog = self._live_catalog()
        if name in catalog.tables:
            raise SqlError(E_TABLE_EXISTS, f"table already exists: {name}")
        columns = _materialize_columns(columns)
        _validate_columns(columns)
        create_table_file(self._table_file_path(name))
        catalog.register(name, columns)  # 空列/重复列 → E_DUP_COLUMN
        try:
            catalog.save()
        except SqlError:
            catalog.tables.pop(name, None)  # 回滚内存，保持与磁盘一致
            raise

    def drop_table(self, name: str) -> None:
        """删表：摘牌并 save → 删表文件；目录先摘牌杜绝“有目录没文件”（§9.4）。"""
        _validate_identifier(name)
        catalog = self._live_catalog()
        columns = catalog.get(name)  # 缺表 → E_TABLE_NOT_FOUND
        self._server._engines.pop((self._db_path, name), None)
        catalog.unregister(name)
        try:
            catalog.save()
        except SqlError:
            catalog.tables[name] = columns  # 回滚内存注册
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
        return self._live_catalog().names()

    def describe(self, name: str) -> TableInfo:
        """只读 catalog 返回 TableInfo（C 语义检查的唯一入口，§9.1）。"""
        _validate_identifier(name)
        return TableInfo(name=name, columns=self._live_catalog().get(name))

    def insert(self, name: str, values: Sequence[Value]) -> RowId:
        """追加一行：边界校验 → engine 落页 → 返回新 row_id（§5.3）。"""
        _validate_identifier(name)
        catalog = self._live_catalog()
        columns = catalog.get(name)
        normalized = _normalize_values(columns, values)
        engine = self._engine_for(name, columns)
        row_id = engine.insert(normalized)
        self._pool.flush(self._table_file_path(name))  # 方法末 flush（D11）
        return row_id

    def scan(self, name: str) -> Iterator[Row]:
        """整表行迭代器：调用时立刻校验；行错误在迭代时抛（§10）。"""
        _validate_identifier(name)
        columns = self._live_catalog().get(name)
        engine = self._engine_for(name, columns)
        return engine.scan()

    def update_row(self, name: str, row_id: RowId, values: Sequence[Value]) -> None:
        """整行替换：值边界检查 → engine 定位并更新（D08/D15）。"""
        _validate_identifier(name)
        columns = self._live_catalog().get(name)
        normalized = _normalize_values(columns, values)
        engine = self._engine_for(name, columns)
        engine.update(row_id, normalized)
        self._pool.flush(self._table_file_path(name))  # 方法末 flush（D11）

    def delete_row(self, name: str, row_id: RowId) -> None:
        """删除一行：engine 定位 → 移除槽 → 页内紧凑（D15）。"""
        _validate_identifier(name)
        columns = self._live_catalog().get(name)
        engine = self._engine_for(name, columns)
        engine.delete(row_id)
        self._pool.flush(self._table_file_path(name))  # 方法末 flush（D11）
