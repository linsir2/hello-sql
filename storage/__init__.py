"""模块 B：存储层（对外入口就是本文件）。

B 分两层，两个类都定义在本文件里：

    from storage import DatabaseServer

    server = DatabaseServer("data")        # 根目录；自动创建默认库 main
    server.create_database("shop")         # 库层：建库 / 删库 / 列库 / 连接
    storage = server.connect("main")       # 连接某个库，返回 Storage
    storage.create_table("users", [...])   # 表层：原 8 个表级方法不变

目录布局：data/<库名>/<表文件>，B 内部自由。进程重启后，已建的库和
表数据必须完整可读；默认库 main 永远存在、不可删除。

方法清单与语义见 docs/contract-v1.md 第 3 节；本文件里的类与方法是
B 方法契约的唯一代码真相，C 只调用、不复制。
跨模块传递的数据形状见 contracts.storage（Row / TableInfo 等）。

本目录由模块 B 的开发独占。禁止 import compiler / runner。
"""
