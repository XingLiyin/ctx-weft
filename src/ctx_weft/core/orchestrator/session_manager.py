"""SessionManager：session 创建 + root agent 启动。"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from ctx_weft.core.events.bus import EventBus
from ctx_weft.core.events.types import EVENT_TYPES, Event, EventType
from ctx_weft.core.orchestrator.lifecycle_manager import LifecycleManager
from ctx_weft.core.orchestrator.task_manager import TaskManager
from ctx_weft.core.state.models import Session, Task
from ctx_weft.core.state.models import NormalTaskSettings
from ctx_weft.core.utils import generate_id, now_utc
from ctx_weft.protocols.context import ProviderContext

logger = logging.getLogger(__name__)


@dataclass
class SessionManager:
    """Coordinates session creation and root task scheduling."""

    lifecycle_manager: LifecycleManager
    event_bus: EventBus
    task_max_concurrent: int = 4
    task_max_retries: int = 3
    default_task_timeout_ms: int = 60_000

    async def create_session(
        self,
        template_id: str,
        user_prompt: str,
        context_limit: int,
        tenant_id: str = "default",
        llm_model: str | None = None,
        llm_account: str | None = None,
        token_budget: int = 200_000,
        reserved_output_tokens: int = 8192,
        session_id: str | None = None,
        initial_task_settings: NormalTaskSettings | None = None,
    ) -> tuple[Session, Task, TaskManager]:
        """Create a new session, instantiate root agent, push initial task."""
        sid = session_id or generate_id("ses")
        ctx = ProviderContext(session_id=sid, tenant_id=tenant_id)

        agent, template = await self.lifecycle_manager.instantiate_agent(
            template_id=template_id, session_id=sid, tenant_id=tenant_id, ctx=ctx,
        )

        session = Session(
            id=sid,
            user_prompt=user_prompt,
            status="RUNNING",
            tenant_id=tenant_id,
            root_agent_id=agent.id,
            llm_provider=llm_account or "",
            context_limit=context_limit,
            reserved_output_tokens=reserved_output_tokens,
            created_at=now_utc(),
        )
        logger.info("Session %s created (template=%s, agent=%s)", sid, template_id, agent.id)

        # Emit in causal order Session → Agent → Task. TASK_CREATED (from
        # _make_root_task_manager → push_task) MUST follow SESSION_CREATED: the host
        # projection inserts the task row with a FK on tasks.session_id → sessions.id,
        # so the session row must be projected first.
        ts = now_utc()
        await self._emit(EventType.SESSION_CREATED, sid, tenant_id, timestamp=ts, payload={
            "template_id": template_id,
            "user_prompt": user_prompt,
            "root_agent_id": agent.id,
            "llm_model": llm_model or "",
            "llm_account": llm_account or "",
            "tenant_id": tenant_id,
            "token_budget": token_budget,
            "context_limit": context_limit,
            "reserved_output_tokens": reserved_output_tokens,
        })
        await self._emit(EventType.AGENT_INSTANTIATED, sid, tenant_id, timestamp=ts, agent_id=agent.id, payload={
            "template_id": template.id,
            "template_version": template.version,
        })

        root_task, task_manager = await self._make_root_task_manager(session, user_prompt, initial_task_settings)

        return session, root_task, task_manager

    async def resume_session(
        self,
        session_id: str,
        event_store: Any,
        user_prompt: str,
        tenant_id: str = "default",
        llm_model: str | None = None,
        llm_account: str | None = None,
        initial_task_settings: NormalTaskSettings | None = None,
    ) -> tuple[Session, Task, TaskManager]:
        """Resume an existing session: recover root_agent_id from event store, push a new root task."""
        from ctx_weft.core.control.reducers import rebuild_view
        view = await rebuild_view(event_store, session_id)
        sess_proj = view.sessions.get(session_id)
        if not sess_proj or not sess_proj.root_agent_id:
            raise RuntimeError(
                f"Cannot resume session {session_id!r}: "
                "no SessionCreated event found in event store."
            )

        session = Session(
            id=session_id,
            user_prompt=user_prompt,
            status="RUNNING",
            tenant_id=tenant_id,
            root_agent_id=sess_proj.root_agent_id,
            llm_provider=llm_account or "",
            context_limit=sess_proj.context_limit,
            reserved_output_tokens=getattr(sess_proj, "reserved_output_tokens", 8192),
            created_at=now_utc(),
        )
        root_task, task_manager = await self._make_root_task_manager(session, user_prompt, initial_task_settings)

        logger.info("Session %s resumed (agent=%s)", session_id, sess_proj.root_agent_id)

        await self._emit(EventType.SESSION_RESUMED, session_id, tenant_id, payload={
            "user_prompt": user_prompt,
            "root_agent_id": sess_proj.root_agent_id,
            "llm_model": llm_model or "",
            "llm_account": llm_account or "",
        })

        return session, root_task, task_manager

    # ── Private helpers ───────────────────────────────────────────────────────

    async def _make_root_task_manager(
        self,
        session: Session,
        user_prompt: str,
        settings: NormalTaskSettings | None,
    ) -> tuple[Task, TaskManager]:
        task = Task(
            id=generate_id("tsk"),
            session_id=session.id,
            status="ACTIVE",
            tenant_id=session.tenant_id,
            assigned_agent_id=session.root_agent_id,
            creator_agent_id=session.root_agent_id,
            title="",
            description="",
            user_prompt=user_prompt,
            settings=settings or NormalTaskSettings(),
            # root task = 用户对话：actor 纯文本即暂停等下一条用户消息（非自动完成）。
            interaction_mode="interactive",
            timeout_ms=self.default_task_timeout_ms,
            created_at=now_utc(),
        )
        task_manager = TaskManager(
            session_id=session.id,
            event_bus=self.event_bus,
            max_concurrent=self.task_max_concurrent,
            task_max_retries=self.task_max_retries,
        )
        await task_manager.push_task(task)
        return task, task_manager

    async def _emit(
        self,
        event_type: EventType,
        session_id: str,
        tenant_id: str,
        payload: dict,
        agent_id: str | None = None,
        timestamp=None,
    ) -> None:
        # 与 make_event 一致的白名单校验：直接构造 Event 的路径此前会绕过它。
        if event_type not in EVENT_TYPES:
            raise ValueError(f"Unknown event type: {event_type}; not in EVENT_TYPES")
        await self.event_bus.emit(Event(
            id=generate_id("evt"),
            run_id=None,
            sequence=0,
            session_id=session_id,
            type=event_type,
            timestamp=timestamp or now_utc(),
            tenant_id=tenant_id,
            agent_id=agent_id,
            payload=payload,
        ))
