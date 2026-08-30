"""Conversion helpers: Projection → core state models.

Used by resume_session to rebuild Session/Task objects from event replay.
"""

from __future__ import annotations

from ctx_weft.core.control.types import AgentView, SessionView, TaskView
from ctx_weft.core.state.models import Agent, Session, Task
from ctx_weft.core.state.models import deserialize_settings
from ctx_weft.core.utils import as_utc


def _as_utc_opt(dt):
    """归一可选 datetime 为 aware(UTC)；None 透传。事件重放 / DB 反序列化可能丢 tz，
    此处在重建 core 模型的边界统一补齐，使内存态 created_at 恒 aware——与 now_utc()
    新建的活任务同 tz，避免二者混入同一注册表后排序抛 naive/aware 比较错。"""
    return as_utc(dt) if dt is not None else None


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
        reserved_output_tokens=getattr(proj, "reserved_output_tokens", 8192),
        failure_counter=proj.failure_counter,
        llm_provider=proj.llm_account or None,
        llm_model=proj.llm_model or None,
        config={"template_id": proj.template_id},
        created_at=_as_utc_opt(proj.created_at),
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
        origin_tool_call_id=proj.origin_tool_call_id or None,
        origin_tool_name=proj.origin_tool_name or None,
        outputs=proj.outputs,
        error=proj.error,
        created_at=_as_utc_opt(proj.created_at),
        finished_at=_as_utc_opt(proj.finished_at),
        user_prompt_in_memory=prompt_in_memory,
    )


def agents_from_projection(
    agent_views: dict[str, AgentView],
    *,
    session_id: str,
    tenant_id: str,
    fallback_template_id: str,
    fallback_template_version: str,
) -> dict[str, Agent]:
    """从投影重建 agent 实例（冷 resume 的 pre_resolved 种子）。

    `template_id` 优先取 AgentView 自己的——它来自 `AgentInstantiated` 事件，是事件流里
    唯一记录 agent 出身的地方（树形推算得不出模板）。存量事件流里子 agent 没发过该事件，
    投影中该字段为空 → 回落 session 模板，与本函数抽出前的行为逐字一致，零数据迁移。

    回落而非报错是刻意的：授权按模板做策略，重启后把未知模板判成"无权限"会让老会话
    直接跑不动；沿用旧行为至少与重启前一致。
    """
    return {
        av.id: Agent(
            id=av.id,
            session_id=session_id,
            template_id=av.template_id or fallback_template_id,
            template_version=fallback_template_version,
            status="IDLE",
            tenant_id=tenant_id,
            spawn_depth=av.spawn_depth,
            parent_agent_id=av.parent_agent_id,
        )
        for av in agent_views.values()
    }
