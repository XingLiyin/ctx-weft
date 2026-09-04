from __future__ import annotations

import pytest

from ctx_weft.core.errors import AgentBusyError, AgentNotFound, AgentTerminatedError
from ctx_weft.core.runtime import SessionStartParams
from ctx_weft.protocols import ToolCall
from ctx_weft.protocols.agent import AgentDetail, AgentSummary
from ctx_weft.providers.llm.mock import MockLLMAdapter, MockResponse
from ctx_weft.providers.memory.in_memory import InMemoryMemoryProvider
from tests.integration.test_minimal_loop import (
    InlineAgentTemplateProvider,
    make_echo_template,
    make_runtime,
)

pytestmark = pytest.mark.asyncio

_TERMINAL_TASK_STATUSES = {"FINISHED", "FAILED", "CANCELED"}


def _rt():
    return make_runtime(agent_provider=InlineAgentTemplateProvider())


def _plant(rt, agent_id, parent, session_id="s1", status="idle"):
    from ctx_weft.core.orchestrator.agent_lifecycle_manager import _AgentRecord

    reg = rt._agent_lifecycle_manager
    reg._agents[agent_id] = _AgentRecord(
        session_id=session_id, tenant_id="default", template_id="tpl",
        parent_agent_id=parent, spawn_depth=0 if parent is None else 1,
        memory_config=None, loop_config=None, status=status,
    )
    if parent is not None:
        reg._children.setdefault(parent, set()).add(agent_id)


def _plant_live_task(rt, agent_id, task_id, *, task_status, agent_status, session_id="s1"):
    """给 `_inject_user_turn` 的真实（未 mock）注入分支搭一个可跑的最小环境：
    一个真的 `TaskManager`（挂进 `rt._task_managers`，带 `session`、登记好 task）+
    一个真的 memory provider——`requeue_for_message` 与 `_ingest_user_turn` 都要
    真跑一遍，不是 monkeypatch 掉。不给它 `set_runner`：这几个回归只关心
    `_inject_user_turn` 同步做的那部分（事件 + agent 状态转移），不需要 task 真的
    被 drain 派发起来，`drain()` 因此在这里被替换成 no-op（否则会因为没有
    `TaskRunner` 而抛错——那不是本测试要盯的东西）。
    """
    from ctx_weft.core.orchestrator.task_manager import TaskManager
    from ctx_weft.core.domain.models import Session, Task
    from ctx_weft.providers.memory.in_memory import InMemoryMemoryProvider

    rt.providers.register_memory(InMemoryMemoryProvider())

    _plant(rt, agent_id, None, session_id=session_id, status=agent_status)
    rt._agent_lifecycle_manager._agents[agent_id].current_task_id = task_id

    tm = TaskManager(session_id=session_id, event_bus=rt._event_bus)
    session = Session(
        id=session_id, user_prompt="hi", status="RUNNING", tenant_id="default",
        root_agent_id=agent_id, created_at=None,
    )
    tm.set_session(session)
    task = Task(
        id=task_id, session_id=session_id, status=task_status, tenant_id="default",
        assigned_agent_id=agent_id, creator_agent_id=agent_id,
    )
    tm.register_task(task)
    rt._task_managers[session_id] = tm

    async def _noop_drain():
        return None

    tm.drain = _noop_drain  # type: ignore[method-assign]
    return tm


async def test_list_agents_returns_flat_list():
    rt = _rt()
    _plant(rt, "root", None)
    _plant(rt, "kid", "root")

    out = rt.list_agents("s1")
    assert {a.agent_id for a in out} == {"root", "kid"}
    assert all(isinstance(a, AgentSummary) for a in out)
    kid = next(a for a in out if a.agent_id == "kid")
    assert kid.parent_agent_id == "root"


async def test_list_agents_filtered_by_parent():
    rt = _rt()
    _plant(rt, "root", None)
    _plant(rt, "kid1", "root")
    _plant(rt, "grandkid", "kid1")

    out = rt.list_agents("s1", parent_agent_id="root")
    assert {a.agent_id for a in out} == {"kid1"}, "只返回直接子 agent"


async def test_list_agents_excludes_terminated_by_default():
    rt = _rt()
    _plant(rt, "alive", None)
    _plant(rt, "dead", None, status="terminated")

    assert {a.agent_id for a in rt.list_agents("s1")} == {"alive"}
    assert {a.agent_id for a in rt.list_agents("s1", include_terminated=True)} == {"alive", "dead"}


async def test_get_agent_returns_detail():
    rt = _rt()
    _plant(rt, "root", None)
    d = rt.get_agent("root")
    assert isinstance(d, AgentDetail)
    assert d.template_id == "tpl"
    assert d.session_id == "s1"


async def test_get_agent_unknown_raises():
    rt = _rt()
    with pytest.raises(AgentNotFound):
        rt.get_agent("ghost")


async def test_send_message_rejects_running_agent():
    """spec 4.1：忙碌直接报错，不排队。"""
    rt = _rt()
    _plant(rt, "a1", None, status="running")
    with pytest.raises(AgentBusyError):
        await rt.send_message("a1", "hi")


async def test_send_message_rejects_terminated_agent():
    rt = _rt()
    _plant(rt, "a1", None, status="terminated")
    with pytest.raises(AgentTerminatedError):
        await rt.send_message("a1", "hi")


async def test_send_message_validates_session_id_when_given():
    rt = _rt()
    _plant(rt, "a1", None, session_id="s1")
    with pytest.raises(ValueError):
        await rt.send_message("a1", "hi", session_id="s-other")


