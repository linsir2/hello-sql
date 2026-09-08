"""行级执行层（PRD §8；D07/D08/D14/D15）。

职责：公开方法拿到的是“Python 值元组 / row_id”，本层负责把它们
翻译成页内的记录字节并反向解码；页分配细节交给 pager。

不变量（D07/D15，页内布局）：
- 数据页：页头 8 B（u16 slot_count + u16 flags + u32 free_ptr），
  记录从 offset 8 起向后写，槽（8 B = offset + len）从页尾向前长；
- 任何时刻：活记录连在页头之后、槽连在页尾之前，中间是单块连续空闲区；
  空闲区 = (PAGE_SIZE - 8 * slot_count) - free_ptr；
- 删除 / 整行更新后立即页内紧凑（D15）；整页空 → 还进空闲页链表（D05，
  M2/M3 过渡期曾留在文件内复用，M4 起改为 free_page）；
- 记录编码：u64 row_id + 按列序的值（INT 8 B / REAL 8 B / TEXT 4 B 长 + UTF-8），
  不带类型标签；解码按同一份 ColumnDef；解码失败 → E_STORAGE。

row_id 不变量（D08）：
- 每表单调递增、永久不复用，计数器持久化在页 0（next_row_id）；
- 运行期维护 rid → 页号 内存映射；页内按记录头 row_id 定位；
- 映射缺失时允许退化全表找；找不到 → E_ROW_NOT_FOUND。

超长行（D14）：编码长度 > INLINE_RECORD_LIMIT 时走溢出页链；M5 实现，
M2 对超长行抛 E_STORAGE 并在消息中标注（§8.4）。

实现阶段：M2 已完成（行存取）；M4 已完成（空闲页回收 D05/D06）；
M5 溢出页链与损坏矩阵。
"""

from __future__ import annotations

import struct
from pathlib import Path
from typing import Iterator, Sequence

from contracts.ast import ColumnDef, SqlType, Value
from contracts.errors import E_ROW_NOT_FOUND, E_STORAGE, SqlError
from contracts.storage import Row, RowId

from storage.cache import BufferPool
from storage.constants import (
    INLINE_RECORD_LIMIT,
    PAGE_FREE_PTR_OFFSET,
    PAGE_HEADER_SIZE,
    PAGE_SIZE,
    PAGE_SLOT_COUNT_OFFSET,
    PAGE0_NEXT_ROW_ID_OFFSET,
    RECORD_HEADER_SIZE,
    SLOT_SIZE,
)
from storage.pager import (
    alloc_page,
    free_page,
    free_pages,
    page_count,
    read_page,
    write_page,
)


# 记录编码格式（§8.1）：u64 row_id；INT=q；REAL=d；TEXT=u32 长度 + UTF-8。
_RID = struct.Struct("<Q")
_INT = struct.Struct("<q")
_REAL = struct.Struct("<d")
_TEXT_LEN = struct.Struct("<I")
_PAGE_HEADER = struct.Struct("<HHI")   # u16 slot_count + u16 flags + u32 free_ptr
_SLOT = struct.Struct("<II")           # u32 record_offset + u32 record_length


def encode_record(
    row_id: RowId, columns: Sequence[ColumnDef], values: Sequence[Value]
) -> bytes:
    """把一行值按列序编码成记录字节（开头带 u64 row_id，§8.1）。

    调用方（门面）保证 values 已通过类型/个数边界检查并按 REAL 归一化为
    float；本函数不再重复语义检查（D13）。
    """
    parts = [_RID.pack(row_id)]
    for column, value in zip(columns, values):
        if column.type is SqlType.INT:
            parts.append(_INT.pack(value))
        elif column.type is SqlType.REAL:
            parts.append(_REAL.pack(value))
        else:  # SqlType.TEXT
            raw = value.encode("utf-8")
            parts.append(_TEXT_LEN.pack(len(raw)))
            parts.append(raw)
    return b"".join(parts)


