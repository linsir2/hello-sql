"""模块 B：存储层（对外入口就是本文件）。

Storage 类直接定义在本文件里，让别人可以：

    from storage import Storage
    storage = Storage("data")   # data_dir 不存在会自动创建
    storage.create_table(...)

方法清单与语义见 docs/contract-v1.md 第 3 节；本类中的方法签名
是 Storage 方法契约的唯一代码真相，C 只调用、不复制。
跨模块传递的数据形状见 contracts.storage（Row / TableInfo 等）。

本目录由模块 B 的开发独占。
禁止 import compiler / runner。
"""
