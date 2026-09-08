"""storage 内部常量与字节布局契约（B 私有，不进公共契约）。

本文件是“常量层面的单一真相”：所有内部模块只从这里取值，
不各自写死 magic / 偏移 / 大小，避免改一处漏多处。
对应决策编号见 .codex/docs/storage/storage_prd.md。
"""

# ---- 页大小与文件命名（D02/D03）----
PAGE_SIZE = 4096
TABLE_FILE_SUFFIX = ".table"
CATALOG_FILE_NAME = "catalog.json"

# ---- 表文件头（页 0）标识（D04）----
TABLE_FILE_MAGIC = b"HSQL"  # 4 B
TABLE_FILE_VERSION = 1      # 写入 2 B

# ---- 页 0 布局（D04）：offset / 大小 ----
PAGE0_MAGIC_OFFSET = 0
PAGE0_MAGIC_SIZE = 4
PAGE0_VERSION_OFFSET = 4
PAGE0_VERSION_SIZE = 2
PAGE0_RESERVED_OFFSET = 6
PAGE0_RESERVED_SIZE = 2
PAGE0_NEXT_ROW_ID_OFFSET = 8
PAGE0_NEXT_ROW_ID_SIZE = 8
PAGE0_FREE_HEAD_OFFSET = 16
PAGE0_FREE_HEAD_SIZE = 4
PAGE0_HEADER_SIZE = 20  # 之后的字节全部置 0，留白

# ---- 空闲页链表（D05）----
FREE_LIST_END = 0  # 链表尾哨兵：0 表示“没有下一个空闲页”

# ---- 数据页 slotted 布局（D07）----
PAGE_HEADER_SIZE = 8   # u16 slot_count + u16 flags + u32 free_ptr
SLOT_SIZE = 8          # u32 record_offset + u32 record_length
RECORD_HEADER_SIZE = 8  # 记录头：u64 row_id
TEXT_LEN_SIZE = 4
INT_SIZE = 8
REAL_SIZE = 8

# 单条记录能放进一个数据页的最大编码长度（≈ 4080 B）
INLINE_RECORD_LIMIT = PAGE_SIZE - PAGE_HEADER_SIZE - SLOT_SIZE

# ---- 超长行溢出页链（D14，M5 实现时定稿）----
OVERFLOW_HEADER_SIZE = 12  # 草案：u32 next_page + u64 total_len

# ---- 缓存（D16）----
DEFAULT_CACHE_CAPACITY = 64

# ---- catalog.json（D12）----
CATALOG_VERSION = 1
JSON_VERSION_KEY = "version"
JSON_TABLES_KEY = "tables"
JSON_COLUMNS_KEY = "columns"
JSON_NAME_KEY = "name"
JSON_TYPE_KEY = "type"
