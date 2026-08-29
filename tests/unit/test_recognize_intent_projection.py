"""RECOGNIZE_INTENT_TOOL_CALL 回放须把 title/description 投影到 TaskView。

root task 创建时 title 为空（recognize_intent 并发补填），事件回放若丢弃
title/description，导入/重启后重建的投影里 root task 就永远无名。
"""

from __future__ import annotations

from ctx_weft.core.control.reducers import reduce_events
from ctx_weft.protocols.events import Event, EventType
from ctx_weft.core.orchestrator.task_manager import _task_payload
from ctx_weft.core.state.models import Task
from ctx_weft.core.utils import generate_id, now_utc


def _ev(seq: int, type_: str, task_id: str | None = None, payload: dict | None = None) -> Event:
    return Event(
        id=generate_id("evt"), run_id="run1", sequence=seq, session_id="s1",
        type=type_, timestamp=now_utc(), tenant_id="default",
        task_id=task_id, payload=payload or {},
    )


def _root_task_created(seq: int) -> Event:
    task = Task(id="t1", session_id="s1", status="ACTIVE", title="")
    return _ev(seq, EventType.TASK_CREATED, task_id="t1",
               payload=_task_payload(task, user_prompt_jsonable=None))


def test_recognize_intent_tool_call_fills_task_title_and_description() -> None:
    events = [
        _root_task_created(1),
        _ev(2, EventType.RECOGNIZE_INTENT_TOOL_CALL, task_id="t1", payload={
            "title": "修复导入", "description": "导入后恢复 root task 元数据", "session_goal": "",
        }),
    ]
    view = reduce_events(events, run_id="run1")
    assert view.tasks["t1"].title == "修复导入"
    assert view.tasks["t1"].description == "导入后恢复 root task 元数据"


def test_recognize_intent_tool_call_empty_fields_do_not_clobber() -> None:
    events = [
        _root_task_created(1),
        _ev(2, EventType.RECOGNIZE_INTENT_TOOL_CALL, task_id="t1", payload={
            "title": "已有标题", "description": "已有描述", "session_goal": "",
        }),
        # 后续空 payload（LLM 未产出）不得清掉已填的元数据
        _ev(3, EventType.RECOGNIZE_INTENT_TOOL_CALL, task_id="t1", payload={
            "title": "", "description": "", "session_goal": "",
        }),
    ]
    view = reduce_events(events, run_id="run1")
    assert view.tasks["t1"].title == "已有标题"
    assert view.tasks["t1"].description == "已有描述"
