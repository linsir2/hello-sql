"""共享契约包（V1.1 冻结）。

三方共同约定：
- contracts/ast.py     —— A 输出、C 输入
- contracts/storage.py —— B 实现、C 调用
- contracts/errors.py  —— 错误码，三方共用
- contracts/result.py  —— C 输出

改动任何共享契约都需要三方同意，并同步 docs/contract-v1.md 与
tests/golden_sql.py。
"""

__version__ = "1.1"