def decode_record(record: bytes, columns: Sequence[ColumnDef]) -> Row:
    """按同一份 ColumnDef 反向解码记录 → (row_id, values 元组)。

    记录字节不足/尾部多余/TEXT 非 UTF-8/长度越界一律 E_STORAGE（§8.1）。
    """
    try:
        (row_id,) = _RID.unpack_from(record, 0)
    except struct.error as exc:
        raise SqlError(E_STORAGE, "corrupt record: missing row_id") from exc
    pos = _RID.size
    values: list[Value] = []
    try:
        for column in columns:
            if column.type is SqlType.INT:
                (value,) = _INT.unpack_from(record, pos)
                pos += _INT.size
            elif column.type is SqlType.REAL:
                (value,) = _REAL.unpack_from(record, pos)
                pos += _REAL.size
            else:  # SqlType.TEXT
                (length,) = _TEXT_LEN.unpack_from(record, pos)
                pos += _TEXT_LEN.size
                raw = record[pos : pos + length]
                if len(raw) < length:
                    raise SqlError(E_STORAGE, "corrupt record: truncated text")
                value = raw.decode("utf-8")
                pos += length
            values.append(value)
    except struct.error as exc:
        raise SqlError(E_STORAGE, "corrupt record: unexpected end") from exc
    except UnicodeDecodeError as exc:
        raise SqlError(E_STORAGE, "corrupt record: invalid utf-8 text") from exc
    if pos != len(record):
        raise SqlError(E_STORAGE, "corrupt record: trailing bytes")
    return (row_id, tuple(values))


# ---- slotted 数据页原语（§8.2、D15；engine 内部使用）----


def new_data_page() -> bytearray:
    """新建一块空数据页：页头 slot_count=0 / flags=0 / free_ptr=8，其余全 0。"""
    page = bytearray(PAGE_SIZE)
    _PAGE_HEADER.pack_into(page, 0, 0, 0, PAGE_HEADER_SIZE)
    return page


def _parse_page_header(page: bytearray) -> tuple[int, int, int]:
    """解页头并做结构校验；记录区与槽目录重叠 → E_STORAGE（§8.2 不变式）。"""
    if len(page) != PAGE_SIZE:
        raise SqlError(E_STORAGE, "corrupt page: not exactly one page")
    slot_count, flags, free_ptr = _PAGE_HEADER.unpack_from(page, 0)
    max_slots = (PAGE_SIZE - PAGE_HEADER_SIZE) // SLOT_SIZE
    if slot_count > max_slots:
        raise SqlError(E_STORAGE, f"corrupt page: slot_count {slot_count} too large")
    if free_ptr < PAGE_HEADER_SIZE:
        raise SqlError(E_STORAGE, "corrupt page: free_ptr before page header")
    slot_start = PAGE_SIZE - SLOT_SIZE * slot_count
    if free_ptr > slot_start:
        raise SqlError(E_STORAGE, "corrupt page: records overlap slot directory")
    return slot_count, flags, free_ptr


def _page_slot_entries(page: bytearray) -> list[tuple[int, int]]:
    """返回每个槽的 (record_offset, record_length)，同时校验槽内边界。"""
    slot_count, _flags, free_ptr = _parse_page_header(page)
    entries: list[tuple[int, int]] = []
    for i in range(slot_count):
        slot_pos = PAGE_SIZE - SLOT_SIZE * (i + 1)
        record_offset, record_length = _SLOT.unpack_from(page, slot_pos)
        if record_offset < PAGE_HEADER_SIZE or record_length < RECORD_HEADER_SIZE:
            raise SqlError(E_STORAGE, "corrupt page: slot points outside record area")
        if record_offset + record_length > free_ptr:
            raise SqlError(E_STORAGE, "corrupt page: slot record beyond free_ptr")
        entries.append((record_offset, record_length))
    return entries


def page_rows(page: bytearray, columns: Sequence[ColumnDef]) -> list[Row]:
    """解码页内全部记录（顺序 = 当前物理顺序，契约不承诺）。"""
    rows: list[Row] = []
    for record_offset, record_length in _page_slot_entries(page):
        record = bytes(page[record_offset : record_offset + record_length])
        rows.append(decode_record(record, columns))
    return rows


def append_record(page: bytearray, record: bytes) -> bool:
    """把记录追加到页尾空闲区并登记新槽；放不下返回 False 且不改页面。"""
    slot_count, _flags, free_ptr = _parse_page_header(page)
    new_count = slot_count + 1
    slot_start = PAGE_SIZE - SLOT_SIZE * new_count
    if free_ptr + len(record) > slot_start:
        return False
    dest = free_ptr
    page[dest : dest + len(record)] = record
    _SLOT.pack_into(page, slot_start, dest, len(record))
    page[PAGE_SLOT_COUNT_OFFSET : PAGE_SLOT_COUNT_OFFSET + 2] = struct.pack(
        "<H", new_count
    )
    page[PAGE_FREE_PTR_OFFSET : PAGE_FREE_PTR_OFFSET + 4] = struct.pack(
        "<I", dest + len(record)
    )
    return True


