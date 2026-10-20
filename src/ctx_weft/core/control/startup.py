"""Persist crash interruption facts without materializing runtime owners."""

from __future__ import annotations

from ctx_weft.core.control.reducers import apply_events, read_task_view, rebuild_view
from ctx_weft.core.models.discriminators import CancelReason, TaskErrorCode
from ctx_weft.core.orchestrator.lifecycle.agent_state import AgentInput, next_agent_transition
from ctx_weft.core.utils.event import new_event
from ctx_weft.protocols.events import EventOrigin, EventType
from ctx_weft.protocols.hitl import HITL_OUTCOME_CANCELLED, NoResumeDelivery, UserTurnDelivery
from ctx_weft.protocols.runtime import (
    InitializationReport, SessionInitializationResult, SessionStateSummary,
)


async def _session_state_summary(store, view, session, head: int) -> SessionStateSummary:
    agents = list(view.agents.values())
    root = view.agents.get(session.root_agent_id)
    if root is None:
        root = next((a for a in agents if a.parent_agent_id is None), None)
    pending = list(view.hitl.pending.values())
    user_turns = [r for r in pending if isinstance(r.delivery, UserTurnDelivery)]
    running = any(a.status == "running" for a in agents)
    interrupted = any(a.status == "interrupted" for a in agents)
    task_id = root.current_task_id if root else ""
    task = view.tasks.get(task_id)
    # A pruned root task is needed only if no agent/HITL fact already determines
    # the display state. Read that task at this same cut, never all task history.
    if (task is None and task_id and not running and not pending and not interrupted
            and root.status != "terminated"):
        task = await read_task_view(store, session.id, task_id, through_position=head,
                                    use_snapshot=False)
    return SessionStateSummary(
        session_status=session.status,
        root_agent_id=root.id if root else session.root_agent_id or "",
        root_agent_status=root.status if root else None,
        root_task_id=task_id or "",
        root_task_status=task.status if task else None,
        has_running_agents=running,
        has_interrupted_agents=interrupted,
        pending_hitl_count=len(pending),
        pending_user_turn_hitl_count=len(user_turns),
        waiting_user_turn_agent_id=next((r.agent_id for r in reversed(user_turns) if r.agent_id), ""),
    )


