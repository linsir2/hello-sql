"""系统目录 catalog（PRD §9；D12）。

定位：B 的**私有内部记忆**，不是给别人调用的接口——describe / list_tables /
create_table / drop_table / insert 全靠它；C 只通过公开方法间接使用，A 不碰。

不变量：
- 每库恰好一份 catalog.json（本库目录下，文件名见 constants，D03）；
- 内存形态：tables: dict[表名, tuple[ColumnDef, ...]]，插入顺序 = 建表顺序；
- 持久化 JSON：{"version": 1, "tables": {表名: {"columns": [{"name", "type"}]}}}；
- 版本不符 / JSON 损坏 / 结构非法 / 文件缺失 → E_STORAGE；
- 一致性顺序（§9.4）：create_table 先建文件后注册；drop_table 先摘牌后删文件；
  catalog 是权威，孤儿表文件本期容忍（不自动清理）；
- 本文件不做 SQL 语义检查（D13），只做注册表增删查与持久化；
- 表名格式校验（E_BAD_ARG）是门面职责，本层不重复；持久化数据里的
  非法名字/列结构一律视为文件损坏（E_STORAGE）。

实现阶段：M0（本文件已实现；错误归属见模块头注释与测试）。
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Sequence

from contracts.ast import ColumnDef, SqlType
from contracts.errors import (
    E_DUP_COLUMN,
    E_STORAGE,
    E_TABLE_EXISTS,
    E_TABLE_NOT_FOUND,
    SqlError,
)
from storage.constants import (
    CATALOG_VERSION,
    JSON_COLUMNS_KEY,
    JSON_NAME_KEY,
    JSON_TABLES_KEY,
    JSON_TYPE_KEY,
    JSON_VERSION_KEY,
)


_IDENTIFIER_RE = re.compile(r"[a-z_][a-z0-9_]*\Z")


class Catalog:
    """本库 schema 的内存注册表 + 持久化。

    path   ：catalog.json 的绝对路径；
    tables ：表名 → 按建表顺序的列定义（表内永久顺序，不可变）。
    """

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.tables: dict[str, tuple[ColumnDef, ...]] = {}

    # ---- 持久化 ----

    def load(self) -> None:
        """从磁盘读入并校验；任何损坏形态都抛 E_STORAGE，绝不静默当空库。"""
        try:
            raw = self.path.read_text(encoding="utf-8")
            doc = json.loads(raw)
        except FileNotFoundError as exc:
            raise SqlError(E_STORAGE, f"catalog file missing: {self.path}") from exc
        except (OSError, json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise SqlError(E_STORAGE, f"cannot read catalog {self.path}") from exc

        if not isinstance(doc, dict):
            raise SqlError(E_STORAGE, f"corrupt catalog {self.path}: not an object")
        if doc.get(JSON_VERSION_KEY) != CATALOG_VERSION:
            raise SqlError(
                E_STORAGE, f"corrupt catalog {self.path}: unsupported version"
            )

        raw_tables = doc.get(JSON_TABLES_KEY)
        if not isinstance(raw_tables, dict):
            raise SqlError(E_STORAGE, f"corrupt catalog {self.path}: tables not an object")

        loaded: dict[str, tuple[ColumnDef, ...]] = {}
        for table_name, raw_meta in raw_tables.items():
            self._check_stored_name(table_name)
            if not isinstance(raw_meta, dict):
                raise SqlError(
                    E_STORAGE,
                    f"corrupt catalog {self.path}: table {table_name!r} meta invalid",
                )
            raw_columns = raw_meta.get(JSON_COLUMNS_KEY)
            if not isinstance(raw_columns, list) or not raw_columns:
                raise SqlError(
                    E_STORAGE,
                    f"corrupt catalog {self.path}: table {table_name!r} has no columns",
                )
            columns: list[ColumnDef] = []
            seen: set[str] = set()
            for item in raw_columns:
                if not isinstance(item, dict):
                    raise SqlError(
                        E_STORAGE,
                        f"corrupt catalog {self.path}: column entry invalid",
                    )
                try:
                    column_name = item[JSON_NAME_KEY]
                    type_name = item[JSON_TYPE_KEY]
                except KeyError as exc:
                    raise SqlError(
                        E_STORAGE,
                        f"corrupt catalog {self.path}: column field missing",
                    ) from exc
                if not isinstance(column_name, str):
                    raise SqlError(
                        E_STORAGE,
                        f"corrupt catalog {self.path}: column name not a string",
                    )
                self._check_stored_name(column_name)
                if column_name in seen:
                    raise SqlError(
                        E_STORAGE,
                        f"corrupt catalog {self.path}: duplicate column {column_name!r}",
                    )
                seen.add(column_name)
                try:
                    sql_type = SqlType(type_name)
                except ValueError as exc:
                    raise SqlError(
                        E_STORAGE,
                        f"corrupt catalog {self.path}: unknown type {type_name!r}",
                    ) from exc
                columns.append(ColumnDef(column_name, sql_type))
            loaded[table_name] = tuple(columns)

        self.tables = loaded

    def save(self) -> None:
        """内存注册表原子写回：先写临时文件再 os.replace（防半份 JSON）。"""
        payload = {
            JSON_VERSION_KEY: CATALOG_VERSION,
            JSON_TABLES_KEY: {
                name: {
                    JSON_COLUMNS_KEY: [
                        {JSON_NAME_KEY: c.name, JSON_TYPE_KEY: c.type.value}
                        for c in columns
                    ]
                }
                for name, columns in self.tables.items()
            },
        }
        tmp_path = self.path.with_suffix(self.path.suffix + ".tmp")
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp_path.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            os.replace(tmp_path, self.path)
        except OSError as exc:
            try:
                tmp_path.unlink(missing_ok=True)
            except OSError:
                pass
            raise SqlError(E_STORAGE, f"cannot write catalog {self.path}") from exc

    # ---- 注册表增删查 ----

    def register(self, name: str, columns: Sequence[ColumnDef]) -> None:
        """登记一张新表。表已存在 / 空列 / 重复列分别抛契约错误码。"""
        if name in self.tables:
            raise SqlError(E_TABLE_EXISTS, f"table already exists: {name}")
        if not columns:
            raise SqlError(E_DUP_COLUMN, f"table {name!r} has no columns")
        seen: set[str] = set()
        for column in columns:
            if column.name in seen:
                raise SqlError(
                    E_DUP_COLUMN,
                    f"table {name!r} has duplicate column {column.name!r}",
                )
            seen.add(column.name)
        self.tables[name] = tuple(columns)

    def unregister(self, name: str) -> None:
        """注销一张表；表不存在抛 E_TABLE_NOT_FOUND。"""
        if name not in self.tables:
            raise SqlError(E_TABLE_NOT_FOUND, f"table not found: {name}")
        del self.tables[name]

    def get(self, name: str) -> tuple[ColumnDef, ...]:
        """查表结构（表不存在抛 E_TABLE_NOT_FOUND）。"""
        try:
            return self.tables[name]
        except KeyError as exc:
            raise SqlError(E_TABLE_NOT_FOUND, f"table not found: {name}") from exc

    def names(self) -> list[str]:
        """返回全部表名（稳定排序，契约不承诺顺序）。"""
        return sorted(self.tables)

    # ---- 内部 ----

    @staticmethod
    def _check_stored_name(name: str) -> None:
        """持久化数据里的标识符必须是合法小写标识符，否则视为文件损坏。"""
        if not isinstance(name, str) or not _IDENTIFIER_RE.fullmatch(name):
            raise SqlError(E_STORAGE, f"corrupt catalog: invalid stored name {name!r}")
