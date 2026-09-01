"""HitlSnapshot：折叠事件得到的、可直接装填进 `HitlRegistry` 的内存态。

恢复是「喂进来」，不是「查回去」：core 的一切 HITL 查询只读内存，装填的完备性
由恢复路径承担（spec §3.1）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ctx_weft.core.hitl.registry import PendingHitl
from ctx_weft.protocols.hitl import HitlDecision


@dataclass
class HitlSnapshot:
    """`pending`：仍未终局的请求。

    `decisions_for`：`tool_call_id → (决定, resume_state)`。**成对**是硬要求——
    冷路径重入调 `resume(ask_id, decision, resume_state, ctx)`，丢掉 resume_state
    就要求 provider 重做让出前的工作（spec §7.2）。只收录**可用**的决定。

    **`decisions_for[*][0].message` 仍是 event 侧引用**：折叠只做到
    `content_from_jsonable`，得到的 `ContentPart` 里若含 blob 引用，那引用落在
    **事件** blob store 的命名空间下，不是记忆 blob store 能打开的。装填进
    `HitlRegistry`/喂给消费方之前，必须先 `hydrate_event_content` 再
    `normalize_content` 写入记忆 blob store，并对失败路径做 `downgrade_images_to_text`
    兜底（该兜底本身绝不可再抛）——现成实现见 `runtime.py:1943` 的
    `_cold_hitl_decision`（spec §12.3.3 第二段）。`fold_hitl_snapshot` 保持同步，
    结构上做不了这一步；这是调用方（下一阶段接 `load_snapshot` 到恢复路径时）的责任。
    """

    pending: dict[str, PendingHitl] = field(default_factory=dict)
    decisions_for: dict[str, tuple[HitlDecision, dict[str, Any] | None]] = field(
        default_factory=dict)
