"""SessionManager：session 创建 + root agent 启动。"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from ctx_weft.core.errors import UnfinishedTasksError
from ctx_weft.protocols.events import EventBus
from ctx_weft.protocols.events import EVENT_TYPES, Event, EventType
from ctx_weft.core.orchestrator.lifecycle_manager import LifecycleManager
from ctx_weft.core.orchestrator.session_state import (
    SessionInput, TERMINAL_SESSION_STATUSES, Transition, next_transition,
)
from ctx_weft.core.orchestrator.task_manager import TaskManager
from ctx_weft.core.state.models import Session, Task
from ctx_weft.core.state.models import NormalTaskSettings
from ctx_weft.core.utils import generate_id, now_utc
from ctx_weft.protocols.context import ProviderContext

if TYPE_CHECKING:
    from ctx_weft.protocols import ContentPart

logger = logging.getLogger(__name__)


def _event_jsonable_or_fallback(
    user_prompt: "str | list[ContentPart]",
    user_prompt_event_jsonable: "str | list[dict] | None",
    where: str,
) -> "str | list[dict] | None":
    """事件侧载荷缺席时的判据，与 `TaskManager.push_task` 完全同形。

    新参数默认 None 是为了不打断既有的纯文本调用方；但「默认 None」若直接写进
    payload，任何忘记传参的调用方都会静默发出 ``user_prompt: None`` 的
    SESSION_CREATED / SESSION_RESUMED——prompt 就此从重放流里消失，正是本次解耦要
    消灭的那类静默降级。故：纯文本回退用它自己（`str`/`None` 本身即 jsonable，与
    `content_to_event_jsonable` 的快路逐字节一致），part 列表则**响亮拒绝**——这里
    没有原始字节可用（`user_prompt` 到这里可能已是 memory ref），拒绝是唯一诚实的选择。
    """
    if user_prompt_event_jsonable is not None:
        return user_prompt_event_jsonable
    if isinstance(user_prompt, list):
        raise ValueError(
            f"{where}: 非纯文本 user_prompt 必须由调用方传 user_prompt_event_jsonable"
            "（由归一化之前的原始 content 算出）——事件库恒不含字节，这里没有原始字节可用"
        )
    return user_prompt


@dataclass
class _SessionState:
    """SM 为每个 session 持有的全部东西——**只有状态本身**。

    没有未决 HITL 集合、没有任务表：那些是 HitlRegistry 和 TaskManager 的，
    SM 需要的结论由 TM 的信号带过来（docs/events-v2.md §2.1.1）。
    """

    status: str = "RUNNING"
    tenant_id: str = "default"


@dataclass
class SessionManager:
    """Coordinates session creation and root task scheduling; owns session status.

    从前是每次调用 new 一个、用完就扔的临时对象（无状态、不订阅事件）——会话状态因此
    无处可放，被 TaskManager / runtime / reducer 各写一份。现在是 runtime 级长生命周期
    组件，`_states` 是会话状态的**唯一住所**（docs/events-v2.md §2.1.1）。
    """

    lifecycle_manager: LifecycleManager
    event_bus: EventBus
    task_max_concurrent: int = 4
    task_max_retries: int = 3
    default_task_timeout_ms: int = 60_000

    #: session_id → 状态。**会话状态的唯一住所。**
    _states: dict[str, _SessionState] = field(default_factory=dict, init=False, repr=False)

    # ── 查询：TaskManager / host 都从这里读，不再各自维护判断 ────────────

    def status_of(self, session_id: str) -> str:
        """当前会话状态；未知 session 返回 `""` 而不是抛——host 会拿任意 id 来问。"""
        st = self._states.get(session_id)
        return st.status if st is not None else ""

    def is_terminal(self, session_id: str) -> bool:
        return self.status_of(session_id) in TERMINAL_SESSION_STATUSES

    # ── 登记 ─────────────────────────────────────────────────────────────

    def register_session(self, session_id: str, *, tenant_id: str = "default") -> None:
        """纳入管理。已存在则保留原状态（重入安全）。"""
        self._states.setdefault(session_id, _SessionState(tenant_id=tenant_id))

    def forget_session(self, session_id: str) -> None:
        """会话彻底收口后释放内存。此后查询返回 `""`，调用方须先读后忘。"""
        self._states.pop(session_id, None)

    # ── 转移：改状态与发事件**只在这里** ──────────────────────────────────

    async def _apply(self, session_id: str, inp: SessionInput, **kw: Any) -> None:
        st = self._states.get(session_id)
        if st is None:
            return
        transition = next_transition(st.status, inp, **kw)
        if transition is None:
            return                      # 不转移就不发事件（否则每条信号都刷前端）
        st.status = transition.status
        await self._emit_session_event(session_id, st, transition)

    async def _emit_session_event(
        self, session_id: str, st: _SessionState, transition: Transition,
    ) -> None:
        await self.event_bus.emit(Event(
            id=generate_id("evt"),
            run_id=None,                 # 会话级事件不属于任何一次 run
            sequence=0,
            session_id=session_id,
            type=transition.event_type,
            timestamp=now_utc(),
            tenant_id=st.tenant_id,
            payload=dict(transition.payload),
        ))

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
        # 先于 instantiate_agent / 任何 emit：被拒的入参不该留下半个 session
        # （同「入口即拒、不落库」）。
        user_prompt_event_jsonable = _event_jsonable_or_fallback(
            user_prompt, user_prompt_event_jsonable, "create_session",
        )

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
        self.register_session(sid, tenant_id=tenant_id)

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
        user_prompt_event_jsonable = _event_jsonable_or_fallback(
            user_prompt, user_prompt_event_jsonable, "resume_session",
        )
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
        self.register_session(session_id, tenant_id=tenant_id)
        self._states[session_id].status = "RUNNING"   # 新一轮：显式回到 RUNNING

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
