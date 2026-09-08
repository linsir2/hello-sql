# hello-sql

三人并行开发的最小 SQL demo：A=编译（compiler/）、B=存储（storage/）、
C=运行（runner/）。所有公共约定已经冻结：

- 契约规范：[docs/contract-v1.md](docs/contract-v1.md)
- 类型定义与 AST：[contracts/ast.py](contracts/ast.py)
- 存储（B 的实现入口）：`storage/__init__.py` 暴露 `DatabaseServer`（库层）与 `Storage`（表层）；方法清单见 [docs/contract-v1.md](docs/contract-v1.md) 第 3 节
- 存储共享形状（Row/TableInfo）：[contracts/storage.py](contracts/storage.py)
- 错误码：[contracts/errors.py](contracts/errors.py)
- 执行结果：[contracts/result.py](contracts/result.py)
- 三方共同基准：[tests/golden_sql.py](tests/golden_sql.py)
- 装配入口：`main.py`（唯一同时 import 三家的文件）

开工前：先一起把 docs/contract-v1.md 第 0 节的决定画勾，然后每人只在自己的
目录里实现，最后集成日按 golden 顺序跑通全部用例。

## 调用关系（包名即 import 名）

```python
from compiler import parse          # A：SQL -> AST
from storage import DatabaseServer  # B：库层 + 表层，Storage 由 connect 得到
from runner import Runner           # C：执行与 REPL

# main.py 装配（唯一允许同时接触三家的地方）
server = DatabaseServer("data")     # 自动创建默认库 main
runner = Runner(server=server, parse=parse)
```

运行期由 runner 维护“当前库”：USE 时换一个 `server.connect(...)` 的
Storage，然后调用它的 describe / scan / insert / update_row / delete_row。
compiler、storage、runner 之间互不 import，只允许 import contracts
里的共享数据格式。

## 环境配置（统一 Python 3.11）

```bash
python3.11 -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
python -m pip install -r requirements.txt
python -m pytest -q              # 目前还没有测试用例，等三份契约测试加入后生效
```
