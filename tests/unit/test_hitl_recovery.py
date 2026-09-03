"""HITL 跨重启恢复：装填 registry 后 task 的 park / 重排判定（spec/07 §9）。

`rebuild_pending` 那两条（重建不带 future / 重建后应答走冷）已删——装填与「装填出来
的项恒无等待槽」由 `test_hitl_registry_load.py` 覆盖，冷应答由
`test_runtime_hitl_wiring.py` 覆盖。
"""

from __future__ import annotations

import pytest

from ctx_weft.core.orchestrator.task_disposition import RunOutcome, RunOutcomeKind
from tests.unit._stub_runner import StubRunner

pytestmark = pytest.mark.asyncio


def test_restore_keeps_hitl_parked_task_suspended() -> None:
    from ctx_weft.core.orchestrator.task_manager import TaskManager
    from ctx_weft.core.state.models import Task
    tm = TaskManager(session_id="s1")
    parked = Task(id="t1", session_id="s1", status="SUSPENDED")
    tm.restore([parked], terminal_ids=set(), parked_task_ids={"t1"})
    assert tm.get_task("t1").status == "SUSPENDED"
    assert not tm._queue.has_pending()


def test_restore_requeues_suspended_on_children_when_all_terminal() -> None:
    from ctx_weft.core.orchestrator.task_manager import TaskManager
    from ctx_weft.core.state.models import Task
    tm = TaskManager(session_id="s1")
    parent = Task(id="p", session_id="s1", status="SUSPENDED")
    child = Task(id="c", session_id="s1", status="FINISHED", parent_task_id="p")
    tm.restore([parent, child], terminal_ids={"c"}, parked_task_ids=set())
    assert tm.get_task("p").status == "PENDING"


def test_restore_parked_ids_default_none_is_old_behavior() -> None:
    from ctx_weft.core.orchestrator.task_manager import TaskManager
    from ctx_weft.core.state.models import Task
    tm = TaskManager(session_id="s1")
    parent = Task(id="p", session_id="s1", status="SUSPENDED")
    tm.restore([parent], terminal_ids=set())   # no parked_task_ids → old behavior: requeue
    assert tm.get_task("p").status == "PENDING"


async def test_recover_session_rebuilds_pending_hitl_and_parks() -> None:
    import asyncio
    from datetime import datetime, timezone
    from ctx_weft.core import CtxWeftRuntime
    from ctx_weft.protocols.events import Event, EventType
    from ctx_weft.providers.llm.mock import MockLLMAdapter, MockResponse
    from ctx_weft.providers.memory.in_memory import InMemoryMemoryProvider
    from tests.integration.test_minimal_loop import InlineAgentTemplateProvider, make_echo_template, make_runtime

    resolver = InlineAgentTemplateProvider()
    resolver.register(make_echo_template())
    llm = MockLLMAdapter(responses=[MockResponse(text="should not run")])
    runtime = make_runtime(llm=llm, agent_provider=resolver)
    runtime.providers.register_memory(InMemoryMemoryProvider())

    ts = datetime(2026, 6, 12, tzinfo=timezone.utc)
    def ev(seq, type_, **payload):
        task_id = payload.pop("task_id", None)
        return Event(id=f"evt_{seq:04d}", run_id="run_1", sequence=seq, session_id="ses_1",
                     type=type_, timestamp=ts, task_id=task_id, payload=payload)

    seed = [
        ev(1, EventType.SESSION_CREATED, user_prompt="do it", template_id="agent:tpl_echo",
           root_agent_id="agt_root"),
        ev(2, EventType.RUN_STARTED),
        ev(3, EventType.TASK_CREATED, task={
            "id": "tsk_1", "status": "PENDING", "title": "T1",
            "assigned_agent_id": "agt_root", "creator_agent_id": "agt_root"}),
        ev(4, EventType.TASK_STARTED, task_id="tsk_1", assigned_agent_id="agt_root"),
        ev(5, EventType.HITL_REQUIRED, task_id="tsk_1", hitl_id="hit_1", form="question",
           capability_id="control:ask_user", tool_call_id="tcA", question="Which DB?"),
        ev(6, EventType.TASK_SUSPENDED, task_id="tsk_1"),
    ]
    for e in seed:
        await runtime.event_store.append(e)

    await runtime.recover_session("ses_1")
    await asyncio.sleep(0)

    pend = runtime.hitl_registry.list_pending(session_id="ses_1")
    assert len(pend) == 1 and pend[0].tool_call_id == "tcA"
    assert llm.last_request is None             # parked task did not run