async def test_send_message_reuses_live_task(monkeypatch):
    """current_task 未终态 -> 注入现有 task，不新建。"""
    rt = _rt()
    _plant(rt, "a1", None)
    rt._agent_lifecycle_manager._agents["a1"].current_task_id = "t-live"

    injected: list[tuple[str, object]] = []

    async def _fake_inject(task_id, content, **_kw):
        injected.append((task_id, content))

    monkeypatch.setattr(rt, "_inject_user_turn", _fake_inject, raising=False)
    monkeypatch.setattr(rt, "_task_is_terminal", lambda _s, _t: False, raising=False)

    tid = await rt.send_message("a1", "hello")
    assert tid == "t-live"
    assert injected == [("t-live", "hello")]


async def test_send_message_creates_new_task_when_current_is_terminal(monkeypatch):
    rt = _rt()
    _plant(rt, "a1", None)
    rt._agent_lifecycle_manager._agents["a1"].current_task_id = "t-done"

    created: list[str] = []

    async def _fake_new_task(agent_id, content, **_kw):
        created.append(agent_id)
        return "t-new"

    monkeypatch.setattr(rt, "_start_task_for_agent", _fake_new_task, raising=False)
    monkeypatch.setattr(rt, "_task_is_terminal", lambda _s, _t: True, raising=False)

    tid = await rt.send_message("a1", "hello")
    assert tid == "t-new"
    assert created == ["a1"]


# ── coordinator fix: 注入分支不得借道 TaskHumanResolved（HITL 专属配对事件）──────


async def test_inject_requeue_does_not_emit_task_human_resolved():
    """`_inject_user_turn` 的重排分支必须发 `TaskRequeued`，不是 `TaskHumanResolved`
    ——后者是 `TaskAwaitingHuman{hitl_id}` 的一对一配对解除事件，只属于 HITL 应答
    （`TaskManager.resume_task` docstring / `reducers.py`）。外部消息注入没有对应的
    `TaskAwaitingHuman`，硬发它会留一个配不上对的孤儿事件。
    """
    from ctx_weft.protocols.events import EventType

    rt = _rt()
    _plant_live_task(rt, "a1", "t1", task_status="AWAITING_HUMAN", agent_status="waiting_human")

    tid = await rt.send_message("a1", "please continue")
    assert tid == "t1"

    events = await rt.event_store.read_by_session("s1")
    types = [e.type for e in events]
    assert EventType.TASK_HUMAN_RESOLVED not in types, (
        f"must not emit TaskHumanResolved for a non-HITL wakeup, got {types}"
    )
    assert EventType.TASK_REQUEUED in types, f"expected TaskRequeued, got {types}"


async def test_inject_requeue_leaves_agent_idle_not_running():
    """入队 ≠ 已经在跑：`TaskRequeued` 必须让 agent 回 `idle`（ALM: SETTLED），不能
    像 `TaskHumanResolved` 那样提前把它翻成 `running`——真正的 `running` 要等
    `drain()` 派发出真实的 `TaskStarted`。
    """
    rt = _rt()
    _plant_live_task(rt, "a1", "t1", task_status="INTERRUPTED", agent_status="interrupted")

    await rt.send_message("a1", "resume with this extra context")

    assert rt._agent_lifecycle_manager.status_of("a1") == "idle", (
        "task 只是被塞回队列、还没真正 drain 派发，agent 不该报 running"
    )


async def test_send_message_twice_in_a_row_is_not_rejected_as_busy():
    """连续两次 `send_message` 到同一个 agent（第一次注入后立刻第二次）不能被
    `AgentBusyError` 误拒——回归的正是"注入把 agent 提前翻成 running"那个 bug。
    """
    rt = _rt()
    _plant_live_task(rt, "a1", "t1", task_status="AWAITING_HUMAN", agent_status="waiting_human")

    tid1 = await rt.send_message("a1", "first message")
    assert tid1 == "t1"

    # 第一次注入之后 task 仍是同一个未终态 task（只是被塞回队列，没被 drain 真派发），
    # 第二次消息应该继续走注入分支、落到同一个 task，而不是被拒绝。
    tid2 = await rt.send_message("a1", "second message right after")
    assert tid2 == "t1"


async def test_start_session_agent_id_is_addressable_root_agent():
    """`RunHandle.agent_id`（`start_session` 构造点，runtime.py:1330）契约钉入测试：
    调用方拿到 handle 后可以直接用 `handle.agent_id` 去 `get_agent()` / `send_message`，
    它就是这条 session 可寻址的 root agent（`parent_agent_id is None`），不是空字符串
    （裁定 R25：`RunHandle` 已有 `agent_id`，不再新增 `root_agent_id` 字段；`session
    .root_agent_id or ""` 里的 `or ""` 只是防御性写法——`SessionRegistry.create_session`
    / `resume_session` 都保证它非空，这里钉住「非空 + 可查到」这条实际契约）。
    """
    resolver = InlineAgentTemplateProvider()
    resolver.register(make_echo_template())
    llm = MockLLMAdapter(responses=[
        MockResponse(tool_calls=[
            ToolCall(id="tc1", name="control__finish_task", arguments={"result": "done"}),
        ]),
    ])
    rt = make_runtime(llm=llm, agent_provider=resolver)
    rt.providers.register_memory(InMemoryMemoryProvider())

    handle = await rt.start_session(SessionStartParams.create(
        template_id="agent:tpl_echo",
        user_prompt="hi",
        context_limit=100_000,
    ))
    await handle.wait_for_finish(timeout=5.0)

    assert handle.agent_id
    detail = rt.get_agent(handle.agent_id)
    assert detail.session_id == handle.session_id
    assert detail.parent_agent_id is None
