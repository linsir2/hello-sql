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
"""

from __future__ import annotations

from pathlib import Path
from typing import Iterator, Sequence

from contracts.ast import ColumnDef, Value
from contracts.storage import Row, RowId, TableInfo

from storage.cache import BufferPool
from storage.constants import DEFAULT_CACHE_CAPACITY


class DatabaseServer:
    """库层：管理 data_dir 下的全部数据库，并持有共享 BufferPool（D09）。

    不变量：
    - 构造时创建 data_dir，并自动创建默认库 main（永存、不可删）；
    - list_databases() 恒包含 main；
    - 每个库 = data_dir/<库名>/ 目录，内含 catalog.json（D03/D12）；
    - 同一 DatabaseServer 的所有 Storage 共享同一个 BufferPool（D09/D16）；
    - 公开方法校验顺序：E_BAD_ARG（名称格式）→ 存在性 → 值/类型边界（D13）。
    """

    def __init__(self, data_dir: str | Path) -> None:
        """M0 骨架，尚未实现。

        计划：创建目录 → 自建 BufferPool(DEFAULT_CACHE_CAPACITY) →
        确保默认库 main 存在。签名固定，不可增加必填参数（契约 §3.1）。
        """
        raise NotImplementedError("M0：DatabaseServer.__init__")

    def create_database(self, name: str) -> None:
        """建库：创建 data_dir/<name>/ 并写入空 catalog（D12）。

        失败：E_BAD_ARG / E_DATABASE_EXISTS。成功返回 None（DDL，affected=0 由 C 组装）。
        """
        raise NotImplementedError("M0：create_database")

    def drop_database(self, name: str) -> None:
        """删库：级联删掉其中所有表；main 由 B 拦 E_DATABASE_IN_USE。

        顺序：先 discard 该库在缓存里的帧（D11），再删目录。
        失败：E_BAD_ARG / E_DATABASE_NOT_FOUND / E_DATABASE_IN_USE。
        """
        raise NotImplementedError("M0：drop_database")

    def list_databases(self) -> list[str]:
        """返回库名列表（含 main）。契约不承诺顺序；实现取稳定顺序即可。"""
        raise NotImplementedError("M0：list_databases")

    def has_database(self, name: str) -> bool:
        """库是否存在。名称非法按 B 边界抛 E_BAD_ARG。"""
        raise NotImplementedError("M0：has_database")

    def connect(self, name: str) -> Storage:
        """连接库：载入该库 catalog → 返回绑定该库的 Storage。

        失败：E_BAD_ARG / E_DATABASE_NOT_FOUND。Storage 绑定库后不再换库；
        当前库属于 C（Runner）的会话状态，B 不保存。
        """
        raise NotImplementedError("M0：connect")


class Storage:
    """表层：一个实例 = 绑定某个库的连接（D03/D12）。

    计划字段：
        self._catalog   本库 Catalog（describe/list_tables/insert 全靠它，私有）
        self._engine    行级执行入口（engine.py，M2）
        self._pool      DatabaseServer 传入的共享 BufferPool
        self._db_path   data_dir/<库名>/

    不变量（对外可见行为）：
    - 表级方法的 name 一律是“当前库下的小写表名”；
    - A 负责把标识符转小写，B 边界仍按 [a-z_][a-z0-9_]* 校验（D13）；
    - 每个“会改数据”的公开方法返回前，本方法涉及的脏页已 flush（D11）；
    - row_id 只在“本次运行、scan 之后、update/delete 之前”有效（契约）。
    """

    def __init__(self, db_path: Path, pool: BufferPool) -> None:
        """Storage 由 server.connect() 内部构造，不对外直接创建。

        M0 骨架：仅约定内部构造参数；正式实现会在此载入 catalog（M0/M2）。
        """
        raise NotImplementedError("M0：Storage.__init__")

    def create_table(self, name: str, columns: Sequence[ColumnDef]) -> None:
        """建表：engine 建表文件 + 写页 0 → catalog.register 并保存（§9.4）。

        顺序：先建文件成功，再注册 catalog（catalog 是权威，中途失败只留孤儿文件）。
        失败：E_BAD_ARG / E_TABLE_EXISTS / E_DUP_COLUMN（空列/重复列）。
        """
        raise NotImplementedError("M2：create_table")

    def drop_table(self, name: str) -> None:
        """删表：catalog.unregister + 保存 → discard 缓存帧 → 删表文件（D11）。

        失败：E_BAD_ARG / E_TABLE_NOT_FOUND。
        """
        raise NotImplementedError("M2：drop_table")

    def list_tables(self) -> list[str]:
        """返回本库表名列表（只读 catalog）。顺序不承诺，实现取稳定顺序。"""
        raise NotImplementedError("M2：list_tables")

    def describe(self, name: str) -> TableInfo:
        """表结构（只读 catalog，返回 TableInfo）。

        C 语义检查的唯一只读入口；catalog 内部结构永不外泄。
        失败：E_BAD_ARG / E_TABLE_NOT_FOUND。
        """
        raise NotImplementedError("M2：describe")

    def insert(self, name: str, values: Sequence[Value]) -> RowId:
        """追加一行，返回新 row_id。

        边界校验（D13）：表存在 → 值个数（E_VALUE_COUNT）→ 逐列类型
        （E_TYPE_MISMATCH；INT 拒 bool；REAL 收 int/float 并归一化 float）。
        写记录 → 返回前 flush（D11）。
        """
        raise NotImplementedError("M2：insert")

    def scan(self, name: str) -> Iterator[Row]:
        """整表行迭代器，顺序不承诺。

        校验在调用时立刻执行（表不存在马上抛 E_TABLE_NOT_FOUND）；
        实现按页解码成内存副本、unpin 后再逐行 yield（D17），不跨 yield 持 pin。
        """
        raise NotImplementedError("M2：scan")

    def update_row(self, name: str, row_id: RowId, values: Sequence[Value]) -> None:
        """整行替换：rid→页（D08）→ 页内按记录头 row_id 定位 → 替换 + 立即紧凑。

        失败：E_BAD_ARG / E_TABLE_NOT_FOUND / E_ROW_NOT_FOUND /
        E_VALUE_COUNT / E_TYPE_MISMATCH。返回前 flush（D11）。
        """
        raise NotImplementedError("M2：update_row")

    def delete_row(self, name: str, row_id: RowId) -> None:
        """删除一行：定位 → 移除槽 → 立即紧凑（D15）；整页空 → 还进空闲页链表。

        失败：E_BAD_ARG / E_TABLE_NOT_FOUND / E_ROW_NOT_FOUND。返回前 flush（D11）。
        """
        raise NotImplementedError("M2：delete_row")
