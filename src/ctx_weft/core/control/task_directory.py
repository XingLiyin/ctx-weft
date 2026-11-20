"""Committed task identities, independent of execution owners and task bodies."""
from __future__ import annotations

import base64
import json

from ctx_weft.core.models.errors import SessionNotFound
from ctx_weft.protocols.events import EventReadProjection, EventStore, EventType, EventValueSource, StoredEventValue
from ctx_weft.protocols.task import TaskIdPage

_SOURCES = {
    EventType.TASK_CREATED: EventValueSource(("task", "id"), "task_id"),
    EventType.TASK_REVIEWING: EventValueSource(("successor", "task", "id")),
}


async def read_task_id_page(store: EventStore, session_id: str, *, cursor: str | None = None,
                            limit: int = 100) -> TaskIdPage:
    if type(limit) is not int or not 1 <= limit <= 500:
        raise ValueError("limit must be an integer between 1 and 500")
    current_head = await store.committed_head(session_id)
    head, after = current_head, None
    if cursor is not None:
        try:
            if not isinstance(cursor, str) or not cursor or len(cursor) > 4096:
                raise ValueError
            value = json.loads(base64.b64decode(cursor, altchars=b"-_", validate=True))
            if (not isinstance(value, list) or len(value) != 5 or type(value[0]) is not int
                    or value[0] != 1 or value[1] != session_id
                    or type(value[2]) is not int or not 0 <= value[2] <= current_head
                    or type(value[3]) is not int or not 0 < value[3] <= value[2]
                    or not isinstance(value[4], str) or not value[4]):
                raise ValueError
            head, after = value[2], StoredEventValue(value[4], value[3])
        except (ValueError, TypeError, UnicodeError) as exc:
            raise ValueError("Invalid task cursor for this session") from exc
    created = await store.read_last_of_type(session_id, EventType.SESSION_CREATED,
                                            projection=EventReadProjection({}))
    if created is None or created.position > head:
        raise SessionNotFound(session_id)
    rows = await store.read_distinct_values(session_id, sources=_SOURCES,
        through_position=head, after=after, limit=limit + 1)
    page = rows[:limit]
    next_cursor = None
    if len(rows) > limit:
        last = page[-1]
        next_cursor = base64.urlsafe_b64encode(json.dumps(
            [1, session_id, head, last.position, last.value],
            separators=(",", ":"), ensure_ascii=False).encode()).decode()
    return TaskIdPage(tuple(row.value for row in page), next_cursor, head)
