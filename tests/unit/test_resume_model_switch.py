"""换模型恢复：llm 覆盖须同步会话窗口参数（context_limit / reserved_output_tokens）。

CONTEXT_OVERFLOW 挂起的会话换更大窗口的模型 /resume：若窗口仍沿用投影里旧模型的值，
重装配会原样再溢出，切换等于无效。未传覆盖时保持投影原值（host 可能刻意配了更小窗口）。
"""

from __future__ import annotations

import pytest

from ctx_weft.core import CtxWeftRuntime
from ctx_weft.core.control.types import RunStateView, SessionView, TaskView
from ctx_weft.core.orchestrator.task_manager import TaskManager
from ctx_weft.core.state.models import Session, Task
from ctx_weft.providers.memory_blackboard import InMemoryMemoryProvider
from tests.integration.test_minimal_loop import InMemoryTemplateResolver, make_echo_template

pytestmark = pytest.mark.asyncio


class _BigClient:
    context_limit = 400_000
    output_reserve = 16_384


def _runtime(monkeypatch) -> CtxWeftRuntime:
    resolver = InMemoryTemplateResolver()
    resolver.register(make_echo_template())
    runtime = CtxWeftRuntime(template_resolver=resolver)
    runtime.providers.register_memory(InMemoryMemoryProvider())
    monkeypatch.setattr(runtime, "_resolve_llm", lambda a=None, m=None: _BigClient())
    return runtime


def test_sync_session_llm_window(monkeypatch) -> None:
    runtime = _runtime(monkeypatch)
    session = Session(id="s1", user_prompt="", status="RUNNING", llm_model="big-model",
                      context_limit=64_000, reserved_output_tokens=8_192)

    runtime._sync_session_llm_window(session)

    assert session.context_limit == 400_000
    assert session.reserved_output_tokens == 16_384


async def test_resume_in_existing_tm_syncs_window(monkeypatch) -> None:
    runtime = _runtime(monkeypatch)
    session = Session(id="s1", user_prompt="", status="RUNNING", context_limit=64_000)
    tm = TaskManager(session_id="s1", event_bus=runtime.event_bus)
    tm.set_session(session)
    tm.register_task(Task(id="A", session_id="s1", status="SUSPENDED"))
    monkeypatch.setattr(runtime, "_register_and_drain", lambda s, m: None)

    await runtime._resume_in_existing_tm(
        tm, user_reply=None, llm_account=None, llm_model="big-model", resumed_task_id="A")

    assert session.llm_model == "big-model"
    assert session.context_limit == 400_000


async def test_resume_in_existing_tm_no_override_keeps_window(monkeypatch) -> None:
    """未换模型的恢复不动窗口参数（host 可能刻意配了小于模型窗口的 limit）。"""
    runtime = _runtime(monkeypatch)
    session = Session(id="s1", user_prompt="", status="RUNNING", context_limit=64_000)
    tm = TaskManager(session_id="s1", event_bus=runtime.event_bus)
    tm.set_session(session)
    tm.register_task(Task(id="A", session_id="s1", status="SUSPENDED"))
    monkeypatch.setattr(runtime, "_register_and_drain", lambda s, m: None)

    await runtime._resume_in_existing_tm(
        tm, user_reply=None, llm_account=None, llm_model=None, resumed_task_id="A")

    assert session.context_limit == 64_000


async def test_recover_session_model_switch_syncs_window(monkeypatch) -> None:
    runtime = _runtime(monkeypatch)
    tmpl = make_echo_template()
    view = RunStateView(
        run_id="", session_id="s1", task_id="", agent_id="",
        sessions={"s1": SessionView(id="s1", template_id=tmpl.id,
                                    root_agent_id="agt_root", context_limit=64_000)},
        tasks={"A": TaskView(id="A", session_id="s1", status="SUSPENDED")},
    )

    async def fake_rebuild(store, sid):  # noqa: ANN001
        return view

    monkeypatch.setattr("ctx_weft.core.control.reducers.rebuild_view", fake_rebuild)
    captured: dict = {}
    monkeypatch.setattr(
        runtime, "_register_and_drain",
        lambda session, tm: captured.update(session=session, tm=tm))

    await runtime.recover_session("s1", llm_model="big-model")

    s = captured["session"]
    assert s.llm_model == "big-model"
    assert s.context_limit == 400_000
    assert s.reserved_output_tokens == 16_384
    # 挂起任务被重排（Task 4：retry_count 归零由 restore 保证）
    assert captured["tm"].get_task("A").status == "PENDING"
