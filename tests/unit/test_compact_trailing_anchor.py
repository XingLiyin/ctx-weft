"""Trailing-segment fold anchor + resume barrier (multi-turn "continue task" bug).

Root cause of the intermittent "user input ignored → continue-task cue → blank reply":
a detached plain_text background observe folds the just-finished segment into a
TASK_COMPACT_SUMMARY. When the folded block has no surviving event after it (the
single-segment plain_text case: [USER_PROMPT, LLM_RESPONSE]), apply_compact used to
anchor the summary to wall-clock now(). If that detached fold completes AFTER the next
turn's USER_PROMPT is already ingested, the now()-stamped summary sorts AFTER the new
user message, so the assembler sees history ending in a (assistant-role) summary,
appends the "continue task" resume cue, and buries the real new request → blank reply.

Fix 2 (this file, provider level): a trailing-segment fold summary must be anchored to
the folded segment's own position (the last archived event's timestamp), never to now(),
so a later-arriving USER_PROMPT always sorts after it.

Fix 1 (this file, runtime level): _inject_user_reply must await any in-flight background
observe for the resumed task before ingesting the new USER_PROMPT, so the fold's write
always precedes the new turn's prompt.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from ctx_weft.core.state.models import Session, Task
from ctx_weft.protocols import (
    MemoryEvent,
    MemoryEventType,
    MemoryScope,
    MemoryAddress,
    ProviderContext,
)
from ctx_weft.protocols.hitl import HitlRequest
from ctx_weft.providers.memory.in_memory import InMemoryMemoryProvider

pytestmark = pytest.mark.asyncio

T = MemoryEventType


def _pctx() -> ProviderContext:
    return ProviderContext(session_id="s1", tenant_id="default", task_id="t1", agent_id="a1")


def _scope() -> MemoryAddress:
    return MemoryAddress(session_id="s1", task_id="t1", agent_id="a1")


def _ts(offset_us: int) -> datetime:
    base = datetime(2024, 1, 1, 12, 0, 0, tzinfo=UTC)
    return base + timedelta(microseconds=offset_us)


async def _fold_trailing_segment(mem: InMemoryMemoryProvider) -> None:
    """Ingest a single plain_text segment [UP1, LLM] and fold it (plain_text boundary)."""
    pctx = _pctx()
    scope = _scope()
    await mem.ingest(MemoryEvent(type=T.USER_PROMPT, address=scope, content="原始诉求",
                                 timestamp=_ts(1), role="user"), pctx)
    await mem.ingest(MemoryEvent(type=T.LLM_RESPONSE, address=scope, content="上一轮回复",
                                 timestamp=_ts(2), role="assistant"), pctx)
    from ctx_weft.core.loop.steps.segment_fold import segment_fold
    await segment_fold(mem, scope, MemoryScope.TASK, "段摘要", pctx)


# ── Fix 2: provider anchor ─────────────────────────────────────────────────────


async def test_trailing_fold_summary_anchored_to_segment_not_now():
    """A trailing-segment fold summary is anchored to the last archived event's timestamp,
    NOT wall-clock now()."""
    mem = InMemoryMemoryProvider()
    await _fold_trailing_segment(mem)

    summaries = await mem.recall_recent(_scope(), [T.TASK_COMPACT_SUMMARY], 10, _pctx())
    assert len(summaries) == 1
    # The folded LLM_RESPONSE sat at _ts(2); the summary must inherit that position.
    assert summaries[0].timestamp == _ts(2), (
        f"summary anchored to {summaries[0].timestamp} (now()?) instead of the folded "
        f"segment position {_ts(2)}"
    )


async def test_user_prompt_after_trailing_fold_sorts_newest():
    """After folding a trailing segment, a freshly ingested USER_PROMPT must be the newest
    record — the fold summary must never leapfrog the new user message."""
    mem = InMemoryMemoryProvider()
    await _fold_trailing_segment(mem)

    # Next turn: the user's new message is injected (later than the folded segment).
    await mem.ingest(MemoryEvent(type=T.USER_PROMPT, address=_scope(), content="新问题",
                                 timestamp=_ts(1000), role="user"), _pctx())

    recent = await mem.recall_recent(
        _scope(),
        [T.USER_PROMPT, T.LLM_RESPONSE, T.TASK_COMPACT_SUMMARY],
        10, _pctx(),
    )
    # recall_recent returns newest-first.
    assert recent[0].content == "新问题", (
        f"newest record is {recent[0].type}:{recent[0].content!r}, not the new USER_PROMPT — "
        "the fold summary leapfrogged the new user message"
    )


# ── Fix 1: resume barrier ──────────────────────────────────────────────────────


async def test_inject_user_reply_awaits_pending_background_observe(monkeypatch):
    """_inject_user_reply must await the resumed task's in-flight background observe before
    ingesting the new USER_PROMPT, so the fold's write precedes the new turn's prompt."""
    from tests.integration.test_minimal_loop import InlineAgentTemplateProvider, make_runtime
    from ctx_weft.core import CtxWeftRuntime
    import ctx_weft.core.loop.steps.background_observe as bo

    order: list[str] = []

    class _RecordingMem(InMemoryMemoryProvider):
        async def ingest(self, event, ctx):
            from ctx_weft.protocols import MemoryKind
            # v2 词汇：user 回合 = kind CONVERSATION_TURN + role user（旧 type 兜底兼容）
            if (event.type is T.USER_PROMPT
                    or (event.kind is MemoryKind.CONVERSATION_TURN and event.role == "user")):
                order.append("ingest")
            return await super().ingest(event, ctx)

    runtime = make_runtime(agent_provider=InlineAgentTemplateProvider())
    mem = _RecordingMem()
    runtime.providers.register_memory(mem)

    monkeypatch.setattr(bo, "_task_pending", {})

    async def _slow_fold():
        await asyncio.sleep(0.05)
        order.append("fold")

    bo._task_pending["t1"] = asyncio.ensure_future(_slow_fold())

    session = Session(id="s1", tenant_id="default", user_prompt="hi", status="RUNNING")
    task = Task(id="t1", session_id="s1", status="SUSPENDED",
                assigned_agent_id="a1", creator_agent_id="a1")
    task_manager = SimpleNamespace(get_task=lambda tid: task if tid == "t1" else None)
    req = HitlRequest(id="h1", form="wait", session_id="s1", task_id="t1", agent_id="a1",
                      outcome="accepted", message="新问题", context="plain_text")

    await runtime._inject_user_reply(req, session, task_manager)

    assert order == ["fold", "ingest"], (
        f"expected the fold to complete before the reply is ingested, got {order}"
    )
