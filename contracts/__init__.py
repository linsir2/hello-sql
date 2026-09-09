"""共享契约包（V2.0）。

三方共同约定：
- contracts/ast.py     —— A 输出、C 输入
- contracts/storage.py —— B 实现、C 调用
- contracts/errors.py  —— 错误码，三方共用
- contracts/result.py  —— C 输出

V2 的范围、迁移顺序与验收标准见 docs/v2-dev/v2-dev-plan.md。
contracts 仍只保存跨模块数据格式和协议，不承载具体实现。
"""

__version__ = "2.0"