def test_restore_keeps_active_parked_task_out_of_queue() -> None:
    """缺陷 A：审批热等的任务恒为 ACTIVE；有未决 HITL 时 restore 必须保持挂起、不重排入队。

    park 判据应看"有无未决 HITL"（parked_task_ids），而非 task.status==SUSPENDED。
    """
    from ctx_weft.core.orchestrator.task_manager import TaskManager
    from ctx_weft.core.state.models import Task
    tm = TaskManager(session_id="s1")
    parked = Task(id="t1", session_id="s1", status="ACTIVE")   # 审批热等 → ACTIVE
    tm.restore([parked], terminal_ids=set(), parked_task_ids={"t1"})
    assert not tm._queue.has_pending(), "ACTIVE+parked 任务不应被重排入队"
    assert tm.get_task("t1").status == "ACTIVE", "parked 任务状态不应被改成 PENDING"


def _tm_with_bus():
    """一个挂着真实 bus、能观察聚合信号的 TaskManager（本文件两条会话收尾用例共用）。"""
    from ctx_weft.providers.events import InProcessEventBus
    from ctx_weft.core.orchestrator.task_manager import TaskManager
    from ctx_weft.core.state.models import Session

    bus = InProcessEventBus()
    seen: list = []

    async def _cap(ev):
        seen.append(ev)

    bus.subscribe(None, _cap)
    tm = TaskManager(session_id="s1", event_bus=bus, max_concurrent=1)
    tm.set_session(Session(id="s1", tenant_id="default", user_prompt="x", status="RUNNING", token_budget=0))

    async def _noop_runner(_sid, _tid):
        return None

    tm.set_runner(StubRunner(tm, _noop_runner))
    return tm, seen


async def test_session_not_finished_while_a_task_parked_on_hitl() -> None:
    """多任务：一个完成、另一个仍在等人 → TM 报 TaskQueueBlocked 而不是 TaskQueueDrained。

    Task 6：「还有人在等」不再靠注入的 pending-HITL 谓词，而是由 AWAITING_HUMAN 的
    任务自己表达；会话终不终结是 SM 看到哪条聚合信号决定的。
    """
    from ctx_weft.protocols.events import EventType
    from ctx_weft.core.state.models import NormalTaskSettings, Task

    tm, seen = _tm_with_bus()
    tm.register_task(Task(id="A", session_id="s1", status="ACTIVE", settings=NormalTaskSettings()))
    tm.register_task(Task(id="B", session_id="s1", status="AWAITING_HUMAN",
                          settings=NormalTaskSettings()))
    tm._running_tasks.add("A")
    await tm.on_task_finished("A", status="FINISHED")

    types = [e.type for e in seen]
    assert EventType.TASK_QUEUE_DRAINED not in types, "仍有等人的任务时不得报「跑完了」"
    assert EventType.SESSION_FINISHED not in types
    blocked = [e for e in seen if e.type == EventType.TASK_QUEUE_BLOCKED]
    assert blocked and blocked[0].payload["count"] == 1


async def test_session_finishes_when_no_pending_hitl() -> None:
    """对照：没有任何等人/中断的任务时，TM 报 TaskQueueDrained（SM 据此终结会话）。"""
    from ctx_weft.protocols.events import EventType
    from ctx_weft.core.state.models import NormalTaskSettings, Task

    tm, seen = _tm_with_bus()
    tm.register_task(Task(id="A", session_id="s1", status="ACTIVE", settings=NormalTaskSettings()))
    tm._running_tasks.add("A")
    await tm.on_task_finished("A", status="FINISHED")

    drained = [e for e in seen if e.type == EventType.TASK_QUEUE_DRAINED]
    assert len(drained) == 1 and drained[0].payload["final_status"] == "SUCCEEDED"


