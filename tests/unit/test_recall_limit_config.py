"""RecentMemorySource recalls ALL non-superseded records.

The recall window is NO LONGER derived from loop_config.max_turns_per_act — that conflated
"act-step size" with "how much history to recall" and silently truncated non-compacted
records (e.g. the original USER_PROMPT once a task runs long). Size is bounded by compaction
(supersedes old turns) + BudgetStrategy (token-budget, priority-aware).
"""

from __future__ import annotations

from types import SimpleNamespace

from ctx_weft.core.assembler.sources.short_memory import _RECALL_ALL, RecentMemorySource
from ctx_weft.protocols import MemoryScope


class _FakeMem:
    def __init__(self):
        self.limit = None

    async def recall_recent(self, scope, types, limit, ctx):
        self.limit = limit
        return []


def _deps(mem):
    return SimpleNamespace(memory=mem, provider_ctx=SimpleNamespace())


async def test_recall_not_capped_by_max_turns_per_act():
    """Recall must not be capped by the act-step size — it returns all active records."""
    src = RecentMemorySource()
    mem = _FakeMem()
    request = SimpleNamespace(
        scope=MemoryScope(session_id="s1", task_id="t1", agent_id="a1"),
        agent=SimpleNamespace(loop_config=SimpleNamespace(max_turns_per_act=7)),
    )
    async for _ in src.fetch(request, _deps(mem)):
        pass
    assert mem.limit == _RECALL_ALL
    assert mem.limit != 7


async def test_recall_all_regardless_of_agent():
    src = RecentMemorySource()
    mem = _FakeMem()
    request = SimpleNamespace(
        scope=MemoryScope(session_id="s1", task_id="t1", agent_id="a1"),
        agent=None,
    )
    async for _ in src.fetch(request, _deps(mem)):
        pass
    assert mem.limit == _RECALL_ALL
