"""Explicit all-session recovery for legacy test fixtures, never production startup."""
from ctx_weft import SessionNotFound
from ctx_weft.protocols.events import EventType
from ctx_weft.protocols.runtime import InitializationReport


async def initialize_all(runtime):
    results = []
    checked = 0
    cursor = None
    while ids := await runtime.event_store.list_session_ids(after_session_id=cursor, limit=100):
        for sid in ids:
            checked += 1
            try:
                result = await runtime.initialize(sid)
            except SessionNotFound:
                continue
            if result is not None:
                results.append(result)
        cursor = ids[-1]
    tasks = [row.event.task_id for result in results for row in result.startup_events
             if row.event.type == EventType.TASK_INTERRUPTED]
    sessions = [result.session_id for result in results if any(
        row.event.type in {EventType.TASK_INTERRUPTED, EventType.AGENT_INTERRUPTED}
        for row in result.startup_events)]
    return InitializationReport(checked, sessions, tasks, results)
