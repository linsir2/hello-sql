# hello-sql

三人并行开发的最小 SQL demo：A=编译（compiler/）、B=存储（storage/）、
C=运行（runner/）。所有公共约定已经冻结：

- 契约规范：[docs/contract-v1.md](docs/contract-v1.md)
- 类型定义与 AST：[contracts/ast.py](contracts/ast.py)
- 存储接口：[contracts/storage.py](contracts/storage.py)
- 错误码：[contracts/errors.py](contracts/errors.py)
- 执行结果：[contracts/result.py](contracts/result.py)
- 三方共同基准：[tests/golden_sql.py](tests/golden_sql.py)

开工前：先一起把 docs/contract-v1.md 第 0 节的决定画勾，然后每人只在自己的
目录里实现，最后集成日按 golden 顺序跑通全部用例。

## 环境配置（统一 Python 3.11）

```bash
python3.11 -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
python -m pip install -r requirements.txt
python -m pytest -q              # 目前还没有测试用例，等三份契约测试加入后生效
```
