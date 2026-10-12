"""blob 存储的实现们。

`FsBlobStore` **同时**满足 `MemoryBlobStore` 与 `EventBlobStore` 两个契约——这是
「实现可以偷懒」的形态，不等于两个契约可以合并（它们各自定义、类型无关，回收锚点
不同：memory 侧是记录 is_superseded，event 侧是事件保留策略）。共用一个实例时
`collect()` 的 live_refs 必须同时含两侧的活引用，见该类 docstring。
"""

from ctx_weft.providers.blob.fs import FsBlobStore

__all__ = ["FsBlobStore"]
