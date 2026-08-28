"""reopen_task 零 blob IO：event 侧 jsonable 挂在 Task 上，reopen 只拼文本、不发外部化。

覆盖 task-5-brief.md：
- TaskManager 结构性不再持有 event blob store（`set_event_blob_store` 已删除）。
- reopen_task 发出的 TASK_REQUEUED.user_prompt 复用首次发射时的 event ref，
  只在尾部追加 reopen 的文本 section。
"""

from __future__ import annotations

import pytest

from ctx_weft.protocols import BLOB_REF_PREFIX


class _CapturingBus:
    def __init__(self) -> None:
        self.events: list = []

    async def emit(self, event) -> None:  # noqa: ANN001
        self.events.append(event)


@pytest.fixture
async def task_manager_with_image_task():
    """用 Task 3 的新签名 push_task(..., user_prompt_event_jsonable=...) 造一个携图 task。"""
    from ctx_weft.core.orchestrator.task_manager import TaskManager
    from ctx_weft.core.state.models import Task

    bus = _CapturingBus()
    tm = TaskManager(session_id="s1", event_bus=bus)
    image_jsonable = [{"type": "image", "data": "blob:evt-1", "media_type": "image/png"}]
    task = Task(
        id="t1", session_id="s1", status="PENDING",
        user_prompt=image_jsonable,
    )
    await tm.push_task(task, user_prompt_event_jsonable=image_jsonable)
    task.status = "FINISHED"  # reopen_task 只对 FINISHED 任务生效
    return tm, task.id, bus.events


@pytest.mark.asyncio
async def test_task_manager_holds_no_event_blob_store():
    """「reopen 零 blob IO」是**结构性**保证，不是运行时哨兵：本任务把 TaskManager
    的 event blob store 整体删掉之后，它根本没有能力发起一次 blob 调用。"""
    from ctx_weft.core.orchestrator.task_manager import TaskManager

    assert not hasattr(TaskManager, "set_event_blob_store")


@pytest.mark.asyncio
async def test_reopen_reuses_carried_event_jsonable(task_manager_with_image_task):
    """TASK_REQUEUED 的 payload 由 task 上挂的 event jsonable + 追加文本拼出，零 blob IO。"""
    tm, task_id, emitted = task_manager_with_image_task

    assert await tm.reopen_task(task_id, reason="重做") is True

    requeued = next(e for e in emitted if e.type == "TaskRequeued")
    img = next(p for p in requeued.payload["user_prompt"] if p["type"] == "image")
    assert img["data"].startswith(f"{BLOB_REF_PREFIX}evt-")   # 仍是首次发射的那个 event ref
    tail = requeued.payload["user_prompt"][-1]
    assert tail["type"] == "text" and "重做" in tail["text"]
