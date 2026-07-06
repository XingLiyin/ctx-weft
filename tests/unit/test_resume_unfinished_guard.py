"""resume_session 开新轮守卫：事件里仍有未终结任务 → 拒绝（须走恢复,不许无声弃置）。

弃轮路径（用户不恢复、直接发新消息）会把上一轮滞留的非终态任务永久遗弃在事件库,
之后任何 recover_session 全量重建又会把它们复活重跑（僵尸重跑,m006 判据 b 的运行时源头）。
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from ctx_weft.core.errors import UnfinishedTasksError
from ctx_weft.core.events.types import Event, EventType
from ctx_weft.core.orchestrator.lifecycle_manager import LifecycleManager
from ctx_weft.core.orchestrator.session_manager import SessionManager

pytestmark = pytest.mark.asyncio


def _ev(seq: int, type_: EventType, **payload) -> Event:
    task_id = payload.pop("task_id", None)
    return Event(id=f"e{seq:04d}", run_id="r1", sequence=seq, session_id="ses_1",
                 type=type_, timestamp=datetime(2026, 7, 6, tzinfo=timezone.utc),
                 task_id=task_id, payload=payload)


async def _runtime_and_sm():
    from ctx_weft.core import CtxWeftRuntime
    from ctx_weft.providers.llm.mock import MockLLMAdapter
    from tests.integration.test_minimal_loop import InMemoryTemplateResolver

    runtime = CtxWeftRuntime(llm=MockLLMAdapter(responses=[]),
                             template_resolver=InMemoryTemplateResolver())
    sm = SessionManager(
        lifecycle_manager=LifecycleManager(template_resolver=runtime.template_resolver),
        event_bus=runtime.event_bus,
    )
    await runtime.event_store.append(_ev(
        1, EventType.SESSION_CREATED, user_prompt="x", template_id="tpl", root_agent_id="agt"))
    return runtime, sm


async def test_resume_refuses_active_leftover() -> None:
    """上一轮滞留 ACTIVE（如硬崩溃未恢复）→ 拒绝开新轮,异常携带任务清单。"""
    runtime, sm = await _runtime_and_sm()
    await runtime.event_store.append(_ev(
        2, EventType.TASK_CREATED, task={"id": "t1", "status": "PENDING", "settings": {}}))
    await runtime.event_store.append(_ev(3, EventType.TASK_STARTED, task_id="t1"))

    with pytest.raises(UnfinishedTasksError) as ei:
        await sm.resume_session("ses_1", runtime.event_store, user_prompt="next")
    assert ei.value.session_id == "ses_1"
    assert "t1" in ei.value.task_ids


async def test_resume_refuses_suspended_leftover() -> None:
    """滞留 SUSPENDED（打断后 HITL 已取消等）同样拒绝——非终态即未完。"""
    runtime, sm = await _runtime_and_sm()
    await runtime.event_store.append(_ev(
        2, EventType.TASK_CREATED, task={"id": "t1", "status": "PENDING", "settings": {}}))
    await runtime.event_store.append(_ev(3, EventType.TASK_STARTED, task_id="t1"))
    await runtime.event_store.append(_ev(4, EventType.TASK_SUSPENDED, task_id="t1"))

    with pytest.raises(UnfinishedTasksError):
        await sm.resume_session("ses_1", runtime.event_store, user_prompt="next")


async def test_resume_allows_terminal_and_helper_tasks() -> None:
    """全部终态 + 辅助任务（compact/metadata,restore 也从不重排它们）→ 正常开新轮。"""
    runtime, sm = await _runtime_and_sm()
    await runtime.event_store.append(_ev(
        2, EventType.TASK_CREATED, task={"id": "t1", "status": "PENDING", "settings": {}}))
    await runtime.event_store.append(_ev(3, EventType.TASK_STARTED, task_id="t1"))
    await runtime.event_store.append(_ev(4, EventType.TASK_FINISHED, task_id="t1"))
    # 滞留的 daemon 辅助任务：不阻塞（恢复路径也永不重排它们,阻塞会把会话锁死）
    await runtime.event_store.append(_ev(
        5, EventType.TASK_CREATED,
        task={"id": "t2", "status": "PENDING",
              "settings": {"_type": "MetadataFillerTaskSettings"}}))
    await runtime.event_store.append(_ev(6, EventType.TASK_STARTED, task_id="t2"))

    session, root_task, tm = await sm.resume_session(
        "ses_1", runtime.event_store, user_prompt="next")
    assert session.id == "ses_1" and session.status == "RUNNING"
    assert root_task.id and tm.get_task(root_task.id) is not None
