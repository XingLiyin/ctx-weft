"""PrepareStep routes compaction through the driver, then rebuilds exactly once."""

from __future__ import annotations

from tests.model_testkit import model_snapshot

from types import SimpleNamespace

from ctx_weft.core.loop.steps.prepare import PrepareStep
from ctx_weft.core.utils.estimate import estimate_tokens
from ctx_weft.protocols.events import EventOrigin
from tests.assembler_testkit import compiled_test_inputs, scripted_inputs


class _SpyEscalatingCompact:
    def __init__(self):
        self.called = False
        self.kwargs = None

    async def __call__(self, state, ctx, **kwargs):
        self.order = list(getattr(ctx.task_manager, "committed", []))
        self.called = True
        self.origin = state.origin
        self.kwargs = kwargs
        return []


async def test_prepare_routes_to_compact_then_reassembles_without_retrigger(monkeypatch):
    spy = _SpyEscalatingCompact()
    monkeypatch.setattr("ctx_weft.core.loop.steps.compact._run_compact", spy)

    pushed = []

    rs = PrepareStep()
    async def _est(state, ctx):
        return 100000, True
    async def _resolve(state, ctx):
        return []
    async def _should(state, ctx, est):
        return True

    monkeypatch.setattr(rs, "_estimate_tokens", _est)
    monkeypatch.setattr("ctx_weft.core.loop.steps.prepare.resolve_and_bind", _resolve)
    monkeypatch.setattr(rs, "_should_compact", _should)

    agent = SimpleNamespace(
        id="a1",
        loop_config=SimpleNamespace(compact_keep_last=2, compact_token_ratio=0.8,
                                    compact_message_delta=0),
        loop_guard=SimpleNamespace(context_limit=1000, context_tokens=0,
                                   context_message_count=0),
    )
    session = SimpleNamespace(id="s1", tenant_id="te1", token_budget=0, token_used=0,
                              context_limit=1000)
    task = SimpleNamespace(id="t1", parent_task_id="p", title="T",
                           settings=SimpleNamespace(skill_name="", purpose="act"))
    state = SimpleNamespace(resolved_model=model_snapshot(context_limit=1000), agent=agent, session=session, task=task,
                            scope=SimpleNamespace(), extra={"template": None},
                            run_id="r1", sequence_counter=0, origin=EventOrigin.LOOP_PREPARE)

    class _Assembler:
        def __init__(self):
            self.calls = 0
        async def compile(self, inputs, **kwargs):
            return await compiled_test_inputs(inputs, **kwargs)

        async def assemble(self, request):
            self.calls += 1
            return scripted_inputs(SimpleNamespace(token_count=10, system="", messages=[], tools=[]), purpose=request.purpose)

    class _TM:
        def __init__(self):
            self.committed = []

        async def push_task(self, *a, **k):
            pushed.append(a)

        async def commit_round(self, task_id):
            self.committed.append(task_id)

    class _Bus:
        async def emit(self, ev):
            pass

    asm = _Assembler()
    ctx = SimpleNamespace(assembler=asm, task_manager=_TM(), event_bus=_Bus(),
                          memory=SimpleNamespace(), provider_ctx=SimpleNamespace(),
                          llm=SimpleNamespace(tokenizer=SimpleNamespace(count=estimate_tokens)))

    outcome = await rs.execute(state, ctx)
    assert not spy.called
    assert outcome.next_step == "compact"
    assert outcome.next_step_options == {
        "trigger": "prepare_budget", "token_estimate": 100000,
        "force": False, "after": "prepare",
    }
    assert pushed == []
    assert asm.calls == 1
    assert ctx.task_manager.committed == ["t1"]
    ctx.execution = SimpleNamespace(options={"after_compact": True})
    rebuilt = await rs.execute(state, ctx)
    assert rebuilt.next_step == "act"
    assert rebuilt.next_step_options == {}
    assert asm.calls == 2
    assert not spy.called



async def test_reason_stashes_bound_capabilities(monkeypatch):
    rs = PrepareStep()
    sentinel = [object()]

    async def _est(state, ctx):
        return 10, True

    async def _resolve(state, ctx):
        return sentinel

    async def _should(state, ctx, est):
        return False  # no compaction this run

    monkeypatch.setattr(rs, "_estimate_tokens", _est)
    monkeypatch.setattr("ctx_weft.core.loop.steps.prepare.resolve_and_bind", _resolve)
    monkeypatch.setattr(rs, "_should_compact", _should)

    agent = SimpleNamespace(
        id="a1",
        loop_config=SimpleNamespace(compact_keep_last=2, compact_token_ratio=0.8,
                                    compact_message_delta=0),
        loop_guard=SimpleNamespace(context_limit=1000, context_tokens=0,
                                   context_message_count=0),
    )
    session = SimpleNamespace(id="s1", tenant_id="te1", token_budget=0, token_used=0,
                              context_limit=1000)
    task = SimpleNamespace(id="t1", parent_task_id="p", title="T",
                           settings=SimpleNamespace(skill_name="", purpose="act"))
    state = SimpleNamespace(resolved_model=model_snapshot(context_limit=1000), agent=agent, session=session, task=task,
                            scope=SimpleNamespace(), extra={"template": None},
                            run_id="r1", sequence_counter=0, origin=EventOrigin.LOOP_PREPARE)

    class _Assembler:
        async def compile(self, inputs, **kwargs):
            return await compiled_test_inputs(inputs, **kwargs)

        async def assemble(self, request):
            return scripted_inputs(SimpleNamespace(token_count=10, system="", messages=[], tools=[]), purpose=request.purpose)

    class _Bus:
        async def emit(self, ev):
            pass

    ctx = SimpleNamespace(assembler=_Assembler(), task_manager=None, event_bus=_Bus(),
                          memory=SimpleNamespace(), provider_ctx=SimpleNamespace(),
                          llm=SimpleNamespace(tokenizer=SimpleNamespace(count=estimate_tokens)))

    await rs.execute(state, ctx)
    assert state.extra["bound_capabilities"] is sentinel
