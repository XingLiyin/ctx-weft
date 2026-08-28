"""图片折叠与回放（子设计 §7）。

墙内：占位格式、blob ref 交互、位置描述。墙外：`BlobStore` 协议归 `protocols/memory`（字节归 memory，裁定 D4）、
ref→base64 的 rehydrate 归 `providers/llm/*`、入口外部化归内容归一层、图片 token 口径归
`core/utils`。

对外只导出子设计 §8 的三个函数——`demote_for_budget` / `demote_all`（Task 2）与
`get_image`（Task 4）；本包其余内容（含 `refs`）是实现细节，`refs` 只供包内与其单测使用。

本仓所有图片占位的清单在 `ctx_weft.core.media.refs` 的模块 docstring（L6 收口归口）。
"""

from __future__ import annotations

from ctx_weft.core.media.capability import get_image
from ctx_weft.core.media.fold import demote_all, demote_for_budget

__all__ = ["demote_all", "demote_for_budget", "get_image"]