def _rebuild_records(page: bytearray, records: list[bytes]) -> bool:
    """重建式紧凑：记录从 offset 8 连续重排，槽目录重写，空闲区清 0（D15）。

    放不下返回 False 且不改页面；调用方必须先取出旧记录字节再调用本函数。
    """
    slot_count = len(records)
    slot_start = PAGE_SIZE - SLOT_SIZE * slot_count
    total_len = sum(len(record) for record in records)
    if PAGE_HEADER_SIZE + total_len > slot_start:
        return False
    free_ptr = PAGE_HEADER_SIZE
    for i, record in enumerate(records):
        page[free_ptr : free_ptr + len(record)] = record
        slot_pos = PAGE_SIZE - SLOT_SIZE * (i + 1)  # 槽 0 离页尾最近
        _SLOT.pack_into(page, slot_pos, free_ptr, len(record))
        free_ptr += len(record)
    _PAGE_HEADER.pack_into(page, 0, slot_count, 0, free_ptr)
    page[free_ptr:slot_start] = bytes(slot_start - free_ptr)
    return True


def remove_slot(page: bytearray, slot_index: int) -> None:
    """删除指定槽并立即页内紧凑（D15）；整页空时留下可复用空页。"""
    entries = _page_slot_entries(page)
    if not 0 <= slot_index < len(entries):
        raise SqlError(E_STORAGE, f"slot index {slot_index} out of range")
    records = [
        bytes(page[offset : offset + length])
        for i, (offset, length) in enumerate(entries)
        if i != slot_index
    ]
    if not _rebuild_records(page, records):
        raise SqlError(E_STORAGE, "internal: removal compaction should always fit")


def replace_slot(page: bytearray, slot_index: int, record: bytes) -> bool:
    """整行更新：把槽内记录换成新记录并紧凑；放不下返回 False 且页面不变。"""
    entries = _page_slot_entries(page)
    if not 0 <= slot_index < len(entries):
        raise SqlError(E_STORAGE, f"slot index {slot_index} out of range")
    records = [
        bytes(page[offset : offset + length])
        for offset, length in entries
    ]
    records[slot_index] = record
    return _rebuild_records(page, records)


