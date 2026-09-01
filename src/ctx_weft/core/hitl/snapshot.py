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
    """

    pending: dict[str, PendingHitl] = field(default_factory=dict)
    decisions_for: dict[str, tuple[HitlDecision, dict[str, Any] | None]] = field(
        default_factory=dict)
