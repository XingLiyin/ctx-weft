"""The next user turn sees a single full or minimal capsule of the previous task."""

from types import SimpleNamespace

import pytest

from ctx_weft import SessionStartParams
from ctx_weft.core.loop.steps.compact import demote_kept_capsules
from ctx_weft.protocols import MemoryAddress, ProviderContext
from ctx_weft.providers.llm.adapters.mock import MockResponse
from ctx_weft.providers.memory.in_memory import InMemoryMemoryProvider
from tests.integration.test_minimal_loop import (
    InlineAgentTemplateProvider,
    make_echo_template,
    make_runtime,
)
from tests.integration.test_pause_on_first_message_e2e import _NamesTasksLLM


class RepliesLLM(_NamesTasksLLM):
    def __init__(self):
        super().__init__(
            responses=[
                MockResponse(text="First delivered answer"),
                MockResponse(text="Second delivered answer"),
            ],
            observer_verdict="success",
            context_limit=100_000,
        )
        self.stalling = False
        self.actor_requests = []

    def complete(self, request, stream=True):
        names = {t.name for t in request.tools}
        if not names.intersection({"control__report_observation", "control__update_task_metadata"}):
            self.actor_requests.append(request)
        return super().complete(request, stream=stream)


@pytest.mark.asyncio
@pytest.mark.parametrize("lean", [False, True])
async def test_followup_sees_one_representation_of_the_finished_task(lean):
    llm = RepliesLLM()
    provider = InlineAgentTemplateProvider()
    provider.register(make_echo_template())
    runtime = make_runtime(llm=llm, agent_provider=provider)
    memory = InMemoryMemoryProvider()
    runtime.providers.register_memory(memory)
    first = await runtime.start_session(
        SessionStartParams.create(
            template_id="agent:tpl_echo", user_prompt="First request", unattended=True
        )
    )
    await first.wait_for_finish(timeout=8)
    if lean:
        scope = MemoryAddress(session_id=first.session_id, agent_id=first.agent_id)
        assert (
            await demote_kept_capsules(
                SimpleNamespace(scope=scope),
                SimpleNamespace(
                    memory=memory, provider_ctx=ProviderContext(session_id=first.session_id)
                ),
                {first.task_id},
            )
            > 0
        )
    second = await runtime.send_message(
        first.agent_id, "Second request", session_id=first.session_id
    )
    await second.wait_for_finish(timeout=8)
    assert second.task_id != first.task_id and len(llm.actor_requests) == 2
    text = "\n".join(str(m.content) for m in llm.actor_requests[-1].messages)
    if lean:
        assert f'<task_finish task_id="{first.task_id}"' in text
        assert "<task_recap" not in text and "<task_final_reply" not in text
    else:
        assert "<task_final_reply" not in text
        assert any(m.role == "assistant" and m.content == "First delivered answer"
                   for m in llm.actor_requests[-1].messages)
        assert "<task_recap" in text and text.count("<task_finish") == 1
        assert text.count("First delivered answer") == 1
    assert "Second request" in text
