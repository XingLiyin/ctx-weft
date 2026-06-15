"""ReplayEngine：从 event log 重建 state 到任意历史事件点。

Phase 6 §6.5 + §7.5 (Pure Replay 算法)。
"""

from __future__ import annotations

import logging
from typing import Any

from ctx_weft.core.control.reducers import reduce_events
from ctx_weft.core.control.types import RunStateView
from ctx_weft.core.events import Event
from ctx_weft.core.state.event_store import EventStore

logger = logging.getLogger(__name__)


class ReplayEngine:
    """Replays events up to a target event_id and returns RunStateView."""

    def __init__(self, event_store: EventStore | None = None) -> None:
        self._store = event_store

    async def replay(
        self,
        session_id: str,
        until_event_id: str | None = None,
        events: list[Event] | None = None,
    ) -> RunStateView:
        """Rebuild state from all events of a session up to until_event_id.

        If events is provided, uses them directly (in-memory mode).
        Otherwise loads from event_store via read_by_session().
        """
        if events is None and self._store is not None:
            events = await self._store.read_by_session(session_id)
        events = events or []

        if until_event_id is not None:
            target_events = []
            for ev in events:
                target_events.append(ev)
                if ev.id == until_event_id:
                    break
            events = target_events

        view = reduce_events(events, run_id=session_id)
        view.target_event_id = until_event_id
        view.events_replayed = len(events)
        return view


class InMemoryEventStore(EventStore):
    """Simple in-process event store (for tests / no-DB mode)."""

    def __init__(self) -> None:
        self._events: list[Event] = []

    async def append(self, event: Event) -> None:
        self._events.append(event)

    async def read_by_session(self, session_id: str) -> list[Event]:
        return [e for e in self._events if e.session_id == session_id]
