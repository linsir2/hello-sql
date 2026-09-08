"""页级原语（PRD §6；D04/D05/D06）。

职责：把“表文件 = 一维页数组”落地——页 0 文件头、页分配/释放、
按页读写；具体行 / 槽布局属于 engine，本层不解释记录。

不变量：
- 表文件长度恒为 PAGE_SIZE 的整数倍，否则视为损坏（E_STORAGE）；
- 页 0 永不释放、不存用户行；字段布局见 constants（D04）；
- 空闲页链表：页 0 的 free_head 指向第一块空闲页；空闲页头部前 4 B
  存 next 页号，FREE_LIST_END(0) 表示链尾（D05）；
- alloc：优先弹空闲链表，空链表则在文件末尾追加一页（D05/D06）；
- 文件只增不减，不自动收缩（D06）；
- 所有页读写必须经 BufferPool（§6.5），本层不直接裸 I/O；
- magic / version / 文件长度校验失败 → E_STORAGE（契约 §4）。

实现阶段：M1（固定页 + 追加页），M4 补空闲页回收。
"""

from __future__ import annotations

from pathlib import Path

from storage.cache import BufferPool


def create_table_file(file_path: Path) -> None:
    """建表文件并写页 0（magic/version/next_row_id=1/free_head=0），flush。

    列定义不写进本文件（catalog 是权威，D12）。
    """
    raise NotImplementedError("M1：create_table_file")


def alloc_page(pool: BufferPool, file_path: Path) -> int:
    """分配一个数据页号：有 free_head 则弹出；否则在文件末尾追加（D05/D06）。"""
    raise NotImplementedError("M1/M4：alloc_page")


def free_page(pool: BufferPool, file_path: Path, page_no: int) -> None:
    """把整页空的数据页还进空闲链表：页头写 next，页 0 free_head 指向它。"""
    raise NotImplementedError("M4：free_page")


def read_page(pool: BufferPool, file_path: Path, page_no: int) -> bytes:
    """读一整页（内部即 pool.get_page + unpin；未命中由 pool 补读）。"""
    raise NotImplementedError("M1：read_page")


def write_page(pool: BufferPool, file_path: Path, page_no: int) -> None:
    """把当前内存页标脏（真正落盘由 flush 决定，D11）。"""
    raise NotImplementedError("M1：write_page")
