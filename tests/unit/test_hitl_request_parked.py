"""request_parked：登记 pending HITL 但不留活 future → 后续应答走冷 resume。

ActStep 的 interactive 任务纯文本暂停用它:request() 后立即驱逐 future、HitlPark 释放协程,
用户回复时(无活 future)经 on_cold_resolve 触发 session resume。
"""

from __future__ import annotations

import pytest

from ctx_weft.providers.events import InProcessEventBus
from ctx_weft.core.orchestrator.hitl_manager import HitlManager

pytestmark = pytest.mark.asyncio


async def test_request_parked_registers_pending_without_live_future() -> None:
    mgr = HitlManager(event_bus=InProcessEventBus())
    rid = await mgr.request_parked(
        form="wait", session_id="s1", task_id="t1", agent_id="ag1",
        capability_id="control:wait_for_user", question="anything else?",
    )
    # pending 已登记（供 /hitl/pending 与崩溃恢复重建）
    assert mgr.get(rid) is not None
    assert mgr.get(rid).status == "pending"
    # future 已驱逐 → 应答必走冷路径
    assert mgr._futures.get(rid) is None


async def test_parked_answer_triggers_cold_resolve() -> None:
    cold: list = []

    async def on_cold(req):
        cold.append(req)

    mgr = HitlManager(event_bus=InProcessEventBus(), on_cold_resolve=on_cold)
    rid = await mgr.request_parked(
        form="wait", session_id="s1", task_id="t1", agent_id="ag1",
        capability_id="control:wait_for_user", question="q",
    )

    req = await mgr.answer(rid, "the user's reply")

    assert req.status == "accepted"
    assert req.message == "the user's reply"
    assert len(cold) == 1 and cold[0].id == rid  # 冷 resume 被触发
