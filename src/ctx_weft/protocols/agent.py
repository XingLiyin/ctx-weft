"""agent 发现接口的 host-facing 视图类型（spec 5）。

层级关系不在接口层嵌套——各条自带 parent_agent_id，调用方按需还原成树。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True)
class AgentSummary:
    agent_id: str
    parent_agent_id: str | None
    status: str
    current_task_id: str | None
    spawn_depth: int
    created_at: datetime | None = None


@dataclass(frozen=True)
class AgentDetail:
    agent_id: str
    parent_agent_id: str | None
    status: str
    current_task_id: str | None
    spawn_depth: int
    session_id: str
    template_id: str
    created_at: datetime | None = None
    current_task_status: str | None = None
