"""Retry Current Progress placement + observe finish_task output (send-only).

Bug: process_report was appended as a trailing '## Current Progress' message, so
once the retry attempt's turns entered history it floated to the END (after the new
attempt) — confusing in the observe prompt. Fix: render it as a chronologically
placed history block via task.process_report_at, and surface the finish_task output
to the observer.
"""
from __future__ import annotations
from datetime import UTC, datetime
from types import SimpleNamespace
import pytest

from ctx_weft.core.assembler.assembler import ContextBlock
from ctx_weft.core.assembler.composer import DefaultComposer
from ctx_weft.protocols import MemoryEventType

pytestmark = pytest.mark.asyncio


def _ts(sec: int) -> str:
    return datetime(2026, 1, 1, 0, 0, sec, tzinfo=UTC).isoformat()


def _hist(role: str, content: str, sec: int, **md) -> ContextBlock:
    return ContextBlock(id=f"b{sec}", source="task_conversation", kind="history",
                        target="messages", content=content, priority=3, token_estimate=1,
                        metadata={"role": role, "timestamp": _ts(sec), **md})


def _task(**over) -> SimpleNamespace:
    base = dict(id="R", user_prompt_in_memory=True,
                process_report="FEEDBACK: approach foo missed Y; next do Y.",
                process_report_at=datetime(2026, 1, 1, 0, 0, 5, tzinfo=UTC),
                outputs="THE FINAL ANSWER", title="", description="", user_prompt="do X",
                tracking_task_ids=[], parent_task_id=None)
    base.update(over)
    return SimpleNamespace(**base)


def _idx(msgs, needle: str) -> int:
    for i, m in enumerate(msgs):
        body = m.content if isinstance(m.content, str) else str(m.content)
        if needle in body:
            return i
    return -1


async def test_observe_progress_before_next_attempt() -> None:
    """In the observe prompt, Current Progress sits before the new attempt's turns, once."""
    blocks = [
        _hist("user", "## Current Message\ndo X", 1),
        _hist("assistant", "ATTEMPT1 tried foo", 2),
        _hist("assistant", "ATTEMPT2 did Y", 10),  # this attempt's turn (already in memory at observe)
    ]
    req = SimpleNamespace(purpose="observe", task=_task(), session=SimpleNamespace(user_prompt="do X"),
                          template=None, actor_transcript=[])
    msgs = DefaultComposer()._build_observer_messages(blocks, req)

    joined = "\n".join(m.content for m in msgs if isinstance(m.content, str))
    assert joined.count("FEEDBACK:") == 1, "progress must appear exactly once"
    assert _idx(msgs, "FEEDBACK:") != -1
    assert _idx(msgs, "FEEDBACK:") < _idx(msgs, "ATTEMPT2"), "progress must precede the new attempt"
    assert _idx(msgs, "ATTEMPT1") < _idx(msgs, "FEEDBACK:"), "progress comes after the prior attempt"


async def test_act_progress_is_latest() -> None:
    """In the act prompt (new attempt not yet in memory), progress is the latest turn."""
    blocks = [
        _hist("user", "## Current Message\ndo X", 1),
        _hist("assistant", "ATTEMPT1 tried foo", 2),
    ]
    req = SimpleNamespace(purpose="act", task=_task(), session=SimpleNamespace(user_prompt="do X"),
                          template=None, bound_capabilities=[])
    msgs = DefaultComposer()._build_actor_messages(blocks, req)
    assert _idx(msgs, "ATTEMPT1") < _idx(msgs, "FEEDBACK:"), "progress after the prior attempt"
    assert "FEEDBACK:" in (msgs[-1].content if isinstance(msgs[-1].content, str) else "")


async def test_observe_includes_finish_output_labeled() -> None:
    """Observer sees the finish_task output, labeled as the act-phase finish_task result."""
    blocks = [
        _hist("user", "## Current Message\ndo X", 1),
        _hist("assistant", "ATTEMPT2 did Y", 10),
    ]
    req = SimpleNamespace(purpose="observe", task=_task(), session=SimpleNamespace(user_prompt="do X"),
                          template=None, actor_transcript=[])
    msgs = DefaultComposer()._build_observer_messages(blocks, req)
    joined = "\n".join(m.content for m in msgs if isinstance(m.content, str))
    assert "THE FINAL ANSWER" in joined
    assert "finish_task" in joined  # explicit label that this was the act-phase finish_task result


