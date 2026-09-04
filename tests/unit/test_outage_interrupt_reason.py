"""LLM-outage interrupt is tagged with reason='llm_outage' on the **run-level** event,
so the host/frontend can tell it apart from a generic interrupt (e.g. restart).

Task 6 起会话状态不在这里宣布：loop 发 RunInterrupted（带 reason），TM 聚合成
TaskQueueInterrupted，SessionRegistry 才把会话判成 INTERRUPTED。
"""
import pytest

from ctx_weft.core import CtxWeftRuntime
from ctx_weft.core.models.config import RuntimeConfig
from ctx_weft.providers.llm.mock import MockLLMAdapter
from ctx_weft.providers.memory.in_memory import InMemoryMemoryProvider
from ctx_weft.protocols import LLMOutageError
from ctx_weft.protocols.events import EventType
from tests.integration.test_minimal_loop import InlineAgentTemplateProvider, make_echo_template, make_runtime

pytestmark = pytest.mark.asyncio


class _OutageLLM(MockLLMAdapter):
    def complete(self, request, stream=True):
        async def _gen():
            raise LLMOutageError("simulated outage exhausted")
            yield  # pragma: no cover  (make this an async generator)
        return _gen()


async def test_outage_interrupt_carries_llm_outage_reason():
    resolver = InlineAgentTemplateProvider()
    resolver.register(make_echo_template())
    runtime = make_runtime(
        llm=_OutageLLM(responses=[]),
        agent_provider=resolver,
        config=RuntimeConfig(llm_self_heal_max_attempts=1),
    )
    runtime.providers.register_memory(InMemoryMemoryProvider())

    seen = []
    orig_emit = runtime._event_bus.emit
    async def _spy(ev):
        seen.append(ev)
        return await orig_emit(ev)
    runtime._event_bus.emit = _spy

    await runtime.run_single_task(template_id="agent:tpl_echo", user_prompt="hi")

    interrupts = [e for e in seen if getattr(e, "type", None) == EventType.RUN_INTERRUPTED]
    assert interrupts, "expected RunInterrupted"
    assert all((e.payload or {}).get("reason") == "llm_outage" for e in interrupts)
    # 成因也随 task.error 抵达 TM 的聚合信号（host 据此区分 LLM 故障 vs 重启中断）。
    # 精确断言：reason 必须是**码** "llm_outage"，不是 str(exc) 那种自由文本。
    # host 按码分流（三份契约：升级须知 / docs/events-v2.md §2.1.2 / spec/golden/07）。
    queue_sig = [e for e in seen if getattr(e, "type", None) == EventType.TASK_QUEUE_INTERRUPTED]
    assert queue_sig, "expected TaskQueueInterrupted"
    assert (queue_sig[0].payload or {}).get("reason") == "llm_outage"
    assert EventType.SESSION_STATUS_CHANGED not in [getattr(e, "type", None) for e in seen]
