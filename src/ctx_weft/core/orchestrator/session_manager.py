"""SessionManager：session 创建 + root agent 启动。"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from ctx_weft.core.errors import UnfinishedTasksError
from ctx_weft.core.events.bus import EventBus
from ctx_weft.core.events.types import EVENT_TYPES, Event, EventType
from ctx_weft.core.orchestrator.lifecycle_manager import LifecycleManager
from ctx_weft.core.orchestrator.task_manager import TaskManager
from ctx_weft.core.state.models import Session, Task
from ctx_weft.core.state.models import NormalTaskSettings
from ctx_weft.core.utils import generate_id, now_utc
from ctx_weft.protocols.context import ProviderContext

if TYPE_CHECKING:
    from ctx_weft.protocols import ContentPart
    from ctx_weft.protocols.events import EventBlobStore

logger = logging.getLogger(__name__)


@dataclass
class SessionManager:
    """Coordinates session creation and root task scheduling."""

    lifecycle_manager: LifecycleManager
    event_bus: EventBus
    task_max_concurrent: int = 4
    task_max_retries: int = 3
    default_task_timeout_ms: int = 60_000
    # 事件侧 blob store。SESSION_CREATED / SESSION_RESUMED / TASK_CREATED 的
    # user_prompt 外部化**已不再经过它**（blob-store 解耦 Task 3：调用方从原始
    # content 算好 event 侧载荷传进来），本字段今天只剩一处用途——原样透传给 root
    # TaskManager，供其 `reopen_task` 的事件外部化（那条路径的内容来自 loop 内部，
    # 不经入口）。CtxWeftRuntime 在 start_session 里按
    # `self.providers.get_event_blob_store()` 注入——SessionManager 本身不持有
    # ProviderRegistry（也不该持有，见 HitlManager.set_content_normalizer 的既有
    # 做法：把「需要什么」注入进来，而不是把整个 registry 塞进构造签名）。
    event_blob_store: "EventBlobStore | None" = None

    async def create_session(
        self,
        template_id: str,
        user_prompt: "str | list[ContentPart]",
        context_limit: int,
        tenant_id: str = "default",
        llm_model: str | None = None,
        llm_account: str | None = None,
        token_budget: int = 200_000,
        reserved_output_tokens: int = 8192,
        session_id: str | None = None,
        initial_task_settings: NormalTaskSettings | None = None,
        user_prompt_event_jsonable: "str | list[dict] | None" = None,
    ) -> tuple[Session, Task, TaskManager]:
        """Create a new session, instantiate root agent, push initial task.

        ``user_prompt_event_jsonable``：调用方（`CtxWeftRuntime.start_session`）由
        **归一化之前的原始** user_prompt 算好的 event 侧载荷（见
        `_validate_and_normalize_content`）。本类不自己算——它手上的 ``user_prompt``
        已经是 memory 侧归一化过的内容，图片 part 是 memory ref，再算一次只会把一个
        event store 打不开的引用写进事件（blob-store 解耦 Task 3）。
        """
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
            llm_model=llm_model or "",
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
        # 保 ref、不落字节、不拍扁（裁定 2026-08-27）——本事件参与状态重建（reducers
        # 的 SESSION_CREATED 分支），拍扁会让重放后「曾有一张图」无痕。
        await self._emit(EventType.SESSION_CREATED, sid, tenant_id, timestamp=ts, payload={
            "template_id": template_id,
            "user_prompt": user_prompt_event_jsonable,
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

        root_task, task_manager = await self._make_root_task_manager(
            session, user_prompt, initial_task_settings, user_prompt_event_jsonable,
        )

        return session, root_task, task_manager

    async def resume_session(
        self,
        session_id: str,
        event_store: Any,
        user_prompt: "str | list[ContentPart]",
        tenant_id: str = "default",
        llm_model: str | None = None,
        llm_account: str | None = None,
        initial_task_settings: NormalTaskSettings | None = None,
        user_prompt_event_jsonable: "str | list[dict] | None" = None,
    ) -> tuple[Session, Task, TaskManager]:
        """Resume an existing session: recover root_agent_id from event store, push a new root task.

        ``user_prompt_event_jsonable`` 同 `create_session`：由调用方从原始 content 算好。"""
        from ctx_weft.core.control.reducers import rebuild_view
        view = await rebuild_view(event_store, session_id)
        sess_proj = view.sessions.get(session_id)
        if not sess_proj or not sess_proj.root_agent_id:
            raise RuntimeError(
                f"Cannot resume session {session_id!r}: "
                "no SessionCreated event found in event store."
            )

        # 弃轮禁止：仍有未终结任务时不许开新轮（本方法只建带新 root task 的全新 TM,滞留
        # 任务会被无声遗弃,之后 recover_session 全量重建又把它们复活重跑）。调用方应走
        # 恢复路径续跑/收尾。辅助任务（compact/metadata）豁免——restore 也从不重排它们,
        # 阻塞会把会话永久锁死（与 TaskManager.restore 的跳过口径一致）。
        unfinished = [
            tid for tid, t in view.tasks.items()
            if t.status not in ("FINISHED", "FAILED", "CANCELED")
            and (t.settings_raw or {}).get("_type") not in (
                "CompactTaskSettings", "MetadataFillerTaskSettings")
        ]
        if unfinished:
            raise UnfinishedTasksError(session_id, unfinished)

        session = Session(
            id=session_id,
            user_prompt=user_prompt,
            status="RUNNING",
            tenant_id=tenant_id,
            root_agent_id=sess_proj.root_agent_id,
            llm_provider=llm_account or "",
            llm_model=llm_model or "",
            context_limit=sess_proj.context_limit,
            reserved_output_tokens=getattr(sess_proj, "reserved_output_tokens", 8192),
            created_at=now_utc(),
        )
        root_task, task_manager = await self._make_root_task_manager(
            session, user_prompt, initial_task_settings, user_prompt_event_jsonable,
        )

        logger.info("Session %s resumed (agent=%s)", session_id, sess_proj.root_agent_id)

        # 同 SESSION_CREATED：保 ref、不落字节、不拍扁。
        await self._emit(EventType.SESSION_RESUMED, session_id, tenant_id, payload={
            "user_prompt": user_prompt_event_jsonable,
            "root_agent_id": sess_proj.root_agent_id,
            "llm_model": llm_model or "",
            "llm_account": llm_account or "",
        })

        return session, root_task, task_manager

    # ── Private helpers ───────────────────────────────────────────────────────

    async def _make_root_task_manager(
        self,
        session: Session,
        user_prompt: "str | list[ContentPart]",
        settings: NormalTaskSettings | None,
        user_prompt_event_jsonable: "str | list[dict] | None" = None,
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
        # push_task 在这里立即发 TASK_CREATED（先于 runtime._register_and_drain 的晚
        # 绑定），故本 TaskManager 的 event_blob_store 必须现在就接上，直接透传
        # SessionManager 自己持有的那份（同一个 registry 解出的同一个 store）。
        task_manager.set_event_blob_store(self.event_blob_store)
        # root task 的 user_prompt 与 SESSION_CREATED 是同一份内容，故 event 侧载荷
        # 也是同一份——同样由调用方从原始 content 算好，不在这里重算（Task 3）。
        await task_manager.push_task(
            task, user_prompt_event_jsonable=user_prompt_event_jsonable,
        )
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