async def test_act_no_progress_without_timestamp() -> None:
    """No process_report_at → don't render Current Progress at all (no trailing fallback),
    so it can never be mis-placed; progress is only ever shown as a timestamped history block."""
    blocks = [_hist("user", "## Current Message\ndo X", 1), _hist("assistant", "ATTEMPT1", 2)]
    req = SimpleNamespace(purpose="act", task=_task(process_report_at=None),
                          session=SimpleNamespace(user_prompt="do X"),
                          template=None, bound_capabilities=[])
    msgs = DefaultComposer()._build_actor_messages(blocks, req)
    joined = "\n".join(m.content for m in msgs if isinstance(m.content, str))
    assert "FEEDBACK:" not in joined
    assert "Progress So Far" not in joined


async def test_progress_deduped_when_in_task_compact_summary() -> None:
    """max_turns round: the compact summary already carries process_report (now headed with
    '## Progress So Far' via _history for role=assistant/task_conversation), so the separate
    progress block must be suppressed — the report and heading should appear only once."""
    from ctx_weft.core.assembler.sources._history import PROGRESS_SO_FAR_HEADING
    report = _task().process_report  # exact string the composer compares against
    blocks = [
        # 直接构造段摘要的 _history 渲染态：role=assistant、冠 PROGRESS_SO_FAR_HEADING。
        _hist("assistant", f"{PROGRESS_SO_FAR_HEADING}\n{report}", 0,
              type=MemoryEventType.TASK_COMPACT_SUMMARY),
        _hist("assistant", "kept turn", 2),
    ]
    req = SimpleNamespace(purpose="act", task=_task(), session=SimpleNamespace(user_prompt="do X"),
                          template=None, bound_capabilities=[])
    msgs = DefaultComposer()._build_actor_messages(blocks, req)
    joined = "\n".join(m.content for m in msgs if isinstance(m.content, str))
    assert joined.count(PROGRESS_SO_FAR_HEADING) == 1   # no separate progress block (deduped)
    assert joined.count("FEEDBACK:") == 1               # only the compact summary carries it


async def test_progress_rendered_when_compact_summary_differs() -> None:
    """A TASK_COMPACT_SUMMARY with DIFFERENT content (rule-path dedicated summary) must NOT
    suppress Progress So Far — they are complementary, not duplicate."""
    blocks = [
        _hist("user", "[Context so far] neutral conversation summary", 0,
              type=MemoryEventType.TASK_COMPACT_SUMMARY),
        _hist("assistant", "kept turn", 2),
    ]
    req = SimpleNamespace(purpose="act", task=_task(), session=SimpleNamespace(user_prompt="do X"),
                          template=None, bound_capabilities=[])
    msgs = DefaultComposer()._build_actor_messages(blocks, req)
    joined = "\n".join(m.content for m in msgs if isinstance(m.content, str))
    assert "## Progress So Far" in joined
    assert "FEEDBACK:" in joined


async def test_observe_no_progress_without_timestamp() -> None:
    """No process_report_at → observe must NOT render Current Progress at all (avoid the stale
    trailing-after-new-attempt confusion); the act fallback above is fine, observe is not."""
    blocks = [
        _hist("user", "## Current Message\ndo X", 1),
        _hist("assistant", "ATTEMPT2 did Y", 10),
    ]
    req = SimpleNamespace(purpose="observe", task=_task(process_report_at=None),
                          session=SimpleNamespace(user_prompt="do X"), template=None, actor_transcript=[])
    msgs = DefaultComposer()._build_observer_messages(blocks, req)
    joined = "\n".join(m.content for m in msgs if isinstance(m.content, str))
    # 无 process_report_at → 不渲染进度块；进度内容(FEEDBACK)缺席即证（"Progress So Far" 字样
    # 本身会出现在 observe 判定提示里，故不以标题字样判定）。
    assert "FEEDBACK:" not in joined
