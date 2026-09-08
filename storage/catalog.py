"""系统目录 catalog（PRD §9；D12）。

定位：B 的**私有内部记忆**，不是给别人调用的接口——describe / list_tables /
create_table / drop_table / insert 全靠它；C 只通过公开方法间接使用，A 不碰。

不变量：
- 每库恰好一份 catalog.json（本库目录下，文件名见 constants，D03）；
- 内存形态：tables: dict[表名, tuple[ColumnDef, ...]]，插入顺序 = 建表顺序；
- 持久化 JSON：{"version": 1, "tables": {表名: {"columns": [{"name", "type"}]}}}；
- 版本不符 / JSON 损坏 / 目录存在但文件缺失 → E_STORAGE；
- 一致性顺序（§9.4）：create_table 先建文件后注册；drop_table 先摘牌后删文件；
  catalog 是权威，孤儿表文件本期容忍（不自动清理）；
- 本文件不做 SQL 语义检查（D13），只做注册表增删查。

实现阶段：M0（可先独立验证建库 / 建表 / 重启恢复）。
"""

from __future__ import annotations

from pathlib import Path
from typing import Sequence

from contracts.ast import ColumnDef


class Catalog:
    """本库 schema 的内存注册表 + 持久化。

    path   ：catalog.json 的绝对路径；
    tables ：表名 → 按建表顺序的列定义（表内永久顺序，不可变）。
    """

    def __init__(self, path: Path) -> None:
        """M0 骨架：path 传入；tables 在 load() 时填充。"""
        raise NotImplementedError("M0：Catalog.__init__")

    def load(self) -> None:
        """从磁盘读入并校验（版本、类型名、列序）。损坏 → E_STORAGE。"""
        raise NotImplementedError("M0：Catalog.load")

    def save(self) -> None:
        """把内存注册表写回 catalog.json（register/unregister 后调用）。"""
        raise NotImplementedError("M0：Catalog.save")

    def register(self, name: str, columns: Sequence[ColumnDef]) -> None:
        """登记一张新表（调用方已做过 E_TABLE_EXISTS 等边界校验）。"""
        raise NotImplementedError("M0：Catalog.register")

    def unregister(self, name: str) -> None:
        """注销一张表（调用方已确认表存在）。"""
        raise NotImplementedError("M0：Catalog.unregister")

    def get(self, name: str) -> tuple[ColumnDef, ...]:
        """查表结构（调用方已确认存在；返回副本，防外部改内部状态）。"""
        raise NotImplementedError("M0：Catalog.get")

    def names(self) -> list[str]:
        """返回本库全部表名（稳定顺序即可，契约不承诺顺序）。"""
        raise NotImplementedError("M0：Catalog.names")
