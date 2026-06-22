"""RecentMemorySource derives its recall limit from loop_config.max_turns_per_act."""

from __future__ import annotations

from types import SimpleNamespace

from ctx_weft.core.assembler.sources.short_memory import RecentMemorySource
from ctx_weft.protocols import MemoryScope


class _FakeMem:
    def __init__(self):
        self.limit = None

    async def recall_recent(self, scope, types, limit, ctx):
        self.limit = limit
        return []


def _deps(mem):
    return SimpleNamespace(memory=mem, provider_ctx=SimpleNamespace())


async def test_recall_limit_derives_from_max_turns_per_act():
    src = RecentMemorySource(limit=40)
    mem = _FakeMem()
    request = SimpleNamespace(
        scope=MemoryScope(session_id="s1", task_id="t1", agent_id="a1"),
        agent=SimpleNamespace(loop_config=SimpleNamespace(max_turns_per_act=7)),
    )
    async for _ in src.fetch(request, _deps(mem)):
        pass
    assert mem.limit == 7


async def test_recall_limit_falls_back_when_no_agent():
    src = RecentMemorySource(limit=5)
    mem = _FakeMem()
    request = SimpleNamespace(
        scope=MemoryScope(session_id="s1", task_id="t1", agent_id="a1"),
        agent=None,
    )
    async for _ in src.fetch(request, _deps(mem)):
        pass
    assert mem.limit == 5
