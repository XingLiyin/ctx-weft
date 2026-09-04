"""同一会话的全部事件必须带同一个 tenant_id（总账 A5）。

三个曾经漏填的发射点，一并收口：
1. `HitlService._emit`（`HitlOpened` / `HitlResolved`）——由真实 `start_session` →
   工具触发 `HumanConfirmationAuthorizer` → `reply_to_hitl` 的热审批链路驱动，复用
   `tests.integration.test_hitl_e2e_v2` 已有的搭台（真实 gateway/HitlService，只在
   LLM 与工具 provider 两处打桩）。
2. root task 的 `TaskCreated`——同一条链路的第一批事件之一，与 ① 合并断言：
   「这个会话发出的每一条事件都不许掉回 default 租户」。
3. `runtime._announce_queue_state_as_tm_proxy`——恢复期代 TaskManager 发的两条队列
   信号，需要单独的「进程重启」场景：另起一个共享同一个 event_store 的新 runtime，
   调 `recover()`。
"""

from __future__ import annotations

import pytest

from ctx_weft.core.runtime import SessionStartParams
from ctx_weft.protocols.events import Event
from ctx_weft.protocols.hitl import HitlReply, ToolResultDelivery
from tests.integration.test_hitl_e2e_v2 import (
    _BASH_CALL,
    _ActRouterLLM,
    _finish_call,
    _make_runtime_with_bash_tool,
    _poll,
)

pytestmark = pytest.mark.asyncio

TENANT = "acme"


async def test_hot_approval_session_events_all_carry_the_sessions_tenant() -> None:
    """`start_session(tenant_id="acme")` 走一轮热审批 + finish，总线上不许出现
    `tenant_id != "acme"` 的事件——`SessionCreated` 本就正确，`TaskCreated`（root task）
    与 `HitlOpened`/`HitlResolved` 是本次要收口的三处里的两处。
    """
    llm = _ActRouterLLM(act_responses=[_BASH_CALL, _finish_call()])
    runtime, tool = _make_runtime_with_bash_tool(llm)

    events: list[Event] = []

    async def _record(ev: Event) -> None:
        events.append(ev)

    runtime._event_bus.subscribe(None, _record)  # type: ignore[attr-defined]

    handle = await runtime.start_session(SessionStartParams.create(
        template_id="agent:tpl_echo", user_prompt="please run ls", context_limit=100_000,
        tenant_id=TENANT,
    ))
    sid = handle.session_id

    pending = await _poll(lambda: runtime.hitl_registry.list_pending(session_id=sid) or None)
    req = pending[0]
    assert req.form == "approval"
    assert isinstance(req.delivery, ToolResultDelivery)

    view = await runtime.reply_to_hitl(
        HitlReply(hitl_id=req.id, outcome="accepted", agent_id=req.agent_id)
    )
    assert view is not None and view.outcome == "accepted"

    state = await handle.wait_for_finish(timeout=5.0)
    assert state is not None and tool.invocations == 1

    offenders = [f"{e.type}:{e.tenant_id}" for e in events if e.tenant_id != TENANT]
    assert offenders == [], f"这些事件掉回了 default 租户: {offenders}"
    # 正面断言不是「offenders 恰好为空的空集合」这种平凡真——确认真有 HITL_OPENED /
    # HITL_RESOLVED / root TaskCreated 混在这批事件里，否则上面的断言毫无区分力。
    types_seen = {e.type for e in events}
    assert {"HitlOpened", "HitlResolved", "TaskCreated"} <= types_seen


async def test_recovery_queue_signal_carries_the_recovered_sessions_tenant() -> None:
    """进程重启场景：`recover()` 代 TaskManager 发的队列信号必须带上被恢复会话的真实
    tenant，而不是新 runtime 实例的默认值。

    用 `hitl_timeout_sec=0` 逼一次冷 park（零热窗），拿到一个持有未决 HITL、已落盘的
    会话；随后另起一个**空白**的新 runtime、只共享同一个 `event_store`（模拟「进程
    刚起来，`_task_managers` 还是空的」），调 `recover()`。
    """
    llm = _ActRouterLLM(act_responses=[_BASH_CALL, _finish_call()])
    runtime, _tool = _make_runtime_with_bash_tool(llm, hitl_timeout_sec=0)

    handle = await runtime.start_session(SessionStartParams.create(
        template_id="agent:tpl_echo", user_prompt="please run ls", context_limit=100_000,
        tenant_id=TENANT,
    ))
    sid = handle.session_id
    state = await handle.wait_for_finish(timeout=5.0)
    assert state is not None and state.task.status == "AWAITING_HUMAN"
    pending = runtime.hitl_registry.list_pending(session_id=sid)
    assert len(pending) == 1

    llm2 = _ActRouterLLM(act_responses=[_BASH_CALL, _finish_call()])
    runtime2, _tool2 = _make_runtime_with_bash_tool(llm2, hitl_timeout_sec=0)
    runtime2.event_store = runtime.event_store  # 同一份持久事件，模拟跨进程重启
    assert runtime2._task_managers == {}         # 复现 docstring 里「_task_managers 还是空的」

    events: list[Event] = []

    async def _record(ev: Event) -> None:
        events.append(ev)

    runtime2._event_bus.subscribe(None, _record)  # type: ignore[attr-defined]

    n = await runtime2.recover()
    assert n == 1

    queue_events = [e for e in events if e.type in ("TaskQueueBlocked", "TaskQueueInterrupted")]
    assert len(queue_events) == 1, queue_events
    assert queue_events[0].session_id == sid
    assert queue_events[0].tenant_id == TENANT
