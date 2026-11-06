import asyncio
from dataclasses import replace

import pytest

from ctx_weft import SessionStartParams
from ctx_weft.core.loop.task_memory_fold import task_record_kind
from ctx_weft.protocols import MemoryAddress, MemoryScope, ProviderContext, load_records
from ctx_weft.providers.llm.adapters.mock import MockResponse
from ctx_weft.providers.memory.in_memory import InMemoryMemoryProvider
from tests.integration.test_minimal_loop import (
    InlineAgentTemplateProvider,
    make_echo_template,
    make_runtime,
)
from tests.integration.test_pause_on_first_message_e2e import _NamesTasksLLM


class ObserverLLM(_NamesTasksLLM):
    def __init__(self, *, fail=False, verdict="success"):
        super().__init__(
            responses=[MockResponse(text="Actual delivered answer")],
            observer_verdict=verdict,
            context_limit=100_000,
        )
        self.stalling = False
        self.fail = fail
        self.observations = 0

    def complete(self, request, stream=True):
        names = {t.name for t in request.tools}
        assert "control__finish_task" not in names
        if "control__report_observation" in names:
            self.observations += 1
            if self.fail == "invalid":
                return self._stream(
                    MockResponse(text="Review did not submit a judgement."), request
                )
            if self.fail:

                async def broken():
                    raise RuntimeError("observer unavailable")
                    yield

                return broken()
        return super().complete(request, stream=stream)


@pytest.mark.asyncio
@pytest.mark.parametrize("role", [True, False])
@pytest.mark.parametrize("unattended", [True, False])
@pytest.mark.parametrize("fail", [True, False, "invalid"])
async def test_normal_reply_is_reviewed_then_closed(role, unattended, fail):
    llm = ObserverLLM(fail=fail)
    template = make_echo_template()
    if not role:
        template = replace(
            template, identity={k: v for k, v in template.identity.items() if k != "observe"}
        )
    provider = InlineAgentTemplateProvider()
    provider.register(template)
    rt = make_runtime(llm=llm, agent_provider=provider)
    memory = InMemoryMemoryProvider()
    rt.providers.register_memory(memory)
    handle = await rt.start_session(
        SessionStartParams.create(
            template_id="agent:tpl_echo",
            user_prompt="Deliver the answer",

            unattended=unattended,
        )
    )
    await handle.wait_for_finish(timeout=8)
    tm = rt._task_managers[handle.session_id]
    async with asyncio.timeout(8):
        while tm._background_asyncio_tasks or tm._dispatch_tasks:
            await asyncio.sleep(0.01)
    task = tm.get_task(handle.task_id)
    assert llm.observations > 0
    assert task.status == "FINISHED" and task.observer_outcome == "success"
    assert task.outputs == "Actual delivered answer"
    records = await load_records(
        memory,
        MemoryAddress(session_id=handle.session_id, agent_id=handle.agent_id),
        MemoryScope.AGENT,
        ProviderContext(session_id=handle.session_id),
    )
    assert any(task_record_kind(r) == "finish" for r in records)
    assert all(not r.metadata.get("tool_calls") for r in records)
    if fail:
        raw = await load_records(
            memory,
            MemoryAddress(
                session_id=handle.session_id, agent_id=handle.agent_id, task_id=handle.task_id
            ),
            MemoryScope.TASK,
            ProviderContext(session_id=handle.session_id),
        )
        assert any("Actual delivered answer" in str(r.content) for r in raw)


@pytest.mark.asyncio
@pytest.mark.parametrize("verdict", ["waiting", "fail"])
async def test_valid_negative_review_is_not_replaced_by_fallback(verdict):
    llm = ObserverLLM(verdict=verdict)
    provider = InlineAgentTemplateProvider()
    provider.register(make_echo_template())
    rt = make_runtime(llm=llm, agent_provider=provider)
    rt.providers.register_memory(InMemoryMemoryProvider())
    handle = await rt.start_session(
        SessionStartParams.create(
            template_id="agent:tpl_echo", user_prompt="Work"
        )
    )
    tm = rt._task_managers[handle.session_id]
    async with asyncio.timeout(8):
        while (
            tm.get_task(handle.task_id).observer_outcome != verdict
            or tm._background_asyncio_tasks
            or tm._dispatch_tasks
        ):
            await asyncio.sleep(0.01)
    assert tm.get_task(handle.task_id).status == ("AWAITING_HUMAN" if verdict == "waiting" else "FAILED")
