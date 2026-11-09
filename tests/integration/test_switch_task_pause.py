"""A session pause must retain a committed replacement as its resume point."""

import asyncio
from contextlib import AsyncExitStack

import pytest

from ctx_weft import SessionStartParams
from ctx_weft.core.control.reducers import rebuild_view
from ctx_weft.protocols import ToolCall
from ctx_weft.protocols.events import EventType
from ctx_weft.protocols.hitl import (
    PREFACE_AFTER_INTERRUPT,
    PREFACE_AFTER_INTERRUPT_EDIT,
    UserTurnDelivery,
)
from ctx_weft.providers.events.store.sql import open_sqlite_event_store
from ctx_weft.providers.llm.adapters.mock import MockResponse
from ctx_weft.providers.memory.in_memory import InMemoryMemoryProvider
from ctx_weft.providers.memory.sql.provider import open_sqlite_memory
from tests._initialization import initialize_all
from tests.integration.test_max_turns_observe_stop import _drained, _runtime
from tests.integration.test_observe_waiting_continue import quiescent
from tests.integration.test_switch_task_cancel import _UserGoalSwitchLLM


@pytest.mark.parametrize("backend,restart", [("memory", False), ("sqlite", False), ("sqlite", True)])
@pytest.mark.parametrize("phase", ["before_decision", "during_commit", "after_commit", "after_enqueue"])
async def test_session_pause_during_goal_switch_retains_public_resume_point(
    tmp_path, monkeypatch, backend, restart, phase,
):
    async with AsyncExitStack() as stack:
        memory = (await stack.enter_async_context(open_sqlite_memory(tmp_path / "memory.db"))
                  if backend == "sqlite" else InMemoryMemoryProvider())
        store = (await stack.enter_async_context(open_sqlite_event_store(tmp_path / "events.db"))
                 if backend == "sqlite" else None)
        llm = _UserGoalSwitchLLM()
        rt = _runtime(llm, memory, store)
        await initialize_all(rt)
        handle = await rt.start_session(SessionStartParams.create(
            template_id="agent:tpl_echo", user_prompt="Work on the old goal and ask me for missing details"))
        tm = rt._task_managers[handle.session_id]
        await quiescent(rt, handle)
        assert tm.get_task(handle.task_id).status == "AWAITING_HUMAN"
        assert len(rt.hitl_registry.list_pending(handle.session_id)) == 1
        entered, release, pause_started, paused = [asyncio.Event() for _ in range(4)]
        runs, acks = set(), []
        append = rt.event_store.append_batch

        async def pause():
            pause_started.set()
            acks.append(await rt.pause_session(handle.session_id))
            paused.set()

        async def on_event(event):
            if event.type == EventType.OBSERVE_REQUESTED and event.payload.get("boundary") == "task_switch":
                runs.add(event.run_id)
            if ((phase == "after_commit" and event.type == EventType.TASK_REVIEWING
                 and event.task_id == handle.task_id)
                    or (phase == "after_enqueue" and event.type == EventType.TASK_CREATED
                        and event.payload.get("task", {}).get("replaces_task_id") == handle.task_id)):
                await pause()

        async def held_append(sid, bid, events, *args, **kwargs):
            before = phase == "before_decision" and any(
                e.type == EventType.RUN_FINISHED and e.run_id in runs for e in events)
            during = phase == "during_commit" and any(e.type == EventType.TASK_REVIEWING for e in events)
            if before or during:
                entered.set()
                await release.wait()
            return await append(sid, bid, events, *args, **kwargs)

        monkeypatch.setattr(rt.event_store, "append_batch", held_append)
        subscription = rt.event_bus.subscribe(None, on_event, provisional=True, required=True)
        stopping = None
        managers = [tm]
        try:
            routed = await rt.send_message(handle.agent_id,
                "Forget the old goal. Switch to the new goal instead.", session_id=handle.session_id)
            assert routed.task_id == handle.task_id
            if phase in {"before_decision", "during_commit"}:
                await asyncio.wait_for(entered.wait(), 5)
                stopping = asyncio.create_task(pause())
                await asyncio.wait_for(pause_started.wait(), 1)
                if phase == "before_decision":
                    await asyncio.wait_for(asyncio.shield(stopping), 2)
                else:
                    assert not stopping.done(), "pause waits for the switch commit arbitration"
                release.set()
            await asyncio.wait_for(paused.wait(), 5)
            async with asyncio.timeout(5):
                while (len(tm.all_tasks()) != 2 or any(t.status not in {"FAILED", "AWAITING_HUMAN"}
                                                      for t in tm.all_tasks())
                       or tm._dispatch_tasks or tm._background_asyncio_tasks):
                    await asyncio.sleep(0.01)
            old = tm.get_task(handle.task_id)
            successor, = [t for t in tm.all_tasks() if t.id != old.id]
            assert acks == [True] and old.status == "FAILED"
            assert successor.status == "AWAITING_HUMAN" and llm.actors == 2
            assert successor.user_prompt == "Answer the new question"
            pending, = rt.hitl_registry.list_pending(handle.session_id)
            assert pending.task_id == successor.id and isinstance(pending.delivery, UserTurnDelivery)
            assert pending.delivery.preface in {PREFACE_AFTER_INTERRUPT, PREFACE_AFTER_INTERRUPT_EDIT}
            assert not tm.running_task_ids() and not tm._queue.peek_all()
            assert not tm._staged and not tm._pending_switches
            assert handle.session_id not in rt._pausing | rt._pause_claimed
            assert not tm._pause_abandon
            view = await rebuild_view(rt.event_store, handle.session_id)
            assert not view.pending_observe and not view.open_runs
            assert view.tasks[successor.id].status == "AWAITING_HUMAN"
            assert await rt.pause_session(handle.session_id) is False
            await handle.wait_for_finish(timeout=2)
            await subscription.unsubscribe()

            if restart:
                rt = _runtime(llm, memory, rt.event_store)
                await initialize_all(rt)
                await rt.rebuild_session(handle.session_id, strict=True)
                tm = rt._task_managers[handle.session_id]
                managers.append(tm)
                successor = tm.get_task(successor.id)
            # Ordinary resume must suffice; recover_agent is not the workaround.
            assert await rt.resume_agent(handle.agent_id, session_id=handle.session_id) == [handle.agent_id]
            async with asyncio.timeout(5):
                while successor.status != "FINISHED" or tm._dispatch_tasks or tm._background_asyncio_tasks:
                    await asyncio.sleep(0.01)
            assert llm.actors == 3
            assert not rt.hitl_registry.list_pending(handle.session_id)
            followup = await rt.send_message(handle.agent_id, "Follow up after the resumed goal",
                                            session_id=handle.session_id)
            assert followup.task_id != successor.id
            await followup.wait_for_finish(timeout=5)
            await _drained(tm, timeout=5)
            assert tm.get_task(followup.task_id).status == "FINISHED" and llm.actors == 4
            view = await rebuild_view(rt.event_store, handle.session_id)
            assert not view.pending_observe and not view.open_runs
            assert all(t.status in {"FAILED", "FINISHED"} for t in view.tasks.values())
            assert handle.session_id not in rt._pausing | rt._pause_claimed
        finally:
            release.set()
            await subscription.unsubscribe()
            jobs = [job for owner in managers
                    for job in owner._background_asyncio_tasks | set(owner._dispatch_tasks.values())]
            if stopping is not None:
                jobs.append(stopping)
            for job in jobs:
                job.cancel()
            await asyncio.gather(*jobs, return_exceptions=True)


