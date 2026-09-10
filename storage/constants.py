"""storage 内部常量与字节布局契约（B 私有，不进公共契约）。

本文件是“常量层面的单一真相”：所有内部模块只从这里取值，
不各自写死 magic / 偏移 / 大小，避免改一处漏多处。
对应决策编号见 .codex/docs/storage/storage_prd.md。
"""

# ---- 页大小与文件命名（D02/D03）----
PAGE_SIZE = 4096
TABLE_FILE_SUFFIX = ".table"
CATALOG_FILE_NAME = "catalog.json"

# V2 页式系统表（D20/D21；M1 只实现自举，M2 起作为权威目录）
SYS_TABLES_FILE_NAME = "sys_tables.db"
SYS_COLUMNS_FILE_NAME = "sys_columns.db"
RESERVED_TABLE_PREFIX = "__sys_"
LEGACY_MIGRATED_FILE_NAME = "catalog.v1.migrated.json"

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
FIRST_ROW_ID = 1        # 新建表第一个可分配的 row_id（D08；页 0 初值）

# ---- 空闲页链表（D05）----
FREE_LIST_END = 0  # 链表尾哨兵：0 表示“没有下一个空闲页”

# ---- 数据页 slotted 布局（D07）----
PAGE_HEADER_SIZE = 8   # u16 slot_count + u16 flags + u32 free_ptr
PAGE_SLOT_COUNT_OFFSET = 0
PAGE_SLOT_COUNT_SIZE = 2
PAGE_FLAGS_OFFSET = 2
PAGE_FLAGS_SIZE = 2
PAGE_FREE_PTR_OFFSET = 4
PAGE_FREE_PTR_SIZE = 4
SLOT_SIZE = 8          # u32 record_offset + u32 record_length
RECORD_HEADER_SIZE = 8  # 记录头：u64 row_id
TEXT_LEN_SIZE = 4
INT_SIZE = 8
REAL_SIZE = 8
BOOL_SIZE = 1
BOOL_FALSE_BYTE = 0x00
BOOL_TRUE_BYTE = 0x01

# 单条记录能放进一个数据页的最大编码长度（≈ 4080 B）
INLINE_RECORD_LIMIT = PAGE_SIZE - PAGE_HEADER_SIZE - SLOT_SIZE

# ---- 超长行溢出页链（D14，M5 定稿）----
# 槽长度最高位 = 溢出锚点标志；inline 记录最大 4080B，永不触及该位。
SLOT_OVERFLOW_FLAG = 0x80000000

# 锚点记录（存放在普通数据页槽里，物理 16B）：
# u64 row_id + u32 first_chain_page + u32 total_len
OVERFLOW_ANCHOR_SIZE = 16
OVERFLOW_ANCHOR_ROW_ID_OFFSET = 0
OVERFLOW_ANCHOR_ROW_ID_SIZE = 8
OVERFLOW_ANCHOR_FIRST_PAGE_OFFSET = 8
OVERFLOW_ANCHOR_FIRST_PAGE_SIZE = 4
OVERFLOW_ANCHOR_TOTAL_LEN_OFFSET = 12
OVERFLOW_ANCHOR_TOTAL_LEN_SIZE = 4

# 溢出页头（每页 16B）：magic b"OVFL" + u32 next_page + u64 total_len。
# magic 让 scan 能识别链页、不与数据页/空闲页混读（替换 12B 草案）。
OVERFLOW_MAGIC = b"OVFL"
OVERFLOW_HEADER_SIZE = 16
OVERFLOW_NEXT_PAGE_OFFSET = 4
OVERFLOW_NEXT_PAGE_SIZE = 4
OVERFLOW_TOTAL_LEN_OFFSET = 8
OVERFLOW_TOTAL_LEN_SIZE = 8
OVERFLOW_PAYLOAD_SIZE = PAGE_SIZE - OVERFLOW_HEADER_SIZE

# 单行编码总长上限（防演示把内存/缓存撑爆；超出仍 E_STORAGE）
MAX_ROW_BYTES = 16 * 1024 * 1024

# ---- 缓存（D16）----
DEFAULT_CACHE_CAPACITY = 64

# ---- V1 catalog.json（D12；V2 起只作为一次性迁移输入，不再创建）----
CATALOG_VERSION = 1
JSON_VERSION_KEY = "version"
JSON_TABLES_KEY = "tables"
JSON_COLUMNS_KEY = "columns"
JSON_NAME_KEY = "name"
JSON_TYPE_KEY = "type"
