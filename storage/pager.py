"""页级原语（PRD §6；D04/D05/D06）。

职责：把“表文件 = 一维页数组”落地——页 0 文件头、页分配/释放、
按页读写；具体行 / 槽布局属于 engine，本层不解释记录。

不变量：
- 表文件长度恒为 PAGE_SIZE 的整数倍，否则视为损坏（E_STORAGE）；
- 页 0 永不释放、不存用户行；字段布局见 constants（D04）；
- 文件只增不减，不自动收缩（D06）；
- magic / version / 文件长度校验失败 → E_STORAGE（契约 §4）。

目标不变量（随阶段生效，见“实现阶段”）：
- M3 起：一切页读写经 BufferPool，本层不直接裸 I/O（§6.5）；
- M4 起：空闲页链表——页 0 free_head 指向头块，空闲页头部前 4 B 存 next，
  FREE_LIST_END(0) 表示链尾；alloc 优先弹空闲链表（D05）。

错误归属：本层是 B 内部原语，所有失败统一 E_STORAGE（E_BAD_ARG 只属于
公开方法对库名/表名/值的边界，D13）。

实现阶段：M1 已完成（固定页、追加页、页 0、直通文件读写）；M3 把
read/write 内部改走 BufferPool（形参已冻结，调用方不改）；M4 补空闲页回收。
"""

from __future__ import annotations

import os
import struct
from pathlib import Path

from contracts.errors import E_STORAGE, SqlError
from storage.cache import BufferPool
from storage.constants import (
    FIRST_ROW_ID,
    FREE_LIST_END,
    PAGE0_HEADER_SIZE,
    PAGE_SIZE,
    TABLE_FILE_MAGIC,
    TABLE_FILE_VERSION,
)


# 页 0 头部 20 B：magic(4s) + version(H) + reserved(H) + next_row_id(Q) + free_head(I)
_PAGE0_STRUCT = struct.Struct("<4sHHQI")


def create_table_file(file_path: Path) -> None:
    """建表文件并写页 0（magic/version/next_row_id=1/free_head=0），flush。

    列定义不写进本文件（catalog 是权威，D12）。
    文件已存在（孤儿文件）时直接覆盖重建：catalog 是权威，孤儿数据属垃圾。
    """
    page = bytearray(PAGE_SIZE)
    _PAGE0_STRUCT.pack_into(
        page,
        0,
        TABLE_FILE_MAGIC,
        TABLE_FILE_VERSION,
        0,  # reserved
        FIRST_ROW_ID,
        FREE_LIST_END,
    )
    try:
        with open(file_path, "wb") as fh:
            written = fh.write(page)
    except OSError as exc:
        raise SqlError(E_STORAGE, f"cannot create table file: {file_path}") from exc
    if written != PAGE_SIZE:
        raise SqlError(E_STORAGE, f"short write creating table file: {file_path}")


def _table_page_count(file_path: Path) -> int:
    """文件长度 → 页数；缺失/半页/空文件都视为损坏（E_STORAGE）。"""
    try:
        size = file_path.stat().st_size
    except OSError as exc:
        raise SqlError(E_STORAGE, f"cannot access table file: {file_path}") from exc
    if size < PAGE_SIZE or size % PAGE_SIZE != 0:
        raise SqlError(
            E_STORAGE,
            f"corrupt table file {file_path}: size {size} is not page-aligned",
        )
    return size // PAGE_SIZE


def page_count(pool: BufferPool, file_path: Path) -> int:
    """返回文件当前页数（engine scan/遍历用；缺失/半页 → E_STORAGE）。

    PRD §6.5 未列此原语，但 scan 需要知道文件有几页，M2 补充。
    pool 形参同其他原语：M3 起内部可改走缓存，调用方不变。
    """
    return _table_page_count(file_path)


