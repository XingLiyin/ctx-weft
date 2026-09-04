"""Session —— 会话容器。

状态词表在 `status.py`；本模块只引 `SessionStatus` 做注解。
Session 不持有 Task / Agent 的对象，只持 id（`root_agent_id`），故本模块不依赖
同包的 `task.py` / `agent.py`。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING, Any

from ctx_weft.core.models.status import SessionStatus

if TYPE_CHECKING:
    from ctx_weft.protocols import ContentPart


# ── Session ───────────────────────────────────────────────────────────────────


@dataclass
class Session:
    """会话容器。"""

    id: str
    # 多模态：保 ref 形态（裁定 2026-08-27）。不拍扁——否则事件流重放不出
    # 「曾有一张图」；也不内联字节——见 content_to_event_jsonable（dual-blob-store §6）。
    user_prompt: "str | list[ContentPart]"
    status: SessionStatus
    goal: str = ""
    tenant_id: str = "default"
    root_agent_id: str | None = None

    token_budget: int = 200_000
    token_used: int = 0
    context_limit: int = 180_000
    reserved_output_tokens: int = 8192
    max_concurrent_tasks: int = 8
    max_concurrent_agents: int = 4
    failure_counter: int = 0
    failure_threshold: int = 3

    llm_provider: str | None = None
    llm_model: str | None = None

    config: dict[str, Any] = field(default_factory=dict)
    runtime_summary: dict[str, Any] = field(default_factory=dict)

    created_at: datetime | None = None
    updated_at: datetime | None = None
    finished_at: datetime | None = None