async def calibrate_startup(store) -> InitializationReport:
    checked = 0
    interrupted_sessions: list[str] = []
    interrupted_tasks: list[str] = []
    results: list[SessionInitializationResult] = []
    cursor = None
    while True:
        ids = await store.list_session_ids(after_session_id=cursor, limit=100)
        if not ids:
            break
        for sid in ids:
            checked += 1
            head = await store.committed_head(sid)
            view = await rebuild_view(store, sid, through_position=head)
            session = view.sessions.get(sid)
            if session is None:
                continue
            events = []
            # Run lifetimes are independent of task status (including background steps).
            for run_id, run in view.open_runs.items():
                sequence = run["sequence"]
                for kind, payload in (
                    (EventType.RUN_INTERRUPTED, {"reason": "crash_recovery"}),
                    (EventType.RUN_FINISHED, {"outcome": "interrupted"}),
                ):
                    sequence += 1
                    events.append(new_event(
                        kind, session_id=sid, tenant_id=session.tenant_id,
                        origin=EventOrigin.RUNTIME, run_id=run_id,
                        sequence=sequence,
                        task_id=run["task_id"], agent_id=run["agent_id"], payload=payload))
            affected_agents = set()
            affected_tasks = []
            settled_agents = set()
            if session.threshold_tripped:
                for req in view.hitl.pending.values():
                    events.append(new_event(
                        EventType.HITL_RESOLVED, session_id=sid,
                        tenant_id=session.tenant_id, origin=EventOrigin.RUNTIME,
                        task_id=req.task_id, agent_id=req.agent_id,
                        payload={"hitl_id": req.id, "outcome": HITL_OUTCOME_CANCELLED,
                                 "message": ""}))
            waiting = {r.task_id: r for r in view.hitl.pending.values()
                       if not session.threshold_tripped and not isinstance(r.delivery, NoResumeDelivery)}
            for task in view.tasks.values():
                aid = task.assigned_agent_id or task.creator_agent_id or None
                terminal_kind = None
                if task.status not in {"FINISHED", "FAILED", "CANCELED"}:
                    if task.id in session.threshold_root_task_ids:
                        terminal_kind = EventType.TASK_FAILED
                        payload = {"error_code": TaskErrorCode.BY_THRESHOLD,
                                   "error_message": "Session failure threshold reached"}
                    elif task.id in session.threshold_cancel_task_ids:
                        terminal_kind = EventType.TASK_CANCELED
                        payload = {"reason": CancelReason.FAILURE_THRESHOLD}
                if terminal_kind is not None:
                    events.append(new_event(
                        terminal_kind, session_id=sid, tenant_id=session.tenant_id,
                        origin=EventOrigin.RUNTIME, task_id=task.id, agent_id=aid,
                        payload=payload))
                    if aid:
                        affected_agents.add(aid)
                        settled_agents.add(aid)
                    continue
                if task.status != "ACTIVE":
                    continue
                if task.id in waiting:
                    events.append(new_event(
                        EventType.TASK_AWAITING_HUMAN, session_id=sid,
                        tenant_id=session.tenant_id, origin=EventOrigin.RUNTIME,
                        task_id=task.id, agent_id=aid,
                        payload={"hitl_id": waiting[task.id].id},
                    ))
                    continue
                events.append(new_event(
                    EventType.TASK_INTERRUPTED, session_id=sid,
                    tenant_id=session.tenant_id, origin=EventOrigin.RUNTIME,
                    task_id=task.id, agent_id=aid,
                    payload={"reason": "crash_recovery", "error_message": "Process interrupted"},
                ))
                interrupted_tasks.append(task.id)
                affected_tasks.append(task.id)
                if aid:
                    affected_agents.add(aid)
            for aid, agent in view.agents.items():
                if agent.status == "terminated":
                    continue
                current_task = view.tasks.get(agent.current_task_id)
                if (current_task is not None and current_task.status in {"FINISHED", "FAILED", "CANCELED"}
                        and agent.status in {"running", "interrupted"}):
                    settled_agents.add(aid)
                    affected_agents.add(aid)
                if session.threshold_tripped and agent.current_task_id in (
                        session.threshold_root_task_ids + session.threshold_cancel_task_ids):
                    # TaskFailed may commit just before ALM's AgentIdle. A snapshot
                    # can already have pruned that terminal task, but retains agents.
                    settled_agents.add(aid)
                    affected_agents.add(aid)
                if agent.status == "running" or aid in affected_agents:
                    req = waiting.get(agent.current_task_id)
                    if req is not None and req.agent_id and req.agent_id != aid:
                        req = None
                    transition = next_agent_transition(
                        agent.status,
                        (AgentInput.SETTLED if aid in settled_agents else
                         AgentInput.AWAITING_HUMAN if req else AgentInput.INTERRUPTED),
                        task_id=agent.current_task_id, hitl_id=req.id if req else "",
                        reason="crash_recovery")
                    if transition is None:
                        continue
                    events.append(new_event(
                        transition.event_type, session_id=sid,
                        tenant_id=session.tenant_id, origin=EventOrigin.RUNTIME,
                        agent_id=aid, task_id=agent.current_task_id,
                        payload=transition.payload,
                    ))
            stored = []
            committed = head
            if events:
                interrupted = bool(affected_tasks) or any(
                    e.type == EventType.AGENT_INTERRUPTED for e in events)
                # No bus fanout: startup must not populate runtime registries or snapshots.
                receipt = await store.append_batch(sid, f"startup:{sid}:{head}", events)
                stored = list(receipt.records)
                committed = stored[-1].position
                apply_events([row.event for row in stored], view)
                if interrupted:
                    interrupted_sessions.append(sid)
            results.append(SessionInitializationResult(
                session_id=sid, before_position=head, committed_position=committed,
                state=await _session_state_summary(store, view, session, committed),
                startup_events=stored,
            ))
        cursor = ids[-1]
    return InitializationReport(checked, interrupted_sessions, interrupted_tasks, results)