class _DelegatedSwitchLLM(_UserGoalSwitchLLM):
    def __init__(self, replace_ancestor):
        super().__init__()
        self.replace_ancestor = replace_ancestor
        self.root_id = ""

    def complete(self, request, stream=True):
        names = {t.name for t in request.tools}
        if names.intersection({"control__update_task_metadata", "control__report_observation"}):
            return super().complete(request, stream)
        self.actors += 1
        if self.actors == 1:
            response = MockResponse(tool_calls=[ToolCall(id="delegate", name="control__delegate_task",
                arguments={"title": "Old child goal", "description": "Replace this subgoal", "use_subagent": True})])
        elif self.actors == 2:
            response = MockResponse(tool_calls=[ToolCall(id="switch", name="control__switch_task", arguments={
                "title": "Replacement", "description": "Complete the replacement goal", "task_prompt": "New goal",
                "target_task_id": self.root_id if self.replace_ancestor else "",
                "reason": "The request requires replacing the goal"})])
        else:
            response = MockResponse(text="Completed the requested work")
        return self._stream(response, request)


@pytest.mark.parametrize("replace_ancestor", [False, True])
async def test_session_pause_of_delegated_switch_preserves_root_resume_point(monkeypatch, replace_ancestor):
    llm = _DelegatedSwitchLLM(replace_ancestor)
    rt = _runtime(llm, InMemoryMemoryProvider())
    await initialize_all(rt)
    entered, release = asyncio.Event(), asyncio.Event()
    runs = set()
    append = rt.event_store.append_batch

    async def on_event(event):
        if event.type == EventType.TASK_CREATED and not llm.root_id:
            llm.root_id = event.task_id
        if event.type == EventType.OBSERVE_REQUESTED and event.payload.get("boundary") == "task_switch":
            runs.add(event.run_id)

    async def held_append(sid, bid, events, *args, **kwargs):
        if any(e.type == EventType.RUN_FINISHED and e.run_id in runs for e in events):
            entered.set()
            await release.wait()
        return await append(sid, bid, events, *args, **kwargs)

    monkeypatch.setattr(rt.event_store, "append_batch", held_append)
    subscription = rt.event_bus.subscribe(None, on_event, provisional=True, required=True)
    handle = await rt.start_session(SessionStartParams.create(template_id="agent:tpl_echo",
        user_prompt="Delegate the old subgoal, replace the requested goal, then complete the work"))
    tm = rt._task_managers[handle.session_id]
    try:
        await asyncio.wait_for(entered.wait(), 5)
        assert await rt.pause_session(handle.session_id) is True
        release.set()
        async with asyncio.timeout(5):
            while (len(tm.all_tasks()) != 3 or any(t.status in {"ACTIVE", "PENDING", "SUSPENDED", "REVIEWING"}
                                                  for t in tm.all_tasks())
                   or tm._dispatch_tasks or tm._background_asyncio_tasks):
                await asyncio.sleep(0.01)
        successor, = [t for t in tm.all_tasks() if t.replaces_task_id]
        pending, = rt.hitl_registry.list_pending(handle.session_id)
        assert pending.agent_id == handle.agent_id and llm.actors == 2
        assert pending.task_id == (successor.id if replace_ancestor else handle.task_id)
        assert successor.status == ("AWAITING_HUMAN" if replace_ancestor else "CANCELED")
        assert not tm._staged and not tm._pending_switches
        assert not (await rebuild_view(rt.event_store, handle.session_id)).pending_observe
        assert await rt.resume_agent(handle.agent_id, session_id=handle.session_id) == [handle.agent_id]
        async with asyncio.timeout(5):
            while any(t.status not in {"FAILED", "FINISHED", "CANCELED"} for t in tm.all_tasks()):
                await asyncio.sleep(0.01)
        await _drained(tm, timeout=5)
        assert llm.actors == 3 and not rt.hitl_registry.list_pending(handle.session_id)
        assert handle.session_id not in rt._pausing | rt._pause_claimed
    finally:
        release.set()
        await subscription.unsubscribe()
        jobs = list(tm._background_asyncio_tasks | set(tm._dispatch_tasks.values()))
        for job in jobs:
            job.cancel()
        await asyncio.gather(*jobs, return_exceptions=True)
