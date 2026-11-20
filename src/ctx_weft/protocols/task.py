"""Public task query values, shared by individual and session queries."""

from dataclasses import dataclass
from datetime import datetime
from typing import Any
from ctx_weft.protocols.content import ContentPart


@dataclass(frozen=True)
class TaskIdPage:
    """Committed task identities in creation order; details are queried separately."""

    ids: tuple[str, ...]
    next_cursor: str | None
    committed_position: int


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
    # Count recorded by the last failure, independent of the live retry budget.
    failure_retry_count: int | None = None
    blocked_by_task_id: str | None = None
    finalized: bool = False
    user_prompt: str | list[ContentPart] | None = None
    settings_type: str = ""
