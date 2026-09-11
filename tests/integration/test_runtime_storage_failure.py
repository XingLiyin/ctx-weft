"""已知缺陷（H1）：事件落库全部失败时，会话仍对外报告成功。

现状：`EventPersister.on_event`（providers/events/persister.py）吞掉 store 异常只打日志，
`InProcessEventBus._fanout` 也兜住 handler 异常——store 全程失败时 emit 不抛、订阅者照常
收到事件、会话推进到 FINISHED，而事件库里一条都没有。崩溃恢复（`rebuild_view`）读的正是
这个库，于是「对外通知过的事实」在重启后不存在。

本文件断言**应有行为**（存储失败必须可观测：要么 wait_for_finish 抛错，要么会话不以
FINISHED 收场），用 `xfail(strict=True)` 标记为已知缺陷。修好后测试会 XPASS 并因 strict
而报红——届时删掉 xfail 标记，同时更新 tests/unit/test_event_persistence_wiring.py::
test_persister_swallows_store_errors（那条钉的是现行的吞异常语义）。
"""
from __future__ import annotations

import pytest

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


@pytest.mark.xfail(
    strict=True, raises=AssertionError,
    reason="H1 未修复：EventPersister 吞掉 store 异常，落库全失败时会话照常 FINISHED",
)
async def test_storage_failure_is_not_silently_reported_as_success():
    resolver = InlineAgentTemplateProvider()
    resolver.register(make_echo_template())
    store = FailingEventStore()
    runtime = make_runtime(llm=_FinishLLM(), agent_provider=resolver, event_store=store)
    runtime.providers.register_memory(InMemoryMemoryProvider())

    handle = await runtime.start_session(SessionStartParams.create(
        template_id="agent:tpl_echo", user_prompt="hello", context_limit=100_000,
    ))
    try:
        state = await handle.wait_for_finish(timeout=10.0)
    except Exception:
        return  # 存储失败被上报给宿主——可接受的应有行为之一

    stored = await store.read_by_session(handle.session_id)
    assert not (state is not None and state.task.status == "FINISHED" and stored == []), (
        "session reported FINISHED while 0 events were persisted — storage failure "
        "was swallowed and never surfaced to the host"
    )