def _check_page0(file_path: Path) -> None:
    """校验页 0 头部：magic / version 不符 → E_STORAGE（文件身份/格式错误）。"""
    try:
        with open(file_path, "rb") as fh:
            raw = fh.read(PAGE0_HEADER_SIZE)
    except OSError as exc:
        raise SqlError(E_STORAGE, f"cannot read table file: {file_path}") from exc
    if len(raw) < PAGE0_HEADER_SIZE:
        raise SqlError(E_STORAGE, f"corrupt table file {file_path}: short page 0")
    magic, version, _reserved, _next_row_id, _free_head = _PAGE0_STRUCT.unpack(raw)
    if magic != TABLE_FILE_MAGIC:
        raise SqlError(E_STORAGE, f"not a hello-sql table file: {file_path}")
    if version != TABLE_FILE_VERSION:
        raise SqlError(
            E_STORAGE,
            f"unsupported table file version {version}: {file_path}",
        )


def _check_page_no(file_path: Path, page_no: int, page_count: int) -> None:
    """页号必须是整型且在 [0, page_count) 内，否则 E_STORAGE。"""
    if type(page_no) is not int or page_no < 0 or page_no >= page_count:
        raise SqlError(
            E_STORAGE,
            f"page {page_no!r} out of range in {file_path} ({page_count} pages)",
        )


def alloc_page(pool: BufferPool, file_path: Path) -> int:
    """分配一个数据页号：M1 只在文件末尾追加（D06）；free list 弹出留 M4。

    追加前校验页 0 身份，防止在冒牌/损坏文件上继续扩展（E_STORAGE）。
    pool 形参已冻结，M3 起页 I/O 改走缓存时本函数内部再用。
    """
    page_count = _table_page_count(file_path)
    _check_page0(file_path)
    new_page_no = page_count
    try:
        with open(file_path, "r+b") as fh:
            fh.seek(0, os.SEEK_END)
            written = fh.write(bytes(PAGE_SIZE))
    except OSError as exc:
        raise SqlError(E_STORAGE, f"cannot extend table file: {file_path}") from exc
    if written != PAGE_SIZE:
        raise SqlError(E_STORAGE, f"short write extending table file: {file_path}")
    return new_page_no


def free_page(pool: BufferPool, file_path: Path, page_no: int) -> None:
    """把整页空的数据页还进空闲链表：页头写 next，页 0 free_head 指向它。"""
    raise NotImplementedError("M4：free_page")


def read_page(pool: BufferPool, file_path: Path, page_no: int) -> bytes:
    """读一整页返回 bytes 副本。

    M1 直通文件（pool 暂不使用，M3 改走缓存）；读页 0 时额外校验
    magic/version；页越界/半页/缺失一律 E_STORAGE。
    """
    page_count = _table_page_count(file_path)
    _check_page_no(file_path, page_no, page_count)
    if page_no == 0:
        _check_page0(file_path)
    try:
        with open(file_path, "rb") as fh:
            fh.seek(page_no * PAGE_SIZE)
            data = fh.read(PAGE_SIZE)
    except OSError as exc:
        raise SqlError(E_STORAGE, f"cannot read table file: {file_path}") from exc
    if len(data) != PAGE_SIZE:
        raise SqlError(
            E_STORAGE, f"corrupt table file {file_path}: short read on page {page_no}"
        )
    return data


def write_page(
    pool: BufferPool, file_path: Path, page_no: int, data: bytes
) -> None:
    """把一整页内容写回文件偏移 page_no * PAGE_SIZE。

    M1 直通文件落盘（pool 暂不使用，M3 改走缓存帧 + 标脏）；data 必须恰好
    一整页，页号必须在文件现有范围内，否则 E_STORAGE。
    """
    if not isinstance(data, (bytes, bytearray)) or len(data) != PAGE_SIZE:
        raise SqlError(
            E_STORAGE, f"write_page requires exactly {PAGE_SIZE} bytes: {file_path}"
        )
    page_count = _table_page_count(file_path)
    _check_page_no(file_path, page_no, page_count)
    try:
        with open(file_path, "r+b") as fh:
            fh.seek(page_no * PAGE_SIZE)
            written = fh.write(data)
    except OSError as exc:
        raise SqlError(E_STORAGE, f"cannot write table file: {file_path}") from exc
    if written != PAGE_SIZE:
        raise SqlError(E_STORAGE, f"short write on page {page_no}: {file_path}")
