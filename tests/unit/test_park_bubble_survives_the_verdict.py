"""park 气泡的归属：判决不收，**用户开口才收**（2026-09-24 订正）。

纯文本回合让位是第一性的——agent 说完一段话就停下，让人能开口。后台 observe 是借
commit 机制起的旁路监控，它的结论落在 task 层（终结、放行 DAG 后继），不该改变「有人
可以开口」这个事实。

曾经有过一版让判决在判 success 时顺手收掉气泡，理由是「task 都终结了，气泡留着没用」。
那是错的，而且错得不显眼：宿主按未决 HITL 折会话状态，且**气泡优先于 task 终态**。
气泡一收，`TaskFinished` 写下的 SUCCEEDED 当场浮出来——用户刚读完回复正要打字，会话
在他眼皮底下从「等你说话」跳成「已完成」。更糟的是投递路径也跟着状态分叉：跳变之前
发的消息续跑老 task，之后发的新建 task，同一个动作因为打字快慢走两条路。

正确的时机是**这个入口被用掉的那一刻**，也就是用户真的开口：`send_message` 的两条
投递分支。注入分支（`_inject_user_turn`）一直在做；新建分支（`_start_task_for_agent`）
从前漏了——以前漏得起，因为终态 task 的气泡总是在终结时就被一并收掉了；纯文本 park
之后不再如此，所以补上。
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from ctx_weft.core.orchestrator.task.manager import TaskManager
from ctx_weft.protocols.events import Event, EventType
from ctx_weft.protocols.hitl import HitlAsk, UserTurnDelivery
from tests._event_helpers import append_one
from tests.integration.test_minimal_loop import (
    InlineAgentTemplateProvider,
    make_echo_template,
    make_runtime,
)
from tests.unit._legacy_recover import rebuild_all_active

pytestmark = pytest.mark.asyncio

_TS = datetime(2026, 9, 24, tzinfo=UTC)
_SID, _AID = "A", "agt_1"


def _ev(seq: int, type_: EventType, *, agent_id: str | None = None, **payload) -> Event:
    return Event(id=f"evt_{seq:04d}", run_id="r1", sequence=seq, session_id=_SID,
                 type=type_, timestamp=_TS, task_id=None, agent_id=agent_id, payload=payload)


async def _noop_drain(self) -> None:
    return None


async def _live_session(monkeypatch):
    """一个活着的会话 + 一个跑完（FINISHED）的 task + 挂在它上面的 park 气泡。

    这正是「后台 observe 判 success、带外终结了 task」之后的现场：task 终态，气泡未决。
    """
    monkeypatch.setattr(TaskManager, "drain", _noop_drain)   # 不真的派发到 LLM

    from ctx_weft.providers.memory.in_memory import InMemoryMemoryProvider
    resolver = InlineAgentTemplateProvider()
    resolver.register(make_echo_template())
    rt = make_runtime(agent_provider=resolver)
    rt.providers.register_memory(InMemoryMemoryProvider())

    await append_one(rt.event_store, _ev(1, EventType.SESSION_CREATED,
                                         template_id="agent:tpl_echo", root_agent_id=_AID))
    await append_one(rt.event_store, _ev(2, EventType.AGENT_INSTANTIATED, agent_id=_AID,
                                         template_id="agent:tpl_echo"))
    await append_one(rt.event_store, _ev(3, EventType.AGENT_IDLE, agent_id=_AID))
    await rebuild_all_active(rt)

    first = await rt.send_message(_AID, "讲个笑话")
    tm = rt._task_managers[_SID]

    # 纯文本回合让位：`_cold_park` 开的正是这个形状的气泡。
    req = await rt.hitl.open(
        HitlAsk(form="wait", delivery=UserTurnDelivery(task_id=first.task_id, preface="")),
        session_id=_SID, task_id=first.task_id, agent_id=_AID, tenant_id="default",
        stage="tool", unattended=False,
    )
    # 后台 observe 判 success → 带外判决把 task 终结掉。
    tm.get_task(first.task_id).status = "FINISHED"
    return rt, tm, first.task_id, req.id


def _pending(rt) -> list[str]:
    return [v.id for v in rt.list_pending_hitl(session_id=_SID) if not v.resolved]


# ── 判决终结 task 之后，气泡还在 ────────────────────────────────────────────────

async def test_the_bubble_outlives_the_finished_task(monkeypatch) -> None:
    """**要害**：task 已是终态，那个「等你说话」的入口仍然开着。

    宿主据它把会话折成 PAUSED；若它在这里就没了，会话会跳成已完成。
    """
    rt, _tm, _tid, hid = await _live_session(monkeypatch)
    assert _pending(rt) == [hid]


# ── 用户开口 → 新建分支收口它 ──────────────────────────────────────────────────

async def test_the_next_message_starts_a_new_task(monkeypatch) -> None:
    """气泡留着**不影响路由**：`send_message` 只看 `current_task_id` 是否终态。"""
    rt, _tm, finished_id, _hid = await _live_session(monkeypatch)
    second = await rt.send_message(_AID, "再讲一个")
    assert second.task_id != finished_id, "应当新开一轮，而不是注进已终结的 task"


async def test_the_next_message_closes_the_stale_bubble(monkeypatch) -> None:
    """用户开口 = 这个入口被用掉了 → 收口。

    不收的后果不在这一轮（新 task 一跑起来宿主就折成 RUNNING，显示不会错），而在之后：
    孤儿气泡会被重启后的 `rebuild_hitl` 当未决恢复出来，还会被 `resume_agent` 的
    `_pause_bubble_of` 误当成暂停气泡放行一次冷续跑。
    """
    rt, _tm, _tid, _hid = await _live_session(monkeypatch)
    await rt.send_message(_AID, "再讲一个")
    assert _pending(rt) == []


async def test_a_fresh_park_still_works_after_that(monkeypatch) -> None:
    """收口是针对**旧**气泡的：新一轮自己再 park 时照常开得出来。"""
    rt, _tm, _tid, _hid = await _live_session(monkeypatch)
    second = await rt.send_message(_AID, "再讲一个")
    again = await rt.hitl.open(
        HitlAsk(form="wait", delivery=UserTurnDelivery(task_id=second.task_id, preface="")),
        session_id=_SID, task_id=second.task_id, agent_id=_AID, tenant_id="default",
        stage="tool", unattended=False,
    )
    assert _pending(rt) == [again.id]