class TableEngine:
    """单表行级执行：insert/scan/update/delete 行语义 + rid→页 映射（§8.3）。

    一个实例 = 一张表 + 一个共享 BufferPool + “本次运行”的行定位记忆；
    Storage 门面持有 {表名: TableEngine}，drop_table 时丢弃。
    """

    def __init__(
        self,
        table_path: str | Path,
        columns: Sequence[ColumnDef],
        pool: BufferPool,
    ) -> None:
        """绑定表文件路径、列定义与共享池；rid→页 映射初始为空（重启语义）。"""
        self._path = Path(table_path)
        self._columns = tuple(columns)
        self._pool = pool
        self._rid_to_page: dict[RowId, int] = {}

    # ---- 内部：row_id / 页定位 ----

    def _check_record_size(self, record: bytes) -> None:
        """单条记录超过 INLINE_RECORD_LIMIT 明确报错（溢出页链 M5 实现，D14）。"""
        if len(record) > INLINE_RECORD_LIMIT:
            raise SqlError(
                E_STORAGE,
                f"row too large ({len(record)}B > {INLINE_RECORD_LIMIT}B); "
                "overflow pages not implemented until M5",
            )

    def _take_next_row_id(self) -> RowId:
        """取页 0 计数器并把 next_row_id+1 写回（D08：单调、持久化）。"""
        page0 = read_page(self._pool, self._path, 0)
        (next_row_id,) = struct.unpack_from("<Q", page0, PAGE0_NEXT_ROW_ID_OFFSET)
        updated = bytearray(page0)
        struct.pack_into("<Q", updated, PAGE0_NEXT_ROW_ID_OFFSET, next_row_id + 1)
        write_page(self._pool, self._path, 0, updated)
        return next_row_id

    def _find_page_for(self, record_length: int) -> tuple[int, bytearray] | None:
        """在现有数据页里找能放下新记录的一页；没有返回 None。

        M4：只遍历不在 free list 的活动页（空闲页前 4 B 是 next 指针）。
        """
        for page_no in self._active_page_numbers():
            page = bytearray(read_page(self._pool, self._path, page_no))
            slot_count, _flags, free_ptr = _parse_page_header(page)
            space = PAGE_SIZE - SLOT_SIZE * (slot_count + 1) - free_ptr
            if space >= record_length:
                return page_no, page
        return None

    def _active_page_numbers(self) -> list[int]:
        """文件里 1..N-1 中不在 free list 的页号（scan/找页/定位共用）。"""
        free = set(free_pages(self._pool, self._path))
        total_pages = page_count(self._pool, self._path)
        return [page_no for page_no in range(1, total_pages) if page_no not in free]

    def _write_or_free_page(self, page_no: int, page: bytearray) -> None:
        """整页有行 → 写回；重建后整页空 → 还进空闲页链表（D05/D15）。"""
        slot_count, _flags, _free_ptr = _parse_page_header(page)
        if slot_count == 0:
            free_page(self._pool, self._path, page_no)
        else:
            write_page(self._pool, self._path, page_no, page)

    def _find_slot_by_row_id(
        self, page: bytes | bytearray, row_id: RowId
    ) -> int | None:
        """页内按记录头 u64 row_id 找槽（§8.3）。"""
        for i, (record_offset, record_length) in enumerate(
            _page_slot_entries(page)
        ):
            if record_length < RECORD_HEADER_SIZE:
                raise SqlError(E_STORAGE, "corrupt page: short record header")
            (stored_rid,) = _RID.unpack_from(page, record_offset)
            if stored_rid == row_id:
                return i
        return None

    def _locate(self, row_id: RowId) -> tuple[int, bytearray, int]:
        """定位 (页号, 页内容副本, 槽号)；找不到 → E_ROW_NOT_FOUND。

        先查 rid→页 映射，映射过期/缺失时退化全表找（§8.3）。
        """
        if type(row_id) is not int:
            raise SqlError(E_ROW_NOT_FOUND, f"row not found: {row_id!r}")
        if row_id in self._rid_to_page:
            page_no = self._rid_to_page[row_id]
            page = bytearray(read_page(self._pool, self._path, page_no))
            slot_index = self._find_slot_by_row_id(page, row_id)
            if slot_index is not None:
                return page_no, page, slot_index
        for page_no in self._active_page_numbers():
            page = bytearray(read_page(self._pool, self._path, page_no))
            slot_index = self._find_slot_by_row_id(page, row_id)
            if slot_index is not None:
                self._rid_to_page[row_id] = page_no
                return page_no, page, slot_index
        raise SqlError(E_ROW_NOT_FOUND, f"row not found: {row_id}")

    def _place_new_record(self, record: bytes) -> int:
        """放进现有空页或新分配页并落盘，返回所在页号。"""
        located = self._find_page_for(len(record))
        if located is not None:
            page_no, page = located
        else:
            page_no = alloc_page(self._pool, self._path)
            page = new_data_page()
        if not append_record(page, record):
            raise SqlError(E_STORAGE, "internal: record placement should fit")
        write_page(self._pool, self._path, page_no, page)
        return page_no

    # ---- 行级方法（供 Storage 门面调用）----

    def insert(self, values: Sequence[Value]) -> RowId:
        """分配新 row_id、落行并更新 rid→页 映射（§5.3）。"""
        row_id = self._take_next_row_id()
        record = encode_record(row_id, self._columns, values)
        self._check_record_size(record)
        page_no = self._place_new_record(record)
        self._rid_to_page[row_id] = page_no
        return row_id

    def scan(self) -> Iterator[Row]:
        """逐数据页解码 yield，顺带重建 rid→页 映射；顺序不承诺。"""
        for page_no in self._active_page_numbers():
            page = read_page(self._pool, self._path, page_no)
            for row in page_rows(page, self._columns):
                self._rid_to_page[row[0]] = page_no
                yield row

    def update(self, row_id: RowId, values: Sequence[Value]) -> None:
        """整行替换：原页放得下就地更新；放不下删旧后搬去别页/新页（D15）。"""
        if type(row_id) is not int:
            raise SqlError(E_ROW_NOT_FOUND, f"row not found: {row_id!r}")
        record = encode_record(row_id, self._columns, values)
        self._check_record_size(record)
        page_no, page, slot_index = self._locate(row_id)
        if replace_slot(page, slot_index, record):
            write_page(self._pool, self._path, page_no, page)
            return
        # 原页放不下：先删旧行落盘，再按插入路径找新家
        remove_slot(page, slot_index)
        self._write_or_free_page(page_no, page)
        new_page_no = self._place_new_record(record)
        self._rid_to_page[row_id] = new_page_no

    def delete(self, row_id: RowId) -> None:
        """删除一行并立即紧凑；整页空还进空闲页链表（D05/D15）。"""
        page_no, page, slot_index = self._locate(row_id)
        remove_slot(page, slot_index)
        self._write_or_free_page(page_no, page)
        self._rid_to_page.pop(row_id, None)
