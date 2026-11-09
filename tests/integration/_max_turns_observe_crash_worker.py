"""Crash after accepting the observation reached by fifty real actor turns."""

import asyncio
import os
import sys
from pathlib import Path

from ctx_weft import SessionStartParams
from ctx_weft.protocols.events import EventType
from ctx_weft.providers.events.store.sql import open_sqlite_event_store
from ctx_weft.providers.memory.sql.provider import open_sqlite_memory
from tests.integration.test_max_turns_observe_stop import _FiftyTurnsLLM, _runtime


async def main(path):
    async with open_sqlite_event_store(path / "events.db") as store:
        async with open_sqlite_memory(path / "memory.db") as memory:
            llm = _FiftyTurnsLLM()
            rt = _runtime(llm, memory, store)
            append = store.append_batch

            async def crash_after_decision(sid, bid, events, *args, **kwargs):
                receipt = await append(sid, bid, events, *args, **kwargs)
                for event in events:
                    if event.type == EventType.OBSERVE_COMPLETED:
                        assert llm.actor_turns == 50 and llm.reviews == 1
                        assert event.payload["accepted"]
                        assert event.payload["disposition"]["status"] == "PENDING"
                        os._exit(73)
                return receipt

            store.append_batch = crash_after_decision
            handle = await rt.start_session(SessionStartParams.create(
                template_id="agent:tpl_echo", session_id="crash-session",
                user_prompt="Keep working until the task is complete"))
            await handle.wait_for_finish(timeout=15)
            raise AssertionError("Did not reach the fifty-turn observation")


if __name__ == "__main__":
    asyncio.run(main(Path(sys.argv[1])))