async def test_recover_emits_paused_hitl_for_pending_session() -> None:
    """缺陷 C：启动恢复时，有 pending HITL 的会话必须如实反映「在等人」，
    而非停在崩溃前的 RUNNING。

    Task 6 起走的是同一条链：recover() 代 TM 发 `TaskQueueBlocked` → SM 判 WAITING
    → 发 `SessionWaiting`。「等的是审批面板（PAUSED_HITL）还是一句话（PAUSED）」是
    delivery 的性质、只有前端需要，由 host 的只读入口推导，不上升到会话状态。
    """
    from ctx_weft.protocols.events import EventType

    runtime, verdicts = _recover_runtime_with_status_capture()
    seed = [
        _mk_ev(1, EventType.SESSION_CREATED, user_prompt="x", template_id="tpl", root_agent_id="agt"),
        _mk_ev(2, EventType.RUN_STARTED),
        _mk_ev(3, EventType.TASK_STARTED, task_id="t1", assigned_agent_id="agt"),
        _mk_ev(4, EventType.HITL_REQUIRED, task_id="t1", hitl_id="h1", form="approval",
               capability_id="fs:bash_exec", tool_call_id="tc1", question="ok?"),
    ]
    for e in seed:
        await runtime.event_store.append(e)

    await runtime.recover()
    assert verdicts == [EventType.SESSION_WAITING], "有 pending HITL 的会话恢复应判 WAITING"
    # 面板 vs 一句话的区分仍在，只是搬去了 host 的只读入口。
    assert await runtime.session_status_after_recover("ses_1") == "PAUSED_HITL"


def _recover_runtime_with_status_capture():
    """构造会捕获**全部会话级宣告**的 runtime（recover 状态语义测试共用）。

    捕的是 SM 发的会话事件类型，不是 TM 的聚合信号——断言要钉的是「会话最终被判成
    什么」，而退役的通用 setter `SessionStatusChanged` 一旦重新出现也会当场被抓到。
    """
    from ctx_weft.protocols.events import EventType
    from ctx_weft.providers.llm.mock import MockLLMAdapter
    from tests.integration.test_minimal_loop import InlineAgentTemplateProvider, make_runtime

    runtime = make_runtime(llm=MockLLMAdapter(responses=[]), agent_provider=InlineAgentTemplateProvider())
    verdicts: list = []
    watched = (EventType.SESSION_STATUS_CHANGED, EventType.SESSION_INTERRUPTED,
               EventType.SESSION_WAITING, EventType.SESSION_FINISHED)

    async def _cap(ev):
        if ev.type in watched:
            verdicts.append(ev.type)

    runtime.event_bus.subscribe(None, _cap)
    return runtime, verdicts


def _mk_ev(seq, type_, **payload):
    from datetime import datetime, timezone
    from ctx_weft.protocols.events import Event
    task_id = payload.pop("task_id", None)
    return Event(id=f"e{seq}", run_id="r1", sequence=seq, session_id="ses_1", type=type_,
                 timestamp=datetime(2026, 6, 12, tzinfo=timezone.utc), task_id=task_id, payload=payload)


async def test_recover_emits_paused_for_wait_only_pending() -> None:
    """wait-only pending（纯文本软待命）：会话判 WAITING，host 侧推导出 PAUSED 而非
    PAUSED_HITL——与 SESSION_PAUSED_HITL 的 reducer/投影语义一致（form=wait → 无面板）。"""
    from ctx_weft.protocols.events import EventType

    runtime, verdicts = _recover_runtime_with_status_capture()
    seed = [
        _mk_ev(1, EventType.SESSION_CREATED, user_prompt="x", template_id="tpl", root_agent_id="agt"),
        _mk_ev(2, EventType.RUN_STARTED),
        _mk_ev(3, EventType.TASK_STARTED, task_id="t1", assigned_agent_id="agt"),
        _mk_ev(4, EventType.HITL_REQUIRED, task_id="t1", hitl_id="h1", form="wait",
               capability_id="control:wait_for_user", tool_call_id="tc1"),
    ]
    for e in seed:
        await runtime.event_store.append(e)

    await runtime.recover()
    # 会话状态只有一个 WAITING：软待命和等面板都是「停着但正常」。
    assert verdicts == [EventType.SESSION_WAITING]
    # wait-only 不应误标 PAUSED_HITL——前端会等一个不存在的面板。
    assert await runtime.session_status_after_recover("ses_1") == "PAUSED"


