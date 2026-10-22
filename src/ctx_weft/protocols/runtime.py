"""Host-facing runtime initialization and query values."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Generic, Literal, TypeVar

from ctx_weft.protocols.events import StoredEvent

T = TypeVar("T")


@dataclass(frozen=True)
class QueryResult(Generic[T]):
    data: T
    source: Literal["memory", "events"]
    loaded: bool
    observed_at: datetime
    committed_position: int | None = None


@dataclass(frozen=True)
class InitializationReport:
    """Host-side aggregate of explicitly selected session initialization results."""

    checked_sessions: int
    interrupted_session_ids: list[str]
    interrupted_task_ids: list[str]
    sessions: list[SessionInitializationResult] = field(default_factory=list)


@dataclass(frozen=True)
class SessionStateSummary:
    """Status facts at one committed cut; contains no runtime owners or HITL bodies."""

    root_agent_id: str
    root_agent_status: str | None
    root_task_id: str
    root_task_status: str | None
    has_running_agents: bool
    has_interrupted_agents: bool
    pending_hitl_count: int
    pending_user_turn_hitl_count: int
    waiting_user_turn_agent_id: str


@dataclass(frozen=True)
class SessionInitializationResult:
    """An initialization-time cut, including sessions requiring no new repairs.

    Returned to concurrent first callers only; repeat initialization returns None.
    This is not a live query cache. A fresh runtime reconstructs these results
    even when an earlier process committed repairs but lost its host projection.
    """

    session_id: str
    before_position: int
    committed_position: int
    state: SessionStateSummary
    startup_events: list[StoredEvent] = field(default_factory=list)


@dataclass(frozen=True)
class SessionDetail:
    session_id: str
    root_agent_id: str
    goal: str
    token_budget: int
    created_at: datetime | None


@dataclass(frozen=True)
class TaskDetail:
    task_id: str
    session_id: str
    assigned_agent_id: str | None
    creator_agent_id: str | None
    parent_task_id: str | None
    status: str
    title: str
    description: str
    outputs: Any
    error: str | None
    error_code: str | None
    created_at: datetime | None
    finished_at: datetime | None
