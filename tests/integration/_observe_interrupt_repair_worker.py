"""Process exits at committed observe handoff facts, including a second recovery."""
import asyncio
import sys
import os
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from ctx_weft.core.control.reducers import rebuild_view, prune_view_for_snapshot
from ctx_weft.providers.events.store.sql import open_sqlite_event_store
from ctx_weft.providers.memory.sql.provider import open_sqlite_memory
from tests._snapshot_helpers import seed_snapshot
from tests.integration.test_foreground_observe_interrupt import runtime, InterruptedReviewLLM, SID
from tests.integration.test_observe_interrupt_repair import start, request_turn
from tests.integration.test_send_message_after_session_done import _FinishEveryTaskLLM
from tests.integration._session_cancel_worker import wait_until


async def worker(path, cut, short):
    async with open_sqlite_event_store(path / "e.db") as store:
        async with open_sqlite_memory(path / "m.db") as memory:
            recovering = cut.startswith("recover_")
            llm = (InterruptedReviewLLM() if not recovering or cut == "recover_handoff"
                   else _FinishEveryTaskLLM())
            rt = runtime(store, memory, llm, short)
            original = store.append_batch

            async def append(sid, bid, events, *args, **kwargs):
                handoff = any(e.type == "HitlOpened" for e in events)
                if handoff and cut == "before":
                    os._exit(73)
                result = await original(sid, bid, events, *args, **kwargs)
                if ((handoff and cut in {"handoff", "recover_handoff"})
                        or cut == "late" and any(e.type == "TaskMessageAppended" for e in events)
                        or cut == "recover_completed" and any(e.type == "ObserveCompleted" for e in events)
                        or cut == "recover_applied" and any(e.type == "ObserveApplied" for e in events)):
                    os._exit(73)
                return result

            store.append_batch = append
            if recovering:
                await rt.initialize()
                await rt.rebuild_session(SID, strict=True)
                tm = rt._task_managers[SID]
                tm._max_concurrent = 0
                await rt.recover_agent(tm.session.root_agent_id, session_id=SID)
                if cut == "recover_handoff":
                    await asyncio.wait_for(llm.review_started.wait(), 10)
                    await rt.pause_agent(tm.session.root_agent_id)
            else:
                if cut == "late":
                    from ctx_weft.core.loop.steps.observe import ObserveStep
                    body = ObserveStep._background_body
                    entered = asyncio.Event()
                    async def hold(self, state, ctx, boundary, judging):
                        if boundary == "interrupt":
                            entered.set()
                            await asyncio.Event().wait()
                        return await body(self, state, ctx, boundary, judging)
                    ObserveStep._background_body = hold
                handle = await start(rt, llm)
                await rt.pause_agent(handle.agent_id)
                if cut == "late":
                    await asyncio.wait_for(entered.wait(), 10)
                    tm = rt._task_managers[SID]
                    await wait_until(lambda: not tm._dispatch_tasks)
                    # Simulate a pre-upgrade snapshot omitting the new reference
                    # field, while its original requested event still carries it.
                    view = prune_view_for_snapshot(await rebuild_view(store, SID))
                    for info in view.pending_observe.values():
                        info.pop("context_record_ids", None)
                    await seed_snapshot(store, SID, view, cut=await store.committed_head(SID),
                                        overrides={"projection_version": 10})
                    await request_turn(rt, handle, "send")
            await asyncio.sleep(10)
            raise AssertionError("process did not reach crash injection")


if __name__ == "__main__":
    asyncio.run(worker(Path(sys.argv[1]), sys.argv[2], sys.argv[3] == "short"))