async def test_recover_emits_paused_hitl_when_wait_mixed_with_question() -> None:
    """混合 pending（wait + question/approval）：会话仍判 WAITING，host 侧推导成
    PAUSED_HITL——有面板可答。"""
    from ctx_weft.protocols.events import EventType

    runtime, verdicts = _recover_runtime_with_status_capture()
    seed = [
        _mk_ev(1, EventType.SESSION_CREATED, user_prompt="x", template_id="tpl", root_agent_id="agt"),
        _mk_ev(2, EventType.RUN_STARTED),
        _mk_ev(3, EventType.TASK_STARTED, task_id="t1", assigned_agent_id="agt"),
        _mk_ev(4, EventType.HITL_REQUIRED, task_id="t1", hitl_id="h1", form="wait",
               capability_id="control:wait_for_user", tool_call_id="tc1"),
        _mk_ev(5, EventType.HITL_REQUIRED, task_id="t1", hitl_id="h2", form="question",
               capability_id="control:ask_user", tool_call_id="tc2", question="which?"),
    ]
    for e in seed:
        await runtime.event_store.append(e)

    await runtime.recover()
    assert verdicts == [EventType.SESSION_WAITING]
    assert await runtime.session_status_after_recover("ses_1") == "PAUSED_HITL"


async def test_recover_does_not_redispatch_task_running_in_live_tm() -> None:
    """方案 II：已有活 TM 正在跑 X 时，recover 建的新 TM 不得重排 X（否则跨 TM 双跑）。

    构造：老 TM alive 且 _running_tasks={X}，X 在事件里是 ACTIVE、非 parked、非终态。
    没有 inflight 过滤时 recover 会重新派发 X（echo 模板 → MockLLM 被调用）；有过滤则 X 被跳过。
    """
    import asyncio
    from datetime import datetime, timezone
    from ctx_weft.core import CtxWeftRuntime
    from ctx_weft.protocols.events import Event, EventType
    from ctx_weft.core.orchestrator.task_manager import TaskManager
    from ctx_weft.providers.llm.mock import MockLLMAdapter, MockResponse
    from ctx_weft.providers.memory.in_memory import InMemoryMemoryProvider
    from tests.integration.test_minimal_loop import InlineAgentTemplateProvider, make_echo_template, make_runtime

    resolver = InlineAgentTemplateProvider()
    resolver.register(make_echo_template())
    llm = MockLLMAdapter(responses=[MockResponse(text="should not run")])
    runtime = make_runtime(llm=llm, agent_provider=resolver)
    runtime.providers.register_memory(InMemoryMemoryProvider())

    # 老 TM：alive，正在跑 X
    old_tm = TaskManager(session_id="ses_1", event_bus=runtime.event_bus, max_concurrent=1)
    old_tm.set_is_current(lambda: True)
    old_tm._running_tasks.add("tsk_X")
    runtime._task_managers["ses_1"] = old_tm

    ts = datetime(2026, 6, 12, tzinfo=timezone.utc)

    def ev(seq, type_, **payload):
        task_id = payload.pop("task_id", None)
        return Event(id=f"e{seq}", run_id="r1", sequence=seq, session_id="ses_1", type=type_,
                     timestamp=ts, task_id=task_id, payload=payload)

    seed = [
        ev(1, EventType.SESSION_CREATED, user_prompt="do it", template_id="agent:tpl_echo", root_agent_id="agt_root"),
        ev(2, EventType.RUN_STARTED),
        ev(3, EventType.TASK_CREATED, task={
            "id": "tsk_X", "status": "ACTIVE", "title": "X",
            "assigned_agent_id": "agt_root", "creator_agent_id": "agt_root"}),
        ev(4, EventType.TASK_STARTED, task_id="tsk_X", assigned_agent_id="agt_root"),
    ]
    for e in seed:
        await runtime.event_store.append(e)

    await runtime.recover_session("ses_1")
    await asyncio.sleep(0)

    assert llm.last_request is None, "X 正被老 TM 执行，新 TM 不应重复派发"


