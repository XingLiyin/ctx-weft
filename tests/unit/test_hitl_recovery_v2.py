"""恢复期装填与暂停态推导（段 2 · Task 9）。

两个方向必须**同时**成立：
- 挂在**未决** HITL 上的 task → 保持 parked、绝不重排（人还没答，任务不能自己跑）；
- 挂在**已终局** HITL 上的 task → 必须重排（决定已落盘、进程在续跑前死了的崩溃窗口）。
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
from datetime import UTC, datetime

import pytest

from ctx_weft.protocols import BLOB_REF_PREFIX, ImagePart, ProviderContext
from ctx_weft.protocols.events import Event, EventType
from ctx_weft.protocols.hitl import ToolResultDelivery, UserTurnDelivery

pytestmark = pytest.mark.asyncio

SID = "ses_1"
TID = "tsk_1"
TS = datetime(2026, 9, 1, tzinfo=UTC)
PNG = b"\x89PNG\r\n\x1a\nfake-bytes"
PNG_REF = f"{BLOB_REF_PREFIX}{hashlib.sha256(PNG).hexdigest()}"


class _StubEventBlobStore:
    """最小可外部化 event blob 桩——`get` 只认自己 put 过的 ref。"""

    can_externalize = True

    def __init__(self) -> None:
        self.blobs: dict[str, tuple[bytes, str]] = {}

    async def put(self, data: bytes, media_type: str, ctx: ProviderContext) -> str:
        ref = f"{BLOB_REF_PREFIX}{hashlib.sha256(data).hexdigest()}"
        self.blobs[ref] = (data, media_type)
        return ref

    async def get(self, ref: str, ctx: ProviderContext):
        return self.blobs.get(ref)


class _ExplodingEventBlobStore(_StubEventBlobStore):
    """`get` 直接抛——用来证明装填路径吞得住异常（best-effort，绝不抛）。"""

    async def get(self, ref: str, ctx: ProviderContext):
        raise RuntimeError("event blob store is down")


# ── 事件构造 ────────────────────────────────────────────────────────────────


def _ev(seq: int, type_: EventType, **payload) -> Event:
    task_id = payload.pop("task_id", None)
    return Event(id=f"evt_{seq:04d}", run_id="run_1", sequence=seq, session_id=SID,
                 type=type_, timestamp=TS, task_id=task_id, payload=payload)


def _session_prelude() -> list[Event]:
    return [
        _ev(1, EventType.SESSION_CREATED, user_prompt="do it",
            template_id="agent:tpl_echo", root_agent_id="agt_root"),
        _ev(2, EventType.RUN_STARTED),
        _ev(3, EventType.TASK_CREATED, task={
            "id": TID, "status": "PENDING", "title": "T1",
            "assigned_agent_id": "agt_root", "creator_agent_id": "agt_root"}),
        _ev(4, EventType.TASK_STARTED, task_id=TID, assigned_agent_id="agt_root"),
    ]


def _legacy_pending_approval_events() -> list[Event]:
    return [
        *_session_prelude(),
        _ev(5, EventType.HITL_REQUIRED, task_id=TID, hitl_id="hit_1", form="approval",
            capability_id="tool:deploy", tool_call_id="call_1", question="ok?"),
        _ev(6, EventType.TASK_SUSPENDED, task_id=TID),
    ]


def _image_ref_payload(ref: str) -> list[dict]:
    return [{"type": "image", "data": ref, "media_type": "image/png", "source_type": "ref"}]


def _legacy_answered_with_event_blob_ref() -> list[Event]:
    return [
        *_session_prelude(),
        _ev(5, EventType.HITL_REQUIRED, task_id=TID, hitl_id="hit_1", form="question",
            capability_id="control:ask_user", tool_call_id="call_1", question="pic?"),
        _ev(6, EventType.HITL_ANSWERED, task_id=TID, hitl_id="hit_1",
            message=_image_ref_payload(PNG_REF)),
        _ev(7, EventType.TASK_SUSPENDED, task_id=TID),
    ]


def _legacy_answered_with_broken_ref() -> list[Event]:
    evs = _legacy_answered_with_event_blob_ref()
    evs[5] = _ev(6, EventType.HITL_ANSWERED, task_id=TID, hitl_id="hit_1",
                 message=_image_ref_payload(f"{BLOB_REF_PREFIX}deadbeef"))
    return evs


def _pending_with_tool_result_delivery() -> list[Event]:
    return _legacy_pending_approval_events()


def _pending_with_user_turn_delivery() -> list[Event]:
    return [
        *_session_prelude(),
        _ev(5, EventType.HITL_REQUIRED, task_id=TID, hitl_id="hit_1", form="wait",
            capability_id="control:wait_for_user", context="plain_text", question="?"),
        _ev(6, EventType.TASK_SUSPENDED, task_id=TID),
    ]


def _pending_with_custom_form_and_user_turn() -> list[Event]:
    """host 自定义 form + user_turn delivery（新两事件模型）。"""
    return [
        *_session_prelude(),
        _ev(5, EventType.HITL_OPENED, task_id=TID, hitl_id="hit_1", form="host:triage",
            delivery={"kind": "user_turn", "task_id": TID, "preface": "normal"},
            prompt="?"),
        _ev(6, EventType.TASK_SUSPENDED, task_id=TID),
    ]


def _resolved_but_never_resumed_tool_result() -> list[Event]:
    """决定已落盘，但进程在续跑之前就死了：HITL 已终局、task 仍 SUSPENDED。"""
    return [
        *_session_prelude(),
        _ev(5, EventType.HITL_REQUIRED, task_id=TID, hitl_id="hit_1", form="approval",
            capability_id="tool:deploy", tool_call_id="call_1", question="ok?"),
        _ev(6, EventType.TASK_SUSPENDED, task_id=TID),
        _ev(7, EventType.HITL_APPROVED, task_id=TID, hitl_id="hit_1", message="go"),
    ]


# ── runtime 装配 ────────────────────────────────────────────────────────────


async def _runtime_with_events(events, *, event_blob_store=None):
    from ctx_weft.providers.llm.mock import MockLLMAdapter, MockResponse
    from ctx_weft.providers.memory.in_memory import InMemoryMemoryProvider
    from tests.integration.test_minimal_loop import (
        InlineAgentTemplateProvider,
        make_echo_template,
        make_runtime,
    )

    resolver = InlineAgentTemplateProvider()
    resolver.register(make_echo_template())
    llm = MockLLMAdapter(responses=[MockResponse(text="resumed")] * 8)
    llm.last_request = None
    rt = make_runtime(llm=llm, agent_provider=resolver)
    rt.providers.register_memory(InMemoryMemoryProvider())
    store = event_blob_store if event_blob_store is not None else _StubEventBlobStore()
    if isinstance(store, _StubEventBlobStore):
        store.blobs[PNG_REF] = (PNG, "image/png")
    rt.providers.register_event_blob_store(store)
    for e in events:
        await rt.event_store.append(e)
    rt._test_llm = llm
    return rt


def _task_status(rt, task_id: str) -> str:
    tm = rt._task_managers.get(SID)
    assert tm is not None, "recover_session should have registered a TaskManager"
    task = tm.get_task(task_id)
    assert task is not None
    return task.status


async def _drained_tasks(rt) -> list[str]:
    """实际被派发跑起来的 task —— 以「LLM 被调用过」为证（与既有恢复测试同口径）。"""
    for _ in range(40):
        if rt._test_llm.last_request is not None:
            return [TID]
        await asyncio.sleep(0.02)
    return []


def _is_event_side_ref(message) -> bool:
    return any(getattr(p, "source_type", "") == "ref" for p in (message or [])
               if isinstance(p, ImagePart))


def _text_of(message) -> str:
    from ctx_weft.core.content import content_to_text
    return content_to_text(message)


# ── 装填 ────────────────────────────────────────────────────────────────────


async def test_recovery_fills_the_registry_from_folded_events():
    rt = await _runtime_with_events(_legacy_pending_approval_events())
    n = await rt.rebuild_hitl(SID)
    assert n == 1
    assert rt.hitl_registry.list_pending(SID)[0].tool_call_id == "call_1"


async def test_filled_requests_have_no_wait_slot():
    """重启后一切皆冷。"""
    rt = await _runtime_with_events(_legacy_pending_approval_events())
    await rt.rebuild_hitl(SID)
    assert rt.hitl_registry.list_pending(SID)[0].slot is None


async def test_event_side_refs_are_hydrated_before_filling():
    """spec §12.3.3：event 侧 ref 直接进 memory 会写一个永远打不开的引用。"""
    rt = await _runtime_with_events(_legacy_answered_with_event_blob_ref())
    await rt.rebuild_hitl(SID)
    got = rt.hitl_registry.decision_for(SID, "call_1", "tool")
    assert got is not None
    decision, _ = got
    assert not _is_event_side_ref(decision.message)
    assert decision.message[0].data == base64.b64encode(PNG).decode()


async def test_hydration_failure_degrades_to_text_and_never_raises():
    rt = await _runtime_with_events(_legacy_answered_with_broken_ref())
    await rt.rebuild_hitl(SID)                       # 不得抛
    decision, _ = rt.hitl_registry.decision_for(SID, "call_1", "tool")
    assert "[image" in _text_of(decision.message)
    assert not _is_event_side_ref(decision.message)


async def test_hydration_never_raises_even_when_the_blob_store_explodes():
    """降级本身也不能再抛：异常会卡住整条恢复路径。"""
    rt = await _runtime_with_events(_legacy_answered_with_event_blob_ref(),
                                    event_blob_store=_ExplodingEventBlobStore())
    await rt.rebuild_hitl(SID)                       # 不得抛
    decision, _ = rt.hitl_registry.decision_for(SID, "call_1", "tool")
    assert not _is_event_side_ref(decision.message)
    assert "[image" in _text_of(decision.message)


# ── 暂停态推导 ──────────────────────────────────────────────────────────────


async def test_session_status_paused_hitl_for_tool_result_delivery():
    rt = await _runtime_with_events(_pending_with_tool_result_delivery())
    assert await rt.session_status_after_recover(SID) == "PAUSED_HITL"
    assert isinstance(rt.hitl_registry.list_pending(SID)[0].delivery, ToolResultDelivery)


async def test_session_status_paused_for_user_turn_delivery():
    """UserTurn = 软待命，没有面板要答 —— 误标会让前端等一个不存在的面板。"""
    rt = await _runtime_with_events(_pending_with_user_turn_delivery())
    assert await rt.session_status_after_recover(SID) == "PAUSED"
    assert isinstance(rt.hitl_registry.list_pending(SID)[0].delivery, UserTurnDelivery)


async def test_status_is_derived_from_delivery_not_from_form():
    """host 自定义 form 也能拿到正确的暂停态（旧实现按 form == "wait" 字面量判定）。"""
    rt = await _runtime_with_events(_pending_with_custom_form_and_user_turn())
    pend = await rt.rebuild_hitl(SID)
    assert pend == 1 and rt.hitl_registry.list_pending(SID)[0].form == "host:triage"
    assert await rt.session_status_after_recover(SID) == "PAUSED"


async def test_session_status_is_empty_when_nothing_is_pending():
    rt = await _runtime_with_events(_resolved_but_never_resumed_tool_result())
    assert await rt.session_status_after_recover(SID) == ""


# ── 两个方向 ────────────────────────────────────────────────────────────────


async def test_a_task_with_an_unresolved_hitl_stays_parked_and_is_not_requeued():
    """最高风险的一条：人还没答，任务绝不能自己跑起来。"""
    rt = await _runtime_with_events(_pending_with_tool_result_delivery())
    await rt.recover_session(SID)
    assert _task_status(rt, TID) == "SUSPENDED"
    assert await _drained_tasks(rt) == []


async def test_a_task_parked_on_an_already_resolved_hitl_is_requeued():
    """崩溃窗口的兜底：决定已落盘、但进程在续跑之前死了。"""
    rt = await _runtime_with_events(_resolved_but_never_resumed_tool_result())
    await rt.recover_session(SID)
    assert TID in await _drained_tasks(rt)


async def test_resolved_hitl_task_is_requeued_even_when_a_child_is_still_running():
    """`restore` 的「children 全终态」闸门不能把已答过的 task 永远晾着。"""
    from ctx_weft.core.orchestrator.task_manager import TaskManager
    from ctx_weft.core.state.models import Task

    tm = TaskManager(session_id=SID)
    parent = Task(id="p", session_id=SID, status="SUSPENDED")
    child = Task(id="c", session_id=SID, status="ACTIVE", parent_task_id="p")
    tm.restore([parent, child], terminal_ids=set(), parked_task_ids=set(),
               resumable_task_ids={"p"})
    assert tm.get_task("p").status == "PENDING"


async def test_parked_wins_over_resumable():
    """同时既有未决、又有已终局 HITL 的 task：未决优先，保持 parked。"""
    from ctx_weft.core.orchestrator.task_manager import TaskManager
    from ctx_weft.core.state.models import Task

    tm = TaskManager(session_id=SID)
    t = Task(id="p", session_id=SID, status="SUSPENDED")
    tm.restore([t], terminal_ids=set(), parked_task_ids={"p"}, resumable_task_ids={"p"})
    assert tm.get_task("p").status == "SUSPENDED"
    assert not tm._queue.has_pending()


async def test_resolved_for_session_sees_the_task_id_of_a_filled_decision():
    """兜底的承重点：装填出来的已终局记录必须带 task_id，否则重排集合恒为空。"""
    rt = await _runtime_with_events(_resolved_but_never_resumed_tool_result())
    await rt.rebuild_hitl(SID)
    resolved = rt.hitl_registry.resolved_for_session(SID)
    assert [r.task_id for r in resolved] == [TID]
    assert rt.hitl_registry.resolved_for_session("other") == []


async def test_requeue_of_a_resolved_user_turn_does_not_duplicate_the_injection():
    """UserTurn 侧的幂等靠 MemoryEvent.id = f"hitlreply:{hitl_id}"（§7.3/§12.2）。"""
    from ctx_weft.core.hitl.registry import HITL_STAGE_TOOL, PendingHitl
    from ctx_weft.core.orchestrator.task_manager import TaskManager
    from ctx_weft.core.state.models import Session, Task
    from ctx_weft.protocols import MemoryAddress, MemoryKind, MemoryScope
    from ctx_weft.protocols.hitl import HitlDecision

    rt = await _runtime_with_events(_pending_with_user_turn_delivery())
    session = Session(id=SID, user_prompt="do it", status="RUNNING", tenant_id="default")
    tm = TaskManager(session_id=SID)
    task = Task(id=TID, session_id=SID, status="SUSPENDED", assigned_agent_id="agt_root")
    tm.restore([task], terminal_ids=set())
    req = PendingHitl(
        id="hit_1", form="wait", session_id=SID, task_id=TID, agent_id="agt_root",
        delivery=UserTurnDelivery(task_id=TID), created_at=TS, stage=HITL_STAGE_TOOL,
        decision=HitlDecision(outcome="accepted", message="carry on"),
    )
    await rt._inject_user_reply(req, session, tm)
    await rt._inject_user_reply(req, session, tm)

    scope = MemoryAddress(session_id=SID, task_id=TID, agent_id="agt_root")
    pctx = ProviderContext(session_id=SID, tenant_id="default", task_id=TID,
                           agent_id="agt_root")
    view = await rt.providers.get_memory().load_view(
        scope, MemoryScope.TASK, pctx, kinds=[MemoryKind.CONVERSATION_TURN])
    assert sum(1 for r in view if r.metadata.get("source") == "hitl_reply") == 1
