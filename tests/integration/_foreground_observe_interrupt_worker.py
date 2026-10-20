"""Exit immediately after real foreground-to-recap handoff commits."""
import asyncio
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from ctx_weft import SessionStartParams
from ctx_weft.providers.events.store.sql import open_sqlite_event_store
from ctx_weft.providers.memory.sql.provider import open_sqlite_memory
from tests.integration.test_foreground_observe_interrupt import runtime, InterruptedReviewLLM, SID
from tests.integration.test_send_message_after_session_done import _FinishEveryTaskLLM


async def worker(path, cut, recovering):
    async with open_sqlite_event_store(path / "events.db") as store:
        async with open_sqlite_memory(path / "memory.db") as memory:
            llm = _FinishEveryTaskLLM() if recovering else InterruptedReviewLLM()
            rt = runtime(store, memory, llm, False)
            original = store.append_batch

            async def append(sid, bid, events, *args, **kwargs):
                result = await original(sid, bid, events, *args, **kwargs)
                if any(e.type == cut for e in events):
                    os._exit(73)
                return result

            store.append_batch = append
            await rt.initialize()
            if recovering:
                await rt.rebuild_session(SID, strict=True)
                root = rt._task_managers[SID].session.root_agent_id
                await rt.recover_agent(root, session_id=SID)
                await asyncio.sleep(10)
                raise AssertionError("recovery did not reach injected crash")
            handle = await rt.start_session(SessionStartParams.create(session_id=SID,
                template_id="agent:tpl_echo", user_prompt="OLD REQUEST", context_limit=100000, unattended=True))
            await asyncio.wait_for(llm.review_started.wait(), 10)
            await rt.pause_agent(handle.agent_id)
            await asyncio.sleep(10)
            raise AssertionError("handoff did not reach injected crash")


if __name__ == "__main__":
    asyncio.run(worker(Path(sys.argv[1]), sys.argv[2], "recover" in sys.argv[3:]))
