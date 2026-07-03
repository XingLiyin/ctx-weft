"""Control plane types.

Phase 6 §6.7.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any


@dataclass
class SessionView:
    """Session 的轻量投影，从事件流 reduce 而来。"""

    id: str
    user_prompt: str = ""
    template_id: str = ""
    status: str = "RUNNING"
    goal: str = ""
    root_agent_id: str = ""
    llm_model: str = ""
    llm_account: str = ""
    tenant_id: str = "default"
    token_budget: int = 200_000
    context_limit: int = 180_000
    reserved_output_tokens: int = 8192
    failure_counter: int = 0
    created_at: datetime | None = None


@dataclass
class TaskView:
    """Task 的轻量投影，从事件流 reduce 而来。"""

    id: str
    session_id: str
    status: str = "PENDING"
    title: str = ""
    description: str = ""
    assigned_agent_id: str = ""
    creator_agent_id: str = ""
    parent_task_id: str = ""
    user_prompt: str = ""
    original_user_prompt: str = ""  # reopen 重写前的原始 prompt 快照（防多轮累加，跨重启保留）
    interaction_mode: str = "auto"  # interactive=纯文本暂停等用户 / auto=自治（跨重启保留，否则 resume 后丢失暂停语义）
    settings_raw: dict[str, Any] = field(default_factory=dict)
    dag_deps: list[str] = field(default_factory=list)
    priority: int = 5
    max_retries: int = 3
    timeout_ms: int = 60_000
    tenant_id: str = "default"
    outputs: Any = None
    error: str | None = None
    created_at: datetime | None = None
    finished_at: datetime | None = None


@dataclass
class AgentView:
    """Agent 的轻量投影，从 task 层级推算而来。"""

    id: str
    spawn_depth: int = 0
    parent_agent_id: str | None = None


@dataclass
class HitlRequestView:
    """未解决 HITL 请求的轻量投影（恢复用，spec/07 §9）。仅 pending 留存。"""

    id: str
    kind: str = "approval"        # approval | input
    session_id: str = ""
    task_id: str = ""
    capability_id: str = ""
    tool_call_id: str = ""
    question: str = ""
    context: str = ""


@dataclass
class RunStateView:
    """Point-in-time view of a run's state for inspect/replay."""

    run_id: str
    session_id: str
    task_id: str
    agent_id: str

    current_step: str | None = None
    task_status: str = "UNKNOWN"
    session_status: str = "UNKNOWN"

    assembled_prompt_tokens: int = 0
    transcript_turns: int = 0
    events_total: int = 0

    target_event_id: str | None = None
    events_replayed: int = 0

    extra: dict[str, Any] = field(default_factory=dict)

    snapshot_at: datetime | None = None

    # Full projections rebuilt from events
    sessions: dict[str, SessionView] = field(default_factory=dict)
    tasks: dict[str, TaskView] = field(default_factory=dict)
    agents: dict[str, AgentView] = field(default_factory=dict)

    # Pending HITL requests folded from events (only unresolved; spec/07 §9)
    pending_hitl: dict[str, "HitlRequestView"] = field(default_factory=dict)
