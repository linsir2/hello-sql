"""模块 B：存储层。

本目录由模块 B 的开发独占。对外唯一入口（由 B 实现）：

    class Storage:
        def __init__(self, data_dir: str | Path): ...
        # 并实现 contracts.storage.Storage 中的全部方法

方法签名与语义见 contracts/storage.py，禁止自行增删改。
"""

