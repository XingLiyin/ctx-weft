"""Conversion helpers: Projection → core state models.

Used by resume_session to rebuild Session/Task objects from event replay.
"""

from __future__ import annotations

from loomex_core.core.control.types import SessionView, TaskView
from loomex_core.core.state.models import Session, Task
from loomex_core.core.state.models import deserialize_settings


def session_from_projection(proj: SessionView) -> Session:
    """Rebuild a Session dataclass from its event-sourced projection."""
    return Session(
        id=proj.id,
        user_prompt=proj.user_prompt,
        status=proj.status,  # type: ignore[arg-type]
        goal=proj.goal,
        tenant_id=proj.tenant_id,
        root_agent_id=proj.root_agent_id or None,
        token_budget=proj.token_budget,
        context_limit=proj.context_limit,
        failure_counter=proj.failure_counter,
        llm_provider=proj.llm_account or None,
        llm_model=proj.llm_model or None,
        config={"template_id": proj.template_id},
        created_at=proj.created_at,
    )


def task_from_projection(proj: TaskView) -> Task:
    """Rebuild a Task dataclass from its event-sourced projection."""
    # Tasks that were already executing (ACTIVE or SUSPENDED) had their user
    # prompt ingested into memory before the crash; mark it so the driver
    # doesn't ingest it a second time on recovery.
    prompt_in_memory = bool(proj.user_prompt) and proj.status in ("ACTIVE", "SUSPENDED")
    return Task(
        id=proj.id,
        session_id=proj.session_id,
        status=proj.status,  # type: ignore[arg-type]
        tenant_id=proj.tenant_id,
        title=proj.title,
        description=proj.description,
        user_prompt=proj.user_prompt or None,
        original_user_prompt=proj.original_user_prompt or None,
        assigned_agent_id=proj.assigned_agent_id or None,
        creator_agent_id=proj.creator_agent_id or None,
        parent_task_id=proj.parent_task_id or None,
        dag_deps=list(proj.dag_deps),
        priority=proj.priority,
        max_retries=proj.max_retries,
        timeout_ms=proj.timeout_ms,
        settings=deserialize_settings(proj.settings_raw),
        interaction_mode=proj.interaction_mode,  # type: ignore[arg-type]
        outputs=proj.outputs,
        error=proj.error,
        created_at=proj.created_at,
        finished_at=proj.finished_at,
        user_prompt_in_memory=prompt_in_memory,
    )
