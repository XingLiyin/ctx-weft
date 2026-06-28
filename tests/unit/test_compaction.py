"""压缩机制全面测试（spec 2026-06-23）：触发 / (a)活跃task折叠 / (b)close短task /
(c)只折已结束root残留 / CompactStep 编排 / 边界 / 幂等。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from ctx_weft.core.assembler.assembler import AssembledPrompt
from ctx_weft.core.loop.driver import LoopState
from ctx_weft.core.loop.steps.compact import (
    CompactStep,
    _count_root_residues,
    fold_root_experience,
)
from ctx_weft.core.loop.steps.prepare import PrepareStep
from ctx_weft.core.state.models import Agent, NormalTaskSettings, Session, Task
from ctx_weft.protocols import MemoryEvent, MemoryEventType, MemoryScope, ProviderContext
from ctx_weft.protocols.template import LoopConfig
from ctx_weft.providers.memory_blackboard.in_memory import InMemoryMemoryProvider

pytestmark = pytest.mark.asyncio

T = MemoryEventType
_BASE = datetime(2026, 1, 1, tzinfo=UTC)


def _ctx() -> ProviderContext:
    return ProviderContext(session_id="s1", tenant_id="default")


def _sc(task_id="t1", agent_id="ag1") -> MemoryScope:
    return MemoryScope(session_id="s1", task_id=task_id, agent_id=agent_id)


def _ev(type_, scope, content, t, role=None, **meta):
    return MemoryEvent(type=type_, scope=scope, content=content,
                       timestamp=_BASE + timedelta(seconds=t), role=role, metadata=meta)


class _FakeTM:
    def __init__(self, tasks: dict[str, Task], children: dict[str, set[str]] | None = None):
        self._tasks = tasks
        self._children = children or {}

    def get_task(self, tid: str) -> Task | None:
        return self._tasks.get(tid)

    def children_of(self, tid: str) -> set[str]:
        return self._children.get(tid, set())


class _FakeAssembler:
    """summarize_for_compact 需要 ctx.assembler.assemble；返回空 prompt（内容由 _FakeLLM 摘要）。"""

    async def assemble(self, request) -> AssembledPrompt:
        return AssembledPrompt(system="", messages=[], tools=[], token_count=0)


class _FakeLLM:
    """summarize_for_compact 经 stream_llm_resilient 调 LLM 出摘要（compact 硬依赖 LLM，无截断兜底）。"""

    def __init__(self, text: str = "SUM"):
        self._text = text

    async def complete(self, request, *, stream: bool = True):
        from ctx_weft.protocols import LLMChunk

        yield LLMChunk(kind="token", text=self._text)


def _agent(cfg: LoopConfig, context_limit: int = 1000) -> Agent:
    a = Agent(id="ag1", session_id="s1", template_id="tpl", template_version="1",
              status="RUNNING", loop_config=cfg)
    a.loop_guard.context_limit = context_limit
    return a


def _state(active: Task, cfg: LoopConfig, context_limit: int = 1000) -> LoopState:
    session = Session(id="s1", user_prompt="u", status="RUNNING")
    return LoopState(run_id="run1", session=session, task=active,
                     agent=_agent(cfg, context_limit), scope=_sc(active.id, "ag1"))


def _loop_ctx(mem, tm, *, with_assembler: bool = False):
    return SimpleNamespace(
        memory=mem, provider_ctx=_ctx(), task_manager=tm,
        llm=_FakeLLM() if with_assembler else None,
        assembler=_FakeAssembler() if with_assembler else None,
    )


def _active_task() -> Task:
    return Task(id="t1", session_id="s1", status="ACTIVE", assigned_agent_id="ag1",
                creator_agent_id="ag1", title="Root", user_prompt="root",
                settings=NormalTaskSettings())


def _finished_short(tid: str, parent="t1", oc=None) -> Task:
    return Task(id=tid, session_id="s1", status="FINISHED", assigned_agent_id="ag1",
                creator_agent_id="ag1", parent_task_id=parent, origin_tool_call_id=oc,
                title="Sub", user_prompt="sub", outputs=f"{tid} answer",
                process_report="done", observer_outcome="success", settings=NormalTaskSettings())


async def _seed_root_residues(mem, sc, n: int, start_t: int = 0) -> None:
    """新 Task-6 格式：每个 root 胶囊 = 4x AGENT_CONVERSATION_TURN（parent_task_id=None）。"""
    for i in range(n):
        t0 = start_t + 4 * i
        tcid = f"rd{i}"
        await mem.ingest(_ev(T.AGENT_CONVERSATION_TURN, sc, f"user prompt rt{i}", t0,
                             role="user", origin_task_id=f"rt{i}", parent_task_id=None), _ctx())
        await mem.ingest(_ev(T.AGENT_CONVERSATION_TURN, sc, f"root res {i}", t0 + 1,
                             role="assistant", origin_task_id=f"rt{i}", parent_task_id=None), _ctx())
        await mem.ingest(_ev(T.AGENT_CONVERSATION_TURN, sc, "", t0 + 2,
                             role="assistant", origin_task_id=f"rt{i}", parent_task_id=None,
                             tool_calls=[{"id": tcid, "name": "control:finish_task", "input": {}}]), _ctx())
        await mem.ingest(_ev(T.AGENT_CONVERSATION_TURN, sc, f"Process Report: done rt{i}", t0 + 3,
                             role="tool", origin_task_id=f"rt{i}", parent_task_id=None,
                             tool_call_id=tcid), _ctx())


# ═══════════════════ _should_compact triggers ═══════════════════

async def test_trigger_token_ratio_over_and_under() -> None:
    mem = InMemoryMemoryProvider()
    cfg = LoopConfig(compact_token_ratio=0.8, compact_message_delta=10_000)  # delta off
    state, ctx = _state(_active_task(), cfg), _loop_ctx(mem, _FakeTM({}))
    step = PrepareStep()
    assert await step._should_compact(state, ctx, token_estimate=700) is False
    assert await step._should_compact(state, ctx, token_estimate=900) is True


async def test_trigger_no_division_when_limit_or_estimate_zero() -> None:
    mem = InMemoryMemoryProvider()
    cfg = LoopConfig(compact_message_delta=10_000)
    step = PrepareStep()
    # context_limit == 0 → skip token branch (no ZeroDivision), no growth → False
    s0 = _state(_active_task(), cfg, context_limit=0)
    assert await step._should_compact(s0, _loop_ctx(mem, _FakeTM({})), token_estimate=999) is False
    # token_estimate == 0 → skip token branch
    s1 = _state(_active_task(), cfg, context_limit=1000)
    assert await step._should_compact(s1, _loop_ctx(mem, _FakeTM({})), token_estimate=0) is False


async def test_trigger_growth_active_task_conversation() -> None:
    mem = InMemoryMemoryProvider()
    cfg = LoopConfig(compact_token_ratio=0.99, compact_message_delta=3)
    for i in range(3):
        await mem.ingest(_ev(T.LLM_RESPONSE, _sc("t1"), f"turn {i}", i, role="assistant"), _ctx())
    state = _state(_active_task(), cfg)
    assert await PrepareStep()._should_compact(state, _loop_ctx(mem, _FakeTM({})), token_estimate=1) is True


async def test_trigger_growth_root_residues() -> None:
    mem = InMemoryMemoryProvider()
    cfg = LoopConfig(compact_token_ratio=0.99, compact_message_delta=3)
    sc = _sc("t1")
    # 新格式：AGENT_CONVERSATION_TURN（parent=None）触发 root residue 计数
    await _seed_root_residues(mem, sc, 4)
    state = _state(_active_task(), cfg)
    assert await PrepareStep()._should_compact(state, _loop_ctx(mem, _FakeTM({})), token_estimate=1) is True


async def test_trigger_growth_finished_short_convs() -> None:
    mem = InMemoryMemoryProvider()
    cfg = LoopConfig(compact_token_ratio=0.99, compact_message_delta=3)
    tasks = {"t1": _active_task()}
    for i in range(4):
        tid = f"s{i}"
        await mem.ingest(_ev(T.LLM_RESPONSE, _sc(tid), f"short {i}", i, role="assistant"), _ctx())
        tasks[tid] = _finished_short(tid)
    state = _state(_active_task(), cfg)
    assert await PrepareStep()._should_compact(state, _loop_ctx(mem, _FakeTM(tasks)), token_estimate=1) is True


async def test_trigger_subtask_residues_do_not_trigger() -> None:
    mem = InMemoryMemoryProvider()
    cfg = LoopConfig(compact_token_ratio=0.99, compact_message_delta=3)
    sc = _sc("t1")
    # many sub-task residues (parent_task_id set) → working set, must NOT trigger
    for i in range(10):
        await mem.ingest(_ev(T.TASK_DISPATCH_RESULT, sc, f"sub {i}", i, role="tool",
                             tool_call_id=f"sd{i}", parent_task_id="t1"), _ctx())
    state = _state(_active_task(), cfg)
    assert await PrepareStep()._should_compact(state, _loop_ctx(mem, _FakeTM({})), token_estimate=1) is False


async def test_trigger_suspended_ancestor_conv_not_counted() -> None:
    mem = InMemoryMemoryProvider()
    cfg = LoopConfig(compact_token_ratio=0.99, compact_message_delta=3)
    # ancestor (SUSPENDED) has many conv turns, but they're not "finished short" → not counted
    for i in range(5):
        await mem.ingest(_ev(T.LLM_RESPONSE, _sc("anc"), f"anc {i}", i, role="assistant"), _ctx())
    anc = Task(id="anc", session_id="s1", status="SUSPENDED", assigned_agent_id="ag1",
               creator_agent_id="ag1", title="Anc", settings=NormalTaskSettings())
    state = _state(_active_task(), cfg)
    ctx = _loop_ctx(mem, _FakeTM({"anc": anc, "t1": _active_task()}))
    assert await PrepareStep()._should_compact(state, ctx, token_estimate=1) is False


async def test_trigger_delta_zero_disables_growth() -> None:
    mem = InMemoryMemoryProvider()
    cfg = LoopConfig(compact_token_ratio=0.99, compact_message_delta=0)
    sc = _sc("t1")
    for i in range(10):
        await mem.ingest(_ev(T.TASK_DISPATCH_RESULT, sc, f"r{i}", i, role="tool",
                             tool_call_id=f"rd{i}", parent_task_id=None), _ctx())
    state = _state(_active_task(), cfg)
    assert await PrepareStep()._should_compact(state, _loop_ctx(mem, _FakeTM({})), token_estimate=1) is False


# ═══════════════════ (b) close_finished_short_tasks — 删除（task-resident） ══════
# spec 2026-06-28 §5：取消「压力下回收 finished 短 task」。结束 task 的 body 留 task 层
# （即胶囊），由跨层 fold 管理。close_finished_short_tasks 函数已删，本节执行测试随之移除。


# ═══════════════════ (c) fold_root_experience ═══════════════════

async def test_fold_root_residues_keep_last_and_subtask_survive() -> None:
    mem = InMemoryMemoryProvider()
    sc = _sc("t1")
    await _seed_root_residues(mem, sc, 5)
    for i in range(2):  # sub-task residues (working set) — must NOT fold
        await mem.ingest(_ev(T.TASK_DISPATCH_RESULT, sc, f"sub res {i}", 200 + i, role="tool",
                             tool_call_id=f"sd{i}", child_task_id=f"st{i}", parent_task_id="t1"), _ctx())

    n = await fold_root_experience(_state(_active_task(), LoopConfig()),
                                   _loop_ctx(mem, _FakeTM({})), keep_last=1, summary_text="SUM")
    assert n > 0
    # 新格式：存活记录在 AGENT_CONVERSATION_TURN + AGENT_COMPACT_SUMMARY + TASK_DISPATCH_RESULT(working set)
    turns = await mem.recall_recent(sc, [T.AGENT_CONVERSATION_TURN], 100, _ctx())
    alive_origins = {r.metadata.get("origin_task_id") for r in turns}
    assert alive_origins == {"rt4"}                      # newest keep_last=1 root kept
    results = await mem.recall_recent(sc, [T.TASK_DISPATCH_RESULT], 100, _ctx())
    sub_contents = {r.content for r in results}
    assert {"sub res 0", "sub res 1"} <= sub_contents    # working set survives
    summ = await mem.recall_recent(sc, [T.AGENT_COMPACT_SUMMARY], 100, _ctx())
    assert any(r.content == "SUM" for r in summ)


async def test_fold_paired_dispatch_superseded_no_dangling() -> None:
    """新格式：所有已折胶囊的 AGENT_CONVERSATION_TURN 消失，最新 keep_last=1 的 origin=rt3 保留。"""
    mem = InMemoryMemoryProvider()
    sc = _sc("t1")
    await _seed_root_residues(mem, sc, 4)
    await fold_root_experience(_state(_active_task(), LoopConfig()),
                               _loop_ctx(mem, _FakeTM({})), keep_last=1, summary_text="SUM")
    # 新格式无 TASK_DISPATCH；检查 AGENT_CONVERSATION_TURN：只剩 rt3 的回合
    turns = await mem.recall_recent(sc, [T.AGENT_CONVERSATION_TURN], 100, _ctx())
    alive_origins = {r.metadata.get("origin_task_id") for r in turns}
    assert alive_origins == {"rt3"}  # only the kept root's turns remain


async def test_fold_rolls_old_summary_into_new() -> None:
    mem = InMemoryMemoryProvider()
    sc = _sc("t1")
    # an existing AGENT_COMPACT_SUMMARY from a prior fold
    await mem.ingest(_ev(T.AGENT_COMPACT_SUMMARY, sc, "OLD SUMMARY", 0, role="user"), _ctx())
    await _seed_root_residues(mem, sc, 3, start_t=10)
    await fold_root_experience(_state(_active_task(), LoopConfig()),
                               _loop_ctx(mem, _FakeTM({})), keep_last=1, summary_text="NEW")
    summaries = await mem.recall_recent(sc, [T.AGENT_COMPACT_SUMMARY], 100, _ctx())
    contents = {s.content for s in summaries}
    assert "OLD SUMMARY" not in contents and "NEW" in contents  # rolled into one


async def test_fold_at_or_below_keep_last_is_noop() -> None:
    mem = InMemoryMemoryProvider()
    sc = _sc("t1")
    await _seed_root_residues(mem, sc, 2)
    n = await fold_root_experience(_state(_active_task(), LoopConfig()),
                                   _loop_ctx(mem, _FakeTM({})), keep_last=6, summary_text="SUM")
    assert n == 0
    summaries = await mem.recall_recent(sc, [T.AGENT_COMPACT_SUMMARY], 100, _ctx())
    assert summaries == []  # nothing written


async def test_fold_summary_anchored_before_kept_window() -> None:
    mem = InMemoryMemoryProvider()
    sc = _sc("t1")
    await _seed_root_residues(mem, sc, 3)
    await fold_root_experience(_state(_active_task(), LoopConfig()),
                               _loop_ctx(mem, _FakeTM({})), keep_last=1, summary_text="SUM")
    # summary must be chronologically before all kept AGENT_CONVERSATION_TURN records
    turns = await mem.recall_recent(sc, [T.AGENT_CONVERSATION_TURN], 100, _ctx())
    summ = await mem.recall_recent(sc, [T.AGENT_COMPACT_SUMMARY], 100, _ctx())
    assert len(summ) == 1
    min_kept_ts = min(r.timestamp for r in turns)
    assert summ[0].timestamp < min_kept_ts            # summary anchored before kept window


async def test_count_root_residues_excludes_subtask() -> None:
    """新格式：root 胶囊（AGENT_CONVERSATION_TURN, parent=None）计 3；
    TASK_DISPATCH_RESULT(parent=t1) 为 working set，_count_root_residues 不计此类型。"""
    mem = InMemoryMemoryProvider()
    sc = _sc("t1")
    await _seed_root_residues(mem, sc, 3)
    # working set: cross-agent TASK_DISPATCH_RESULT with parent_task_id set (不是 AGENT_CONVERSATION_TURN)
    await mem.ingest(_ev(T.TASK_DISPATCH_RESULT, sc, "sub", 99, role="tool",
                         tool_call_id="sd", parent_task_id="t1"), _ctx())
    assert await _count_root_residues(_state(_active_task(), LoopConfig()), _loop_ctx(mem, _FakeTM({}))) == 3


# ═══════════════════ CompactStep.execute orchestration ═══════════════════

async def test_execute_noop_when_nothing_foldable() -> None:
    mem = InMemoryMemoryProvider()
    state = _state(_active_task(), LoopConfig(compact_keep_last=1))
    outcome = await CompactStep().execute(state, _loop_ctx(mem, _FakeTM({}), with_assembler=True))
    assert outcome.events == []
    assert await mem.recall_recent(_sc("t1"), [T.TASK_COMPACT_SUMMARY, T.AGENT_COMPACT_SUMMARY], 100, _ctx()) == []


async def test_execute_folds_task_and_root() -> None:
    mem = InMemoryMemoryProvider()
    sc = _sc("t1")
    for i in range(4):  # active task conv > keep_last
        await mem.ingest(_ev(T.LLM_RESPONSE, sc, f"turn {i}", i, role="assistant"), _ctx())
    await _seed_root_residues(mem, sc, 4, start_t=100)  # root residues > keep_last

    state = _state(_active_task(), LoopConfig(compact_keep_last=1))
    await CompactStep().execute(state, _loop_ctx(mem, _FakeTM({}), with_assembler=True))

    task_recs = await mem.recall_recent(sc, [T.LLM_RESPONSE, T.TASK_COMPACT_SUMMARY], 100, _ctx())
    assert any(r.type == T.TASK_COMPACT_SUMMARY for r in task_recs)          # (a) task folded
    agent_recs = await mem.recall_recent(sc, [T.AGENT_COMPACT_SUMMARY], 100, _ctx())
    assert agent_recs != []                                                  # (c) root folded


async def test_execute_task_fold_untouches_subtask_residues() -> None:
    mem = InMemoryMemoryProvider()
    sc = _sc("t1")
    for i in range(4):
        await mem.ingest(_ev(T.LLM_RESPONSE, sc, f"turn {i}", i, role="assistant"), _ctx())
    for i in range(3):  # only sub-task residues (no root residues)
        await mem.ingest(_ev(T.TASK_DISPATCH_RESULT, sc, f"sub {i}", 100 + i, role="tool",
                             tool_call_id=f"sd{i}", parent_task_id="t1"), _ctx())

    state = _state(_active_task(), LoopConfig(compact_keep_last=1))
    await CompactStep().execute(state, _loop_ctx(mem, _FakeTM({}), with_assembler=True))

    assert any(r.type == T.TASK_COMPACT_SUMMARY
               for r in await mem.recall_recent(sc, [T.TASK_COMPACT_SUMMARY], 100, _ctx()))
    subs = await mem.recall_recent(sc, [T.TASK_DISPATCH_RESULT], 100, _ctx())
    assert {r.content for r in subs} == {"sub 0", "sub 1", "sub 2"}          # working set survives
    assert await mem.recall_recent(sc, [T.AGENT_COMPACT_SUMMARY], 100, _ctx()) == []  # no root fold


async def test_execute_keeps_finished_short_body_then_folds_root() -> None:
    """task-resident（spec 2026-06-28 §5）：CompactStep 不再回收 finished 短 task——其 body 留
    task 层（即胶囊，由跨层 fold 管理）；root residues 仍照常 fold。"""
    mem = InMemoryMemoryProvider()
    sc = _sc("t1")
    # a finished short same-agent leaf + enough root residues to fold
    await mem.ingest(_ev(T.TASK_DISPATCH, sc, "", 0, role="assistant",
                         tool_call_id="oc2", tool_name="delegate_task", arguments={}), _ctx())
    await mem.ingest(_ev(T.LLM_RESPONSE, _sc("t2"), "sub work", 1, role="assistant"), _ctx())
    await _seed_root_residues(mem, sc, 4, start_t=100)
    tm = _FakeTM({"t1": _active_task(), "t2": _finished_short("t2", oc="oc2")})

    state = _state(_active_task(), LoopConfig(compact_keep_last=1))
    await CompactStep().execute(state, _loop_ctx(mem, tm, with_assembler=True))

    # task-resident：short task t2 的 body 留 task 层（不被 CompactStep 回收）
    conv = await mem.recall_recent_by_agent(_sc("x", "ag1"), [T.LLM_RESPONSE], 100, _ctx())
    assert any(r.metadata.get("task_id") == "t2" for r in conv), (
        "finished short task body must stay (task-resident: no pressure-collapse)"
    )
    # root residues 仍 fold
    assert await mem.recall_recent(sc, [T.AGENT_COMPACT_SUMMARY], 100, _ctx()) != []


async def test_execute_idempotent_second_run_noop() -> None:
    mem = InMemoryMemoryProvider()
    sc = _sc("t1")
    await _seed_root_residues(mem, sc, 4)
    state = _state(_active_task(), LoopConfig(compact_keep_last=1))
    ctx = _loop_ctx(mem, _FakeTM({}), with_assembler=True)

    await CompactStep().execute(state, ctx)
    after_first = await mem.recall_recent(sc, [T.TASK_DISPATCH_RESULT], 100, _ctx())
    out2 = await CompactStep().execute(state, ctx)  # nothing new to fold (root residues now ≤ keep_last)
    after_second = await mem.recall_recent(sc, [T.TASK_DISPATCH_RESULT], 100, _ctx())
    assert out2.events == []
    assert {r.id for r in after_first} == {r.id for r in after_second}  # stable


# ═══════════════════ pre-dispatch compaction (派发前压缩) ═══════════════════

async def test_predispatch_folds_task_and_agent_layers() -> None:
    """派发前压缩越过阈值时，与 CompactStep 同构地同时折 task 层与 agent 层，事件标 pre_dispatch。"""
    from ctx_weft.core.loop.steps.compact import maybe_compact_before_dispatch

    mem = InMemoryMemoryProvider()
    sc = _sc("t1")
    for i in range(4):  # active task conv > keep_last
        await mem.ingest(_ev(T.LLM_RESPONSE, sc, f"turn {i}", i, role="assistant"), _ctx())
    await _seed_root_residues(mem, sc, 4, start_t=100)  # completed root residues > keep_last

    cfg = LoopConfig(compact_keep_last=1, predispatch_compact_token_ratio=0.6)
    state = _state(_active_task(), cfg, context_limit=1000)
    # 800 / 1000 = 0.8 >= 0.6 → 门控通过
    events = await maybe_compact_before_dispatch(
        state, _loop_ctx(mem, _FakeTM({}), with_assembler=True), prompt_tokens=800)

    compacted = [e for e in events if e.type == "MemoryCompacted"]
    assert {e.payload.get("layer") for e in compacted} == {"task", "agent"}      # 两层都折
    assert all(e.payload.get("trigger") == "pre_dispatch" for e in events)
    assert any(r.type == T.TASK_COMPACT_SUMMARY
               for r in await mem.recall_recent(sc, [T.TASK_COMPACT_SUMMARY], 100, _ctx()))
    assert await mem.recall_recent(sc, [T.AGENT_COMPACT_SUMMARY], 100, _ctx()) != []


async def test_predispatch_gate_blocks_below_threshold() -> None:
    from ctx_weft.core.loop.steps.compact import maybe_compact_before_dispatch

    mem = InMemoryMemoryProvider()
    sc = _sc("t1")
    for i in range(4):
        await mem.ingest(_ev(T.LLM_RESPONSE, sc, f"turn {i}", i, role="assistant"), _ctx())
    await _seed_root_residues(mem, sc, 4, start_t=100)

    cfg = LoopConfig(compact_keep_last=1, predispatch_compact_token_ratio=0.6)
    state = _state(_active_task(), cfg, context_limit=1000)
    # 500 / 1000 = 0.5 < 0.6 → 不压
    events = await maybe_compact_before_dispatch(
        state, _loop_ctx(mem, _FakeTM({}), with_assembler=True), prompt_tokens=500)
    assert events == []
    assert await mem.recall_recent(sc, [T.TASK_COMPACT_SUMMARY, T.AGENT_COMPACT_SUMMARY], 100, _ctx()) == []
