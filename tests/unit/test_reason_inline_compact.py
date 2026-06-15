"""PrepareStep compacts inline (no compact Task pushed) and still routes to act."""

from __future__ import annotations

from types import SimpleNamespace

from loomex_core.core.loop.steps.prepare import PrepareStep


class _SpyCompact:
    def __init__(self):
        self.called = False

    async def execute(self, state, ctx):
        from loomex_core.core.loop.driver import StepOutcome
        self.called = True
        return StepOutcome(next_step=None, events=[])


async def test_reason_runs_compact_inline_and_routes_to_act(monkeypatch):
    spy = _SpyCompact()
    monkeypatch.setattr("loomex_core.core.loop.steps.prepare.CompactStep", lambda: spy)

    pushed = []

    rs = PrepareStep()
    async def _est(state, ctx):
        return 100000, True
    async def _resolve(state, ctx):
        return []
    async def _skill(state, ctx, name):
        return ""
    async def _should(state, ctx, est):
        return True

    monkeypatch.setattr(rs, "_estimate_tokens", _est)
    monkeypatch.setattr("loomex_core.core.loop.steps.prepare.resolve_and_bind", _resolve)
    monkeypatch.setattr(rs, "_load_skill_instructions", _skill)
    monkeypatch.setattr(rs, "_should_compact", _should)

    agent = SimpleNamespace(
        id="a1",
        loop_config=SimpleNamespace(compact_keep_last=2, compact_token_ratio=0.8,
                                    compact_message_delta=0),
        loop_guard=SimpleNamespace(context_limit=1000, context_tokens=0,
                                   context_message_count=0, last_compact_at_message=0),
    )
    session = SimpleNamespace(id="s1", tenant_id="te1", token_budget=0, token_used=0,
                              context_limit=1000)
    task = SimpleNamespace(id="t1", parent_task_id="p", title="T",
                           settings=SimpleNamespace(skill_name="", purpose="act"))
    state = SimpleNamespace(agent=agent, session=session, task=task,
                            scope=SimpleNamespace(), extra={"template": None},
                            run_id="r1", sequence_counter=0)

    class _Assembler:
        def __init__(self):
            self.calls = 0
        async def assemble(self, request):
            self.calls += 1
            return SimpleNamespace(token_count=10, system="", messages=[], tools=[])

    class _TM:
        async def push_task(self, *a, **k):
            pushed.append(a)

    class _Bus:
        async def emit(self, ev):
            pass

    asm = _Assembler()
    ctx = SimpleNamespace(assembler=asm, task_manager=_TM(), event_bus=_Bus(),
                          memory=SimpleNamespace(), provider_ctx=SimpleNamespace())

    outcome = await rs.execute(state, ctx)
    assert spy.called is True          # compaction ran inline
    assert pushed == []                # no compact Task pushed
    assert outcome.next_step == "act"  # still proceeds to act
    assert asm.calls == 2              # assembled once, then re-assembled after compaction


async def test_reason_stashes_bound_capabilities(monkeypatch):
    rs = PrepareStep()
    sentinel = [object()]

    async def _est(state, ctx):
        return 10, True

    async def _resolve(state, ctx):
        return sentinel

    async def _skill(state, ctx, name):
        return ""

    async def _should(state, ctx, est):
        return False  # no compaction this run

    monkeypatch.setattr(rs, "_estimate_tokens", _est)
    monkeypatch.setattr("loomex_core.core.loop.steps.prepare.resolve_and_bind", _resolve)
    monkeypatch.setattr(rs, "_load_skill_instructions", _skill)
    monkeypatch.setattr(rs, "_should_compact", _should)

    agent = SimpleNamespace(
        id="a1",
        loop_config=SimpleNamespace(compact_keep_last=2, compact_token_ratio=0.8,
                                    compact_message_delta=0),
        loop_guard=SimpleNamespace(context_limit=1000, context_tokens=0,
                                   context_message_count=0, last_compact_at_message=0),
    )
    session = SimpleNamespace(id="s1", tenant_id="te1", token_budget=0, token_used=0,
                              context_limit=1000)
    task = SimpleNamespace(id="t1", parent_task_id="p", title="T",
                           settings=SimpleNamespace(skill_name="", purpose="act"))
    state = SimpleNamespace(agent=agent, session=session, task=task,
                            scope=SimpleNamespace(), extra={"template": None},
                            run_id="r1", sequence_counter=0)

    class _Assembler:
        async def assemble(self, request):
            return SimpleNamespace(token_count=10, system="", messages=[], tools=[])

    class _Bus:
        async def emit(self, ev):
            pass

    ctx = SimpleNamespace(assembler=_Assembler(), task_manager=None, event_bus=_Bus(),
                          memory=SimpleNamespace(), provider_ctx=SimpleNamespace())

    await rs.execute(state, ctx)
    assert state.extra["bound_capabilities"] is sentinel