async def test_cold_answer_reuses_live_owner_instead_of_rebuilding(monkeypatch) -> None:
    """单 owner 架构：冷 HITL 应答且存活 owner 拥有该 task → 就地重驱、**不重建 TM**。

    验证：(1) rebuild_view 未被调用(走复用而非重建)；(2) 被应答的 task 重新派发；
    (3) owner TM 未被顶替。**不再验证**「model 写回 session」——`recover_session`
    批次 B 起不再接受 llm_account/llm_model，换模型走 `set_agent_llm`/
    `set_session_llm` 两条独立命令（Task 9）。
    """
    import asyncio
    from ctx_weft.core import CtxWeftRuntime
    from ctx_weft.core.orchestrator.task_manager import TaskManager
    from ctx_weft.core.state.models import NormalTaskSettings, Session, Task
    from ctx_weft.providers.llm.mock import MockLLMAdapter
    from tests.integration.test_minimal_loop import InlineAgentTemplateProvider, make_runtime

    runtime = make_runtime(llm=MockLLMAdapter(responses=[]), agent_provider=InlineAgentTemplateProvider())

    rebuild_calls: list = []
    import ctx_weft.core.control.reducers as _reducers
    _orig = _reducers.rebuild_view

    async def _spy(*a, **k):
        rebuild_calls.append(1)
        return await _orig(*a, **k)

    monkeypatch.setattr(_reducers, "rebuild_view", _spy)

    session = Session(id="ses_1", tenant_id="default", user_prompt="x", status="RUNNING",
                      root_agent_id="agt", llm_provider="acct1", llm_model="m1", token_budget=0)
    tm = TaskManager(session_id="ses_1", event_bus=runtime.event_bus, max_concurrent=1)
    tm.set_session(session)
    tm.set_is_current(lambda: runtime._task_managers.get("ses_1") is tm)

    ran: list = []

    async def _runner(_sid, tid):
        ran.append(tid)
        # 重新 park，避免 stub 无限重排；owner 保持存活。Task 4 起「这次 run 停在哪」
        # 走返回值（RunOutcome），不再由 runner 直接写 task.status。
        ran_outcome = RunOutcome(kind=RunOutcomeKind.SUSPENDED_ON_CHILDREN)
        return ran_outcome

    tm.set_runner(StubRunner(tm, _runner))
    tm.register_task(Task(id="tsk_A", session_id="ses_1", status="SUSPENDED", settings=NormalTaskSettings()))
    runtime._task_managers["ses_1"] = tm

    await runtime.recover_session("ses_1", resumed_task_id="tsk_A")
    for _ in range(10):
        await asyncio.sleep(0)

    assert rebuild_calls == [], "复用路径不应调用 rebuild_view（未重建 TM）"
    assert ran == ["tsk_A"], "被应答的 task 应就地重新派发"
    assert runtime._task_managers["ses_1"] is tm, "owner TM 不应被顶替/替换"


async def test_crash_mid_batch_routes_to_reconcile() -> None:
    from datetime import datetime, timezone, timedelta
    from ctx_weft.core.runtime import _task_has_dangling_tool_call
    from ctx_weft.protocols import MemoryEventType, MemoryAddress, ProviderContext
    from ctx_weft.protocols.memory import MemoryEvent
    from ctx_weft.providers.memory.in_memory import InMemoryMemoryProvider

    base = datetime(2026, 6, 12, tzinfo=timezone.utc)
    mem = InMemoryMemoryProvider()
    pctx = ProviderContext(session_id="s1", tenant_id="default")
    sc = MemoryAddress(session_id="s1", task_id="t1", agent_id="ag1")
    await mem.ingest(MemoryEvent(type=MemoryEventType.LLM_RESPONSE, address=sc, content="",
        timestamp=base + timedelta(seconds=1), role="assistant",
        metadata={"tool_calls": [{"id": "x1", "name": "web", "input": {}},
                                 {"id": "x2", "name": "web", "input": {}}]}), pctx)
    await mem.ingest(MemoryEvent(type=MemoryEventType.TOOL_RESULT, address=sc, content="r1",
        timestamp=base + timedelta(seconds=2), role="tool", metadata={"tool_call_id": "x1"}), pctx)
    assert await _task_has_dangling_tool_call(mem, sc, pctx) is True   # x2 dangling → reconcile
