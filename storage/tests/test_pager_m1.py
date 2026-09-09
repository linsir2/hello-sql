"""pager M1 测试：固定 4KB 页、页 0 文件头、追加分配、直通文件读写。

命名规则：每个测试先问“哪个生产改动会让它失败”；期望值（头部字节、
文件长度、错误码）全部手写字面量，不引用生产代码里的取值常量。

M1 直通文件 I/O，BufferPool 形参已冻结但暂不使用：测试照常传真实小池，
M3 把内部改成走缓存时这些调用与签名都不必改。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from contracts.errors import E_STORAGE, SqlError
from storage.cache import BufferPool
from storage.constants import PAGE_SIZE
from storage.pager import alloc_page, create_table_file, read_page, write_page


# 页 0 初始 20 B 头部，手工拼字面量：
# magic b"HSQL" + version 1(u16) + reserved 0(u16)
# + next_row_id 1(u64) + free_head 0(u32)
_INITIAL_PAGE0_HEADER = (
    b"HSQL"
    + (1).to_bytes(2, "little")
    + (0).to_bytes(2, "little")
    + (1).to_bytes(8, "little")
    + (0).to_bytes(4, "little")
)

_DATA_4096 = bytes(range(256)) * 16  # 正好一整页


@pytest.fixture
def pool() -> BufferPool:
    return BufferPool(capacity=8)


@pytest.fixture
def table_path(tmp_path) -> Path:
    return tmp_path / "users.table"


def _header(raw: bytes) -> bytes:
    return raw[:20]


def _corrupt(path: Path, offset: int, new_bytes: bytes) -> None:
    raw = path.read_bytes()
    path.write_bytes(raw[:offset] + new_bytes + raw[offset + len(new_bytes) :])


def _expect_storage_error(call) -> None:
    with pytest.raises(SqlError) as exc:
        call()
    assert exc.value.code == E_STORAGE


# ---- create_table_file ----


def test_create_table_file_writes_single_page_with_initial_header(table_path):
    """建表文件必须恰好一页，页 0 头 20 B 按固定布局写初值，其余全 0。

    断言的改动：不写页 0、写错偏移/初值（next_row_id 不是 1、free_head 非 0）、
    文件长度不是 PAGE_SIZE、尾部残留旧字节，任一都会让测试失败。
    """
    create_table_file(table_path)

    raw = table_path.read_bytes()
    assert len(raw) == PAGE_SIZE
    assert _header(raw) == _INITIAL_PAGE0_HEADER
    assert raw[20:] == bytes(PAGE_SIZE - 20)


def test_create_table_file_overwrites_existing_file(table_path):
    """已存在的孤儿文件应被覆盖重建，而不是报错或保留旧内容。

    断言的改动：create_table_file 遇到已存在文件时抛错/跳过。
    """
    table_path.write_bytes(b"junk" * 3000)

    create_table_file(table_path)

    raw = table_path.read_bytes()
    assert len(raw) == PAGE_SIZE
    assert _header(raw) == _INITIAL_PAGE0_HEADER


def test_create_table_file_missing_parent_dir_raises_storage(tmp_path):
    """父目录不存在写不进去时抛 E_STORAGE，而不是静默成功。

    断言的改动：create_table_file 吞掉 OSError 或抛其他码。
    """
    target = tmp_path / "no_such_dir" / "users.table"

    _expect_storage_error(lambda: create_table_file(target))


# ---- alloc_page ----


def test_alloc_page_appends_zeroed_pages_and_returns_increasing_numbers(pool, table_path):
    """追加分配：页号从 1 起连续，文件按整页增长，新页内容全 0。

    断言的改动：alloc_page 复用页号、不把文件长度按 PAGE_SIZE 递增、
    或在旧文件内容后留下非零残留。
    """
    create_table_file(table_path)

    assert alloc_page(pool, table_path) == 1
    assert table_path.stat().st_size == PAGE_SIZE * 2
    assert read_page(pool, table_path, 1) == bytes(PAGE_SIZE)

    assert alloc_page(pool, table_path) == 2
    assert table_path.stat().st_size == PAGE_SIZE * 3
    assert read_page(pool, table_path, 2) == bytes(PAGE_SIZE)


def test_alloc_page_does_not_modify_page0(pool, table_path):
    """追加页不得改写页 0 账本（next_row_id/free_head 保持初值）。

    断言的改动：alloc_page 顺手把页 0 头部改掉。
    """
    create_table_file(table_path)

    alloc_page(pool, table_path)

    raw = table_path.read_bytes()
    assert _header(raw) == _INITIAL_PAGE0_HEADER


def test_alloc_page_missing_file_raises_storage(pool, table_path):
    """文件不存在时 alloc 抛 E_STORAGE，绝不静默创建空文件。

    断言的改动：alloc_page 对缺失文件自行补建（绕过 create_table_file 的页 0 初始化）。
    """
    _expect_storage_error(lambda: alloc_page(pool, table_path))


def test_alloc_page_rejects_half_page_file(pool, table_path):
    """长度不是整页（出现半页）时 alloc 抛 E_STORAGE。

    断言的改动：alloc_page 无视文件长度直接追加，把损坏文件继续撑大。
    """
    create_table_file(table_path)
    table_path.write_bytes(table_path.read_bytes() + b"x" * 100)

    _expect_storage_error(lambda: alloc_page(pool, table_path))


def test_alloc_page_rejects_corrupt_page0_magic(pool, table_path):
    """页 0 magic 被改坏时 alloc 抛 E_STORAGE，防止在冒牌文件上扩展。

    断言的改动：alloc_page 不校验文件身份就追加新页。
    """
    create_table_file(table_path)
    _corrupt(table_path, 0, b"XXXX")

    _expect_storage_error(lambda: alloc_page(pool, table_path))


# ---- read_page / write_page ----


def test_write_read_page_roundtrip_survives_new_buffer_pool(pool, table_path):
    """写一整页再读回必须逐字节一致；换一个 BufferPool（模拟重启）仍可读。

    断言的改动：write_page 写错偏移/长度、read_page 读错偏移，或数据只留在
    旧 pool 内存没落盘，任一都会让测试失败。
    """
    create_table_file(table_path)
    alloc_page(pool, table_path)

    write_page(pool, table_path, 1, _DATA_4096)
    assert read_page(pool, table_path, 1) == _DATA_4096
    pool.flush(table_path)  # M3 起持久化由 flush 负责（D11）

    fresh_pool = BufferPool(capacity=4)
    assert read_page(fresh_pool, table_path, 1) == _DATA_4096


@pytest.mark.parametrize("bad_length", [PAGE_SIZE - 1, PAGE_SIZE + 1])
def test_write_page_rejects_non_page_sized_data(pool, table_path, bad_length):
    """写入数据长度不是一整页时抛 E_STORAGE（内部原语契约）。

    断言的改动：write_page 接受任意长度并写出半页，破坏“文件=整页数组”。
    """
    create_table_file(table_path)
    alloc_page(pool, table_path)

    _expect_storage_error(
        lambda: write_page(pool, table_path, 1, bytes(bad_length))
    )


def test_write_page_missing_file_raises_storage(pool, table_path):
    """write 到不存在的文件抛 E_STORAGE。

    断言的改动：write_page 静默新建文件（绕过页 0 初始化）。
    """
    _expect_storage_error(
        lambda: write_page(pool, table_path, 0, bytes(PAGE_SIZE))
    )


@pytest.mark.parametrize("page_no", [-1, 1, 2])
def test_write_page_out_of_range_raises_storage(pool, table_path, page_no):
    """文件里不存在的页号（含负数）write 必须抛 E_STORAGE 且不产生副作用。

    断言的改动：write_page 越界时静默成功或把文件撑大。
    """
    create_table_file(table_path)

    _expect_storage_error(
        lambda: write_page(pool, table_path, page_no, bytes(PAGE_SIZE))
    )
    assert table_path.stat().st_size == PAGE_SIZE


def test_read_page_missing_file_raises_storage(pool, table_path):
    """read 不存在的文件抛 E_STORAGE。

    断言的改动：read_page 吞掉 FileNotFoundError 返回空/零页。
    """
    _expect_storage_error(lambda: read_page(pool, table_path, 0))


@pytest.mark.parametrize("page_no", [-1, 1, 2])
def test_read_page_out_of_range_raises_storage(pool, table_path, page_no):
    """read 不存在的页号（含负数）抛 E_STORAGE。

    断言的改动：read_page 越界时返回零页/越界截断内容。
    """
    create_table_file(table_path)

    _expect_storage_error(lambda: read_page(pool, table_path, page_no))


def test_read_page_rejects_half_page_file(pool, table_path):
    """文件长度出现半页时 read 抛 E_STORAGE（截断/追加残留都算损坏）。

    断言的改动：read_page 只按偏移读不校验总长度，半页文件仍能读。
    """
    create_table_file(table_path)
    table_path.write_bytes(table_path.read_bytes() + b"tail")

    _expect_storage_error(lambda: read_page(pool, table_path, 0))


def test_read_page0_rejects_bad_magic(pool, table_path):
    """页 0 magic 不是 b"HSQL" 时 read 抛 E_STORAGE（文件身份不符）。

    断言的改动：read_page 不校验 magic，冒牌/半初始化文件照样读出。
    """
    create_table_file(table_path)
    _corrupt(table_path, 0, b"XXXX")

    _expect_storage_error(lambda: read_page(pool, table_path, 0))


def test_read_page0_rejects_unsupported_version(pool, table_path):
    """页 0 version 不支持时 read 抛 E_STORAGE（防止新旧格式互读）。

    断言的改动：read_page 忽略 version，未来格式变更时旧代码误读新文件。
    """
    create_table_file(table_path)
    _corrupt(table_path, 4, (99).to_bytes(2, "little"))

    _expect_storage_error(lambda: read_page(pool, table_path, 0))
