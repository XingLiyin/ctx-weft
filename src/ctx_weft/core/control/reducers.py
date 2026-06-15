"""Event reducers：把 event 序列重建成 state。

Phase 6 §6.6。核心是 reduce(events) → RunStateView。
RunStateView.sessions / .tasks 包含完整的 Session/Task 投影。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from ctx_weft.core.control.types import AgentView, HitlRequestView, RunStateView, SessionView, TaskView
from ctx_weft.core.events import TASK_STATUS_BY_EVENT, Event, EventType

# HITL 状态相关事件（请求 + 各终态）。供"取该 session 待解决 HITL"的轻查询折叠用，
# 与下面 _apply 的 pending_hitl 折叠语义一致（单一真相）。
_HITL_RESOLVE_TYPES = (
    EventType.HITL_APPROVED, EventType.HITL_MODIFIED, EventType.HITL_ANSWERED,
    EventType.HITL_REJECTED, EventType.HITL_CANCELLED,
)
HITL_STATUS_EVENT_TYPES: tuple[EventType, ...] = (EventType.HITL_REQUIRED, *_HITL_RESOLVE_TYPES)


def fold_pending_hitl(events: list[Event]) -> dict[str, HitlRequestView]:
    """折叠 HITL 事件 → 仍未解决的 {approval_id: HitlRequestView}（HitlRequired 减去各终态）。

    只需 HITL_STATUS_EVENT_TYPES 这几类事件即可,无需全量回放——崩溃恢复据此既判某 session 是
    "等人答复 / 崩溃中断",又(等人答复时)直接重建内存 HitlManager（spec/07 §9）。
    """
    pending: dict[str, HitlRequestView] = {}
    for ev in events:
        p = ev.payload or {}
        rid = p.get("approval_id", "")
        if not rid:
            continue
        if ev.type == EventType.HITL_REQUIRED:
            pending[rid] = HitlRequestView(
                id=rid, kind=p.get("kind", "approval"),
                session_id=ev.session_id, task_id=ev.task_id or "",
                capability_id=p.get("capability_id", ""), tool_call_id=p.get("tool_call_id", ""),
                question=p.get("question", ""), context=p.get("context", ""),
            )
        elif ev.type in _HITL_RESOLVE_TYPES:
            pending.pop(rid, None)
    return pending


def unresolved_hitl_ids(events: list[Event]) -> set[str]:
    """仍未解决的 approval_id 集合（fold_pending_hitl 的键）。"""
    return set(fold_pending_hitl(events))


def apply_events(events: list[Event], view: RunStateView) -> RunStateView:
    """在已有 RunStateView 上增量应用事件列表（快照恢复后的 delta replay）。"""
    for ev in events:
        if not view.session_id:
            view.session_id = ev.session_id
        if not view.task_id and ev.task_id:
            view.task_id = ev.task_id
        if not view.agent_id and ev.agent_id:
            view.agent_id = ev.agent_id
        view.events_total += 1
        _apply(view, ev)
    _rebuild_agents(view)
    return view


def serialize_view(view: RunStateView) -> dict[str, Any]:
    """把 RunStateView 序列化为可 JSON 存储的 dict（用于快照写入）。"""
    def _dt(d: datetime | None) -> str | None:
        return d.isoformat() if d is not None else None

    return {
        "run_id": view.run_id,
        "session_id": view.session_id,
        "task_id": view.task_id,
        "agent_id": view.agent_id,
        "current_step": view.current_step,
        "task_status": view.task_status,
        "session_status": view.session_status,
        "assembled_prompt_tokens": view.assembled_prompt_tokens,
        "transcript_turns": view.transcript_turns,
        "events_total": view.events_total,
        "sessions": {
            sid: {
                "id": s.id,
                "user_prompt": s.user_prompt,
                "template_id": s.template_id,
                "status": s.status,
                "goal": s.goal,
                "root_agent_id": s.root_agent_id,
                "llm_model": s.llm_model,
                "llm_account": s.llm_account,
                "tenant_id": s.tenant_id,
                "token_budget": s.token_budget,
                "context_limit": s.context_limit,
                "failure_counter": s.failure_counter,
                "created_at": _dt(s.created_at),
            }
            for sid, s in view.sessions.items()
        },
        "tasks": {
            tid: {
                "id": t.id,
                "session_id": t.session_id,
                "status": t.status,
                "title": t.title,
                "description": t.description,
                "assigned_agent_id": t.assigned_agent_id,
                "creator_agent_id": t.creator_agent_id,
                "parent_task_id": t.parent_task_id,
                "user_prompt": t.user_prompt,
                "original_user_prompt": t.original_user_prompt,
                "interaction_mode": t.interaction_mode,
                "settings_raw": t.settings_raw,
                "dag_deps": t.dag_deps,
                "priority": t.priority,
                "max_retries": t.max_retries,
                "timeout_ms": t.timeout_ms,
                "tenant_id": t.tenant_id,
                "outputs": t.outputs,
                "error": t.error,
                "created_at": _dt(t.created_at),
                "finished_at": _dt(t.finished_at),
            }
            for tid, t in view.tasks.items()
        },
        "agents": {
            aid: {
                "id": a.id,
                "spawn_depth": a.spawn_depth,
                "parent_agent_id": a.parent_agent_id,
            }
            for aid, a in view.agents.items()
        },
        "pending_hitl": {
            rid: {
                "id": h.id, "kind": h.kind, "session_id": h.session_id,
                "task_id": h.task_id, "capability_id": h.capability_id,
                "tool_call_id": h.tool_call_id, "question": h.question, "context": h.context,
            }
            for rid, h in view.pending_hitl.items()
        },
    }


def deserialize_view(data: dict[str, Any]) -> RunStateView:
    """从快照 dict 还原 RunStateView（用于快照读取后恢复）。"""
    def _dt(s: str | None) -> datetime | None:
        return datetime.fromisoformat(s) if s else None

    sessions: dict[str, SessionView] = {}
    for sid, s in data.get("sessions", {}).items():
        sessions[sid] = SessionView(
            id=s["id"],
            user_prompt=s.get("user_prompt", ""),
            template_id=s.get("template_id", ""),
            status=s.get("status", "UNKNOWN"),
            goal=s.get("goal", ""),
            root_agent_id=s.get("root_agent_id", ""),
            llm_model=s.get("llm_model", ""),
            llm_account=s.get("llm_account", ""),
            tenant_id=s.get("tenant_id", "default"),
            token_budget=s.get("token_budget", 200_000),
            context_limit=s.get("context_limit", 180_000),
            failure_counter=s.get("failure_counter", 0),
            created_at=_dt(s.get("created_at")),
        )

    tasks: dict[str, TaskView] = {}
    for tid, t in data.get("tasks", {}).items():
        tasks[tid] = TaskView(
            id=t["id"],
            session_id=t.get("session_id", ""),
            status=t.get("status", "UNKNOWN"),
            title=t.get("title", ""),
            description=t.get("description", ""),
            assigned_agent_id=t.get("assigned_agent_id", ""),
            creator_agent_id=t.get("creator_agent_id", ""),
            parent_task_id=t.get("parent_task_id", ""),
            user_prompt=t.get("user_prompt", ""),
            original_user_prompt=t.get("original_user_prompt", ""),
            interaction_mode=t.get("interaction_mode", "auto"),
            settings_raw=t.get("settings_raw", {}),
            dag_deps=t.get("dag_deps", []),
            priority=t.get("priority", 5),
            max_retries=t.get("max_retries", 3),
            timeout_ms=t.get("timeout_ms", 60_000),
            tenant_id=t.get("tenant_id", "default"),
            outputs=t.get("outputs"),
            error=t.get("error"),
            created_at=_dt(t.get("created_at")),
            finished_at=_dt(t.get("finished_at")),
        )

    agents: dict[str, AgentView] = {}
    for aid, a in data.get("agents", {}).items():
        agents[aid] = AgentView(
            id=a["id"],
            spawn_depth=a.get("spawn_depth", 0),
            parent_agent_id=a.get("parent_agent_id"),
        )

    from ctx_weft.core.control.types import HitlRequestView
    pending_hitl: dict[str, HitlRequestView] = {}
    for rid, h in data.get("pending_hitl", {}).items():
        pending_hitl[rid] = HitlRequestView(
            id=h["id"], kind=h.get("kind", "approval"), session_id=h.get("session_id", ""),
            task_id=h.get("task_id", ""), capability_id=h.get("capability_id", ""),
            tool_call_id=h.get("tool_call_id", ""), question=h.get("question", ""),
            context=h.get("context", ""),
        )

    return RunStateView(
        run_id=data.get("run_id", ""),
        session_id=data.get("session_id", ""),
        task_id=data.get("task_id", ""),
        agent_id=data.get("agent_id", ""),
        current_step=data.get("current_step"),
        task_status=data.get("task_status", "UNKNOWN"),
        session_status=data.get("session_status", "UNKNOWN"),
        assembled_prompt_tokens=data.get("assembled_prompt_tokens", 0),
        transcript_turns=data.get("transcript_turns", 0),
        events_total=data.get("events_total", 0),
        sessions=sessions,
        tasks=tasks,
        agents=agents,
        pending_hitl=pending_hitl,
    )


async def rebuild_view(event_store: Any, session_id: str) -> RunStateView:
    """Rebuild RunStateView via snapshot + delta, or full replay as fallback.

    Works with any EventStore; snapshot methods are optional (NotImplementedError → full replay).
    """
    try:
        snapshot = await event_store.load_latest_snapshot(session_id)
    except NotImplementedError:
        snapshot = None

    if snapshot:
        view = deserialize_view(snapshot.state_blob)
        delta = await event_store.read_after(session_id, snapshot.last_event_id)
        return apply_events(delta, view)

    events = await event_store.read_by_session(session_id)
    return reduce_events(events, run_id=session_id)


def reduce_events(events: list[Event], run_id: str) -> RunStateView:
    """Replay event list into a RunStateView."""
    view = RunStateView(
        run_id=run_id,
        session_id="",
        task_id="",
        agent_id="",
    )

    for ev in events:
        if not view.session_id:
            view.session_id = ev.session_id
        if not view.task_id and ev.task_id:
            view.task_id = ev.task_id
        if not view.agent_id and ev.agent_id:
            view.agent_id = ev.agent_id

        view.events_total += 1
        _apply(view, ev)

    _rebuild_agents(view)
    return view


def _rebuild_agents(view: RunStateView) -> None:
    """从 session/task 层级推算所有 AgentView（含 spawn_depth）。"""
    # Root agents from sessions
    for sess in view.sessions.values():
        if sess.root_agent_id and sess.root_agent_id not in view.agents:
            view.agents[sess.root_agent_id] = AgentView(
                id=sess.root_agent_id,
                spawn_depth=0,
                parent_agent_id=None,
            )

    # Subagent tasks sorted by creation time (parent tasks precede children in event stream)
    ordered = sorted(view.tasks.values(), key=lambda t: t.created_at or datetime.min)
    for task in ordered:
        aid = task.assigned_agent_id
        if not aid or aid in view.agents:
            continue
        use_subagent = task.settings_raw.get("use_subagent", False)
        creator = task.creator_agent_id
        if use_subagent and creator and creator in view.agents:
            depth = view.agents[creator].spawn_depth + 1
        else:
            depth = 0
        view.agents[aid] = AgentView(
            id=aid,
            spawn_depth=depth,
            parent_agent_id=creator or None,
        )


def _apply(view: RunStateView, ev: Event) -> None:
    t = ev.type
    p: dict[str, Any] = ev.payload or {}

    # ── Run / Step ────────────────────────────────────────────────────────────
    if t == EventType.STEP_STARTED:
        view.current_step = p.get("step_name")
    elif t == EventType.STEP_COMPLETED:
        view.current_step = p.get("next_step")
    elif t == EventType.RUN_STARTED:
        view.task_status = "ACTIVE"
        view.session_status = "RUNNING"
    elif t == EventType.RUN_FINISHED:
        view.session_status = p.get("final_status", "FINISHED")

    # ── Session projection ────────────────────────────────────────────────────
    elif t == EventType.SESSION_CREATED:
        sess = SessionView(
            id=ev.session_id,
            user_prompt=p.get("user_prompt", ""),
            template_id=p.get("template_id", ""),
            root_agent_id=p.get("root_agent_id", ""),
            llm_model=p.get("llm_model", ""),
            llm_account=p.get("llm_account", ""),
            tenant_id=p.get("tenant_id", ev.tenant_id),
            token_budget=p.get("token_budget", 200_000),
            context_limit=p.get("context_limit", 180_000),
            status="RUNNING",
            created_at=ev.timestamp,
        )
        view.sessions[ev.session_id] = sess
        view.session_status = "RUNNING"

    elif t == EventType.SESSION_RESUMED:
        sess = view.sessions.get(ev.session_id)
        if sess is not None:
            sess.user_prompt = p.get("user_prompt", sess.user_prompt)
            sess.status = "RUNNING"
        view.session_status = "RUNNING"

    elif t == EventType.SESSION_STATUS_CHANGED:
        new_status = p.get("new_status", "")
        if new_status:
            view.session_status = new_status
            sess = view.sessions.get(ev.session_id)
            if sess is not None:
                sess.status = new_status

    elif t == EventType.SESSION_FINISHED:
        final_status = p.get("final_status", "SUCCEEDED")
        view.session_status = final_status
        sess = view.sessions.get(ev.session_id)
        if sess is not None:
            sess.status = final_status

    elif t == EventType.RECOGNIZE_INTENT_TOOL_CALL:
        goal = p.get("session_goal", "")
        if goal:
            sess = view.sessions.get(ev.session_id)
            if sess is not None:
                sess.goal = goal

    elif t == EventType.FAILURE_THRESHOLD_HIT:
        sess = view.sessions.get(ev.session_id)
        if sess is not None:
            sess.failure_counter += 1

    # ── Task projection ───────────────────────────────────────────────────────
    elif t == EventType.TASK_CREATED:
        task_data: dict = p.get("task", {})
        task_id = task_data.get("id") or ev.task_id
        if task_id:
            task = TaskView(
                id=task_id,
                session_id=ev.session_id,
                status=task_data.get("status", "PENDING"),
                title=task_data.get("title", ""),
                description=task_data.get("description", ""),
                assigned_agent_id=task_data.get("assigned_agent_id", ""),
                creator_agent_id=task_data.get("creator_agent_id", ""),
                parent_task_id=task_data.get("parent_task_id", ""),
                user_prompt=task_data.get("user_prompt", ""),
                interaction_mode=task_data.get("interaction_mode", "auto"),
                settings_raw=task_data.get("settings", {}),
                dag_deps=task_data.get("dag_deps", []),
                priority=task_data.get("priority", 5),
                max_retries=task_data.get("max_retries", 3),
                timeout_ms=task_data.get("timeout_ms", 60_000),
                tenant_id=ev.tenant_id or "default",
                created_at=ev.timestamp,
            )
            view.tasks[task_id] = task
            if not view.task_id:
                view.task_id = task_id

    elif t == EventType.TASK_REQUEUED and ev.task_id:
        # 重排（observer active 或 review reopen）：状态回 PENDING、清旧产出；
        # reopen 还会携带改写后的 user_prompt / 原始 prompt 快照，replay 时一并恢复。
        task = view.tasks.get(ev.task_id)
        if task is not None:
            task.status = "PENDING"
            task.outputs = None
            up = p.get("user_prompt")
            if up is not None:
                task.user_prompt = up
            oup = p.get("original_user_prompt")
            if oup is not None:
                task.original_user_prompt = oup
        view.task_status = "PENDING"

    elif t in TASK_STATUS_BY_EVENT and ev.task_id:
        task = view.tasks.get(ev.task_id)
        if task is not None:
            task.status = TASK_STATUS_BY_EVENT[t]
            if t == EventType.TASK_STARTED:
                assigned = p.get("assigned_agent_id", "")
                if assigned:
                    task.assigned_agent_id = assigned
        view.task_status = TASK_STATUS_BY_EVENT[t]

    elif t == EventType.TASK_FINALIZED and ev.task_id:
        task = view.tasks.get(ev.task_id)
        if task is not None:
            task.outputs = p.get("outputs")
            task.error = p.get("error")
            task.finished_at = ev.timestamp

    elif t == EventType.BLACKBOARD_PUBLISHED and ev.task_id:
        task = view.tasks.get(ev.task_id)
        if task is not None:
            task.title = p.get("title", task.title)
            task.description = p.get("description", task.description)

    # ── LLM / Context ─────────────────────────────────────────────────────────
    elif t == EventType.PREPARE_COMPLETED:
        view.assembled_prompt_tokens = p.get("assembled_token_count", 0)
    elif t == EventType.ACT_TURN_COMPLETED:
        view.transcript_turns = p.get("turn", view.transcript_turns)

    # ── HITL projection (spec/07 §9) ───────────────────────────────────────────
    elif t == EventType.HITL_REQUIRED:
        from ctx_weft.core.control.types import HitlRequestView
        rid = p.get("approval_id", "")
        if rid:
            view.pending_hitl[rid] = HitlRequestView(
                id=rid,
                kind=p.get("kind", "approval"),
                session_id=ev.session_id,
                task_id=ev.task_id or "",
                capability_id=p.get("capability_id", ""),
                tool_call_id=p.get("tool_call_id", ""),
                question=p.get("question", ""),
                context=p.get("context", ""),
            )
    elif t in (
        EventType.HITL_APPROVED, EventType.HITL_MODIFIED, EventType.HITL_ANSWERED,
        EventType.HITL_REJECTED, EventType.HITL_CANCELLED,
    ):
        view.pending_hitl.pop(p.get("approval_id", ""), None)
