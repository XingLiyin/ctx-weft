"""Read committed task settlement prefixes, reusing the recovery reducer."""
from __future__ import annotations

import logging
from copy import deepcopy

from ctx_weft.core.control.reducers import (
    _SNAP_BLOB,
    _SNAP_CUT,
    _SNAP_DEPTH,
    _SNAP_REASON,
    _SNAP_VERSION,
    TASK_STATUS_BY_EVENT,
    _apply,
    deserialize_view,
    snapshot_facts_from_event,
    snapshot_is_usable,
)
from ctx_weft.core.control.types import (
    ObserveWaitBaseline,
    RunStateView,
    TaskSettlementProgress,
    TaskView,
)
from ctx_weft.protocols.events import EventReadProjection, EventStore, EventType

logger = logging.getLogger(__name__)

_OBSERVE_FIELDS = ("task_id", "observe_id")
_TASK_FIELDS = {
    **dict.fromkeys(TASK_STATUS_BY_EVENT, ()),
    EventType.TASK_CREATED: (("task", "id"), ("task", "status")),
    EventType.TASK_REVIEWING: (("successor", "task", "id"), ("successor", "task", "status"),
                              ("replaced_task_ids",)),
}
_OBSERVE_TYPES = (EventType.OBSERVE_REQUESTED, EventType.OBSERVE_STARTED,
                  EventType.OBSERVE_COMPLETED, EventType.OBSERVE_APPLIED,
                  "TaskRecapRequested", "TaskRecapStarted", "TaskRecapDone")
_FIELDS = tuple((key,) for key in _OBSERVE_FIELDS)
_DELTA_PROJECTION = EventReadProjection({
    **dict.fromkeys(_OBSERVE_TYPES, _FIELDS),
    EventType.HITL_OPENED: tuple(("observe_request", *path) for path in _FIELDS),
    **_TASK_FIELDS,
})


def observe_wait_baseline(pending_observe: dict[str, dict], position: int) -> ObserveWaitBaseline:
    return ObserveWaitBaseline(position, {
        key: {name: info[name] for name in _OBSERVE_FIELDS if name in info}
        for key, info in pending_observe.items()
    })


async def load_task_settlement_baseline(event_store: EventStore, session_id: str, task_id: str, *,
                                        through_position: int,
                                        baseline: ObserveWaitBaseline | None = None,
                                        loaded_task_status: str | None = None) -> TaskSettlementProgress:
    """Choose the newest valid baseline, then initialize all facts at the fixed cut."""
    view = RunStateView(run_id=session_id, session_id=session_id, task_id=task_id, agent_id="")
    after = 0
    if baseline is not None and 0 <= baseline.position <= through_position:
        after = baseline.position
        view.pending_observe = deepcopy({key: info for key, info in baseline.pending_observe.items()
                                        if info.get("task_id", key) == task_id})
    # A baseline already at H cannot be improved by another snapshot. Otherwise
    # always compare cuts: an old loaded/new-session baseline must not mask one.
    if after < through_position:
        stored = await event_store.read_last_of_type(session_id, EventType.STATE_SNAPSHOT,
            projection=EventReadProjection({EventType.STATE_SNAPSHOT: (
                (_SNAP_CUT,), (_SNAP_VERSION,), (_SNAP_DEPTH,), (_SNAP_REASON,),
                *tuple((_SNAP_BLOB, "pending_observe", "*", key) for key in _OBSERVE_FIELDS),
                *tuple((_SNAP_BLOB, "tasks", task_id, key) for key in ("id", "session_id", "status")),
            )}))
        snapshot = snapshot_facts_from_event(stored.event if stored else None)
        if (stored is not None and stored.position <= through_position
                and snapshot_is_usable(snapshot, through_position)
                and snapshot.last_commit_position >= after):
            try:
                base = deserialize_view(snapshot.state_blob)
            except (KeyError, TypeError, ValueError, AttributeError):
                logger.warning("Invalid task settlement snapshot for %s; keeping earlier baseline", session_id)
            else:
                view.pending_observe = {key: info for key, info in base.pending_observe.items()
                                        if info.get("task_id", key) == task_id}
                if task_id in base.tasks:
                    view.tasks[task_id] = base.tasks[task_id]
                after = snapshot.last_commit_position
    await _fold_delta(event_store, view, after, through_position)
    if task_id not in view.tasks:
        if loaded_task_status is not None:
            view.tasks[task_id] = TaskView(id=task_id, session_id=session_id, status=loaded_task_status,
                                           details_complete=False)
        elif through_position:
            # A snapshot can prune historical task details; status alone suffices.
            task = await _read_task_status(event_store, session_id, task_id, through_position)
            if task is not None:
                view.tasks[task_id] = task
    return TaskSettlementProgress(through_position, view)


async def advance_task_settlement(event_store: EventStore, progress: TaskSettlementProgress, *,
                                   through_position: int) -> TaskSettlementProgress:
    """Return a new complete prefix; a failed read/fold never mutates the old one."""
    if through_position < progress.position:
        raise ValueError("Task settlement cursor cannot move backwards")
    if through_position == progress.position:
        return progress
    view = deepcopy(progress._view)
    await _fold_delta(event_store, view, progress.position, through_position)
    return TaskSettlementProgress(through_position, view)


async def _fold_delta(event_store: EventStore, view: RunStateView, after: int, through: int) -> None:
    if after < through:
        rows = await event_store.read_range(view.session_id, after_position=after, through_position=through,
            include_types=tuple(_DELTA_PROJECTION.payload_fields_by_type), related_task_id=view.task_id,
            projection=_DELTA_PROJECTION)
        for row in rows:
            _apply(view, row.event)
    view.pending_observe = {key: info for key, info in view.pending_observe.items()
                            if info.get("task_id", key) == view.task_id}
    view.tasks = {key: task for key, task in view.tasks.items() if key == view.task_id}
    for task in view.tasks.values():
        task.details_complete = False


async def _read_task_status(event_store: EventStore, session_id: str, task_id: str, through: int) -> TaskView | None:
    view = RunStateView(run_id=session_id, session_id=session_id, task_id=task_id, agent_id="")
    rows = await event_store.read_range(session_id, through_position=through,
        include_types=tuple(_TASK_FIELDS), related_task_id=task_id, projection=EventReadProjection(_TASK_FIELDS))
    for row in rows:
        _apply(view, row.event)
    task = view.tasks.get(task_id)
    if task is not None:
        task.details_complete = False
    return task
