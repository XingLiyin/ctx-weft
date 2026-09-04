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
    from ctx_weft.core.domain.models import Task

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


@pytest.fixture
async def task_manager_with_image_then_text_task():
    """base prompt 形如 [image, text]：memory 侧 reopen（`content_with_suffix`）会把
    追加的 section 合并进已有的尾部 text part；event 侧的 `_append_text_sections`
    必须复现同一合并语义，否则两侧在事件流里的形状会分歧（review round 2 finding 1）。
    """
    from ctx_weft.core.orchestrator.task_manager import TaskManager
    from ctx_weft.core.domain.models import Task

    bus = _CapturingBus()
    tm = TaskManager(session_id="s1", event_bus=bus)
    image_then_text_jsonable = [
        {"type": "image", "data": "blob:evt-2", "media_type": "image/png"},
        {"type": "text", "text": "看这张图"},
    ]
    task = Task(
        id="t2", session_id="s1", status="PENDING",
        user_prompt=image_then_text_jsonable,
    )
    await tm.push_task(task, user_prompt_event_jsonable=image_then_text_jsonable)
    task.status = "FINISHED"
    return tm, task.id, bus.events


@pytest.mark.asyncio
async def test_reopen_merges_trailing_text_section_like_memory_side(
    task_manager_with_image_then_text_task,
):
    """base 尾部已是 text part 时，reopen 追加的 section 必须合并进同一个尾部 text
    part（对齐 memory 侧 `content_with_suffix` 对连续 text 的合并语义），而不是新增
    一个独立 part——事件侧形状必须与 memory 侧同构（review round 2 finding 1）。"""
    tm, task_id, emitted = task_manager_with_image_then_text_task

    assert await tm.reopen_task(task_id, reason="重做") is True

    requeued = next(e for e in emitted if e.type == "TaskRequeued")
    parts = requeued.payload["user_prompt"]
    text_parts = [p for p in parts if p["type"] == "text"]
    assert len(text_parts) == 1, "尾部 text part 必须合并，不能产生第二个独立 text part"
    assert text_parts[0]["text"].startswith("看这张图")
    assert "重做" in text_parts[0]["text"]


def test_append_text_sections_empty_list_base_collapses_to_str():
    """base 为空列表（jsonable == []）时，memory 侧 `if base_prompt:` 判空退回纯文本
    join（str，无前导空行）；event 侧必须跟着收敛成同样的 str，不能停留成 list
    （review round 2 finding 2）。"""
    from ctx_weft.core.orchestrator.task_reopen import (
        append_text_sections as _append_text_sections,
    )

    result = _append_text_sections([], ["a", "b"])
    assert result == "a\n\nb"
    assert isinstance(result, str)


def test_append_text_sections_empty_str_base_no_leading_blank_line():
    """base 为空字符串（jsonable == ""）同理：memory 侧同一 else 分支产出
    'a\\n\\nb'，不带前导 '\\n\\n'。"""
    from ctx_weft.core.orchestrator.task_reopen import (
        append_text_sections as _append_text_sections,
    )

    assert _append_text_sections("", ["a", "b"]) == "a\n\nb"


def test_append_text_sections_none_base_no_leading_blank_line():
    """base 为 None 同理：与空字符串 / 空列表走同一 memory 侧 else 分支。"""
    from ctx_weft.core.orchestrator.task_reopen import (
        append_text_sections as _append_text_sections,
    )

    assert _append_text_sections(None, ["a", "b"]) == "a\n\nb"
