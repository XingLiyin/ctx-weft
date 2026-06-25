"""InMemoryEventStore.list_active_session_ids must re-activate a session on SessionResumed.

Multi-turn sessions emit SessionFinished at the end of each turn and SessionResumed when
the next message re-activates them; "ever finished" is not terminal. A session resumed
after a finished turn must stay recoverable.
"""
from __future__ import annotations

from datetime import datetime, timezone

from ctx_weft.core.events.types import Event
from ctx_weft.core.state.event_store import InMemoryEventStore


def _ev(seq: int, type_: str) -> Event:
    return Event(
        id=f"evt_{seq:04d}", run_id="run_1", sequence=seq, session_id="s1",
        type=type_, timestamp=datetime(2026, 6, 25, tzinfo=timezone.utc), payload={},
    )


async def test_resumed_after_finished_turn_is_active() -> None:
    store = InMemoryEventStore()
    for seq, t in enumerate(
        ["SessionCreated", "RunStarted", "RunFinished", "SessionFinished",
         "SessionResumed", "RunStarted"], start=1):
        await store.append(_ev(seq, t))
    assert "s1" in await store.list_active_session_ids()


async def test_finished_session_is_not_active() -> None:
    store = InMemoryEventStore()
    for seq, t in enumerate(["SessionCreated", "RunStarted", "SessionFinished"], start=1):
        await store.append(_ev(seq, t))
    assert "s1" not in await store.list_active_session_ids()
