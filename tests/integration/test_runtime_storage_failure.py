"""WP0 基线夹具（H1）：事件落库失败仍对外通知成功。

钉住 2026-09-11 可靠性方案 H1 的**缺陷现状**（探针 verify_agent_architecture.py
`persistence_failure` 的 Runtime 级移植；上游 docs/plans/2026-09-11-agent-core-
reliability-plan.md §1.2/§4.1）：EventPersister.on_event 吞掉存储异常——store 全程
失败时 emit 不抛、订阅者照常收到事件、会话继续推进到 FINISHED、落库为零。

⚠️ 本文件断言的是**旧契约**（缺陷行为），供 WP3（required 提交门 + 通知分离）实施时
**有意翻转**：翻转后——emit 拒绝提交、必要状态消费者异常上报、会话进入不可推进状态。
届时与 tests/unit/test_event_persistence_wiring.py::test_persister_swallows_store_errors
（同样是旧契约锚）同批更新。时序无需 barrier：H1 无交错，故障为全期常置。
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from ctx_weft.core import CtxWeftRuntime
from ctx_weft.core.runtime import SessionStartParams
from ctx_weft.protocols import ToolCall
from ctx_weft.providers.events.store.in_memory.store import InMemoryEventStore
from ctx_weft.providers.llm.mock import MockLLMAdapter, MockResponse
from ctx_weft.providers.memory.in_memory import InMemoryMemoryProvider
from tests.integration.test_minimal_loop import (
    InlineAgentTemplateProvider,
    make_echo_template,
    make_runtime,
)

pytestmark = pytest.mark.asyncio


class FailingEventStore(InMemoryEventStore):
    """append / append_batch 永远失败——模拟存储不可用（读路径保持可用以便断言）。"""

    async def append(self, item):
        raise OSError("simulated storage unavailable")

    async def append_batch(self, *args, **kwargs):
        raise OSError("simulated storage unavailable")


class _FinishLLM(MockLLMAdapter):
    """最短路径跑完一个 root 任务：act 收尾（正文+裸 finish_task）、后台观察收段报告。"""

    def __init__(self, **kw) -> None:
        super().__init__(responses=[], **kw)
        self._n = 0

    def complete(self, request, stream=True):
        names = {getattr(t, "name", "") for t in (getattr(request, "tools", None) or [])}
        if "control__update_task_metadata" in names:
            return self._stream(MockResponse(text=""), request)
        if "control__collect_process_report" in names:
            self._n += 1
            return self._stream(MockResponse(tool_calls=[
                ToolCall(id=f"bg{self._n}", name="control__collect_process_report",
                         arguments={"act_recap": "done", "task_summary": "done"}),
            ]), request)
        return self._stream(MockResponse(text="done", tool_calls=[
            ToolCall(id=f"fin{self._n}", name="control__finish_task", arguments={}),
        ]), request)


async def test_storage_failure_still_notifies_and_advances():
    """旧契约锚：store 全程失败 → 会话照常 FINISHED、订阅者收到事件、落库为零。"""
    resolver = InlineAgentTemplateProvider()
    resolver.register(make_echo_template())
    store = FailingEventStore()
    runtime = make_runtime(llm=_FinishLLM(), agent_provider=resolver, event_store=store)
    runtime.providers.register_memory(InMemoryMemoryProvider())

    observed: list[str] = []

    async def _observe(event) -> None:
        observed.append(event.type if isinstance(event.type, str) else event.type.value)

    runtime.event_bus.subscribe(None, _observe)

    handle = await runtime.start_session(SessionStartParams.create(
        template_id="agent:tpl_echo", user_prompt="hello", context_limit=100_000,
    ))
    state = await handle.wait_for_finish(timeout=10.0)  # 不抛——emit 从不因存储失败拒绝

    # 通知成功（缺陷）：外部订阅者收到了事件
    assert observed, "observer received nothing — notification channel broken"
    assert any(t == "TaskFinished" for t in observed), (
        f"TaskFinished never notified; saw: {observed[:10]}"
    )
    # 推进成功（缺陷）：会话在存储全失下照常跑完
    assert state is not None and state.task.status == "FINISHED"
    # 落库为零（缺陷）：对外通知过的「事实」没有一条真正提交
    stored = await store.read_by_session(handle.session_id)
    assert stored == [], f"expected 0 stored events, got {len(stored)}"
