"""缓存层 BufferPool（PRD §7；D09/D10/D16/D17/D18）。

职责：管理页帧。pager / engine 的一切页 I/O 都经过本层，本层是
页读写唯一入口（读盘、写盘都由这里做）。

不变量：
- 容量在构造时固定，默认 DEFAULT_CACHE_CAPACITY = 64（D16）；
- key = (表文件绝对路径, page_no)，跨库同名表天然隔离（D09）；
- 帧 = 4 KB bytearray + clean/dirty + pin 计数（本文件 Frame）；
- pin > 0 的帧不可淘汰；淘汰只发生在 pin == 0 的帧上（D17）；
- 淘汰脏帧前必须先写回文件；clean 帧可直接丢弃；
- drop_table / drop_database 用 discard（不写回，D11）；
- 统计：hits / misses / evictions / dirty_writes 只增不减（D18）。

实现阶段：M3（M1/M2 可先直读文件过渡，里程碑表见 PRD §12）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from storage.constants import DEFAULT_CACHE_CAPACITY, PAGE_SIZE


@dataclass
class Frame:
    """一个页帧 = 内存里的一个“车位”（D17）。

    data  ：该页 4 KB 内容的副本；engine 改这里后标脏；
    dirty ：内存与磁盘是否不一致（标脏 = 置 True）；
    pin   ：当前正握着本帧的操作数；>0 时 LRU 不得淘汰。
    """

    data: bytearray = field(default_factory=lambda: bytearray(PAGE_SIZE))
    dirty: bool = False
    pin: int = 0


class BufferPool:
    """页缓存：LRU + pin/dirty + 写回（M3 实现）。

    计划内部结构：OrderedDict[key, Frame]（D10：只做 LRU，FIFO 预留策略位）。
    """

    def __init__(self, capacity: int = DEFAULT_CACHE_CAPACITY) -> None:
        """M0 骨架。capacity 为 B 内部参数，不进任何公开签名（D16）。"""
        raise NotImplementedError("M3：BufferPool.__init__")

    def get_page(self, file_path: Path, page_no: int) -> bytearray:
        """取页：命中直接返回；未命中读盘后返回。返回前已 pin（D17）。

        调用方必须成对调用 unpin_page；不允许跨公开方法持有。
        """
        raise NotImplementedError("M3：get_page")

    def unpin_page(self, file_path: Path, page_no: int) -> None:
        """放页：pin -= 1；归零后该帧恢复可淘汰状态（D17）。"""
        raise NotImplementedError("M3：unpin_page")

    def mark_dirty(self, file_path: Path, page_no: int) -> None:
        """标脏：记录本帧与磁盘不一致（flush 时写回）。"""
        raise NotImplementedError("M3：mark_dirty")

    def flush(self, file_path: Path | None = None) -> None:
        """把指定表文件（或全部）的脏帧写回磁盘；写回后置 clean（D11）。"""
        raise NotImplementedError("M3：flush")

    def discard(self, file_path: Path) -> None:
        """丢弃某表文件的全部帧，不写回——删表/删库前调用（D11）。"""
        raise NotImplementedError("M3：discard")

    def reset_stats(self) -> None:
        """清零统计（仅供测试与报告，D18）。"""
        raise NotImplementedError("M3：reset_stats")

    @property
    def stats(self) -> dict[str, int | float]:
        """只读统计快照：capacity/hits/misses/evictions/dirty_writes/hit_rate。"""
        raise NotImplementedError("M3：stats")
