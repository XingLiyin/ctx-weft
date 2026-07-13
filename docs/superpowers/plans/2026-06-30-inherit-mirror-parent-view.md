# Inherit Mirror (Parent View) Implementation Plan — Phase 2

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** When a sub-task spawns in a fresh sub-agent, make `_copy_memory_for_inherit` reproduce the parent agent's *current recall view* — so inherited same-agent sibling bodies arrive **framed** (no naked leak) and cross-agent siblings arrive as **bubbles** — by copying the parent's agent-layer dispatch turns in addition to its task-layer body.

**Architecture:** Today `_copy_memory_for_inherit` copies only the parent's task-layer body (`recall_recent_by_agent`), which (a) sweeps in same-agent siblings' raw bodies *without their dispatch frames* (the "请向 Lily 问好" naked-leak), and (b) misses cross-agent siblings entirely (their representation in the parent scope is an agent-layer `AGENT_CONVERSATION_TURN`, excluded by the task-layer filter). Phase 1 made every dispatched child own a `start_task` frame + paired result in the parent agent scope. Phase 2 adds a second recall for those agent-layer turns and merges both streams by `(timestamp, seq_no)` before re-ingesting into the child scope — exactly the union `AgentRecallSource` assembles for the parent. Result: same-agent siblings = full *framed* capsule (frame → body → finish pair); cross-agent siblings = frame → bubble.

**Tech Stack:** Python 3.11, `ctx_weft` core, `InMemoryMemoryProvider`, pytest (`uv run pytest` from `ctx-weft/`).

## Global Constraints

- Tests run with `uv run pytest` from `ctx-weft/` (pyproject sets `pythonpath=["."]`).
- Do NOT re-key `origin_task_id` on any copied record. The copy preserves each record's role, content, timestamp, and tool pairing metadata (`tool_calls` for assistant, `tool_call_id` for tool) exactly as Phase 1 wrote them; it only changes the *event type* to `AGENT_CONVERSATION_TURN` in the child scope (existing behaviour) and tags `inherited_from_task_id`.
- **Keep `AGENT_COMPACT_SUMMARY` excluded** from the copy. The existing design intent (runtime.py:86-88) is that the parent's folded black-box dispatch log is of little use to a child; Phase 2 adds only un-folded `AGENT_CONVERSATION_TURN` turns (the current plan's frames/bubbles/finish pairs), not folded summaries.
- Merge the two recalls by `(timestamp, metadata["seq_no"])` ascending before re-ingest, so the new per-child-scope ingest order (which becomes the child's recall order) preserves frame-before-result and chronological sibling ordering. This mirrors how `composer._history_to_messages_with_sources` already sorts (composer.py:677).
- Phase 2 depends on Phase 1 (`docs/superpowers/plans/2026-06-30-per-task-start-task-frames.md`, commits `f67b8c8`/`4c7a28f`/`bdfe38c`) being present — the `start_task` frames it copies do not exist without it.

## Out of Scope — Phase 3 (separate plan, open decisions first)

Blackboard predecessor-recall removal is **deferred to Phase 3** and intentionally not in this plan, because it has unresolved design decisions that should be settled (brainstormed) before planning:

1. **Predecessor-only vs. also subtask.** After Phase 2, predecessor sibling results are already visible through normal recall: a same-agent successor sees them via its own `AgentRecallSource` agent-layer recall (`recall_recent` on the shared agent scope returns sibling frames/bubbles); a sub-agent successor sees them via this plan's inherit copy. So the **predecessor** blackboard recall (`BlackboardSource` `intent=predecessor` → composer `## Task Background`) becomes redundant. The **subtask** path (`intent=subtask`) is a *different* affordance and is NOT subsumed.
2. **Observer confirm/reopen affordance.** `composer._build_observer_messages` (composer.py:736-749) renders subtask blackboard blocks as "your sub-task results (you may confirm / reopen these)". Removing the subtask blackboard would drop that structured review affordance; a replacement (derive from `origin_task_id` + `task.status`) must be designed first.

Settle (1) and (2) before writing the Phase 3 plan. This Phase 2 plan stands alone and produces working, reviewable software (cross-agent visibility for sub-agents + naked-leak fix) without touching blackboard.

---

## File Structure

- `src/ctx_weft/core/runtime.py` — extend `_copy_memory_for_inherit` (lines 73-123): add the agent-layer recall, merge, and update the docstring/comment to reflect the mirrored view.
- `tests/unit/test_inherit_mirror.py` — NEW focused test file for `_copy_memory_for_inherit` (constructs a parent scope with a same-agent sibling capsule and a cross-agent sibling bubble, runs the copy, asserts the child scope mirrors the parent's framed view with no naked body).

---

### Task 1: Mirror the parent's agent-layer turns into the inherited child scope

**Files:**
- Modify: `src/ctx_weft/core/runtime.py:73-123` (`_copy_memory_for_inherit`)
- Test: `tests/unit/test_inherit_mirror.py` (new)

**Interfaces:**
- Consumes: `memory.recall_recent_by_agent(agent_scope, types, limit, ctx)` and `memory.recall_recent(scope, types, limit, ctx)` (both return newest-first `MemoryRecord`s, each carrying `metadata["seq_no"]`); `MemoryEvent`, `MemoryEventType`, `MemoryScope` (imported inside the function as today); the Phase-1 `start_task` frames (`AGENT_CONVERSATION_TURN`, assistant, `tool_calls=[{id, name=control:start_task,…}]`) and dispatch results already present in the parent agent scope.
- Produces: after the copy, the child scope (`task=child_task.id, agent=sub_agent.id`) contains, in chronological `(timestamp, seq_no)` order, the parent's task-body turns AND the parent's `AGENT_CONVERSATION_TURN` turns, all re-typed `AGENT_CONVERSATION_TURN`, each tagged `inherited_from_task_id=parent_task.id`, with `tool_calls`/`tool_call_id` preserved. `AGENT_COMPACT_SUMMARY` is not copied.

- [ ] **Step 1: Write the failing test**

Create `tests/unit/test_inherit_mirror.py`:

```python
"""_copy_memory_for_inherit mirrors the parent's recall view (spec Phase 2 2026-06-30).

Same-agent sibling → framed full capsule (start_task frame precedes its body); cross-agent
sibling → frame + bubble. No naked sibling body without its dispatch frame.
"""
import pytest
from datetime import datetime, timezone, timedelta

from ctx_weft.core.runtime import _copy_memory_for_inherit
from ctx_weft.core.state.models import Task, NormalTaskSettings
from ctx_weft.protocols import MemoryEvent, MemoryEventType as T, MemoryScope
from ctx_weft.protocols.context import ProviderContext
from ctx_weft.providers.memory_blackboard.in_memory import InMemoryMemoryProvider

_BASE = datetime(2026, 6, 30, 12, 0, 0, tzinfo=timezone.utc)


def _ctx():
    return ProviderContext(session_id="s1", tenant_id="default")


def _ev(typ, scope, content, *, role, ts, md=None):
    return MemoryEvent(type=typ, scope=scope, content=content, timestamp=ts, role=role,
                       metadata=md or {})


@pytest.mark.asyncio
async def test_inherit_mirrors_frames_and_bubbles():
    mem = InMemoryMemoryProvider()
    sess, parent_agent = "s1", "agR"
    parent_task_id = "p1"
    parent_scope = MemoryScope(session_id=sess, task_id=parent_task_id, agent_id=parent_agent)

    # parent's own body (plan task) — task layer
    await mem.ingest(_ev(T.USER_PROMPT, parent_scope, "提交一个 plan", role="user", ts=_BASE), _ctx())
    await mem.ingest(_ev(T.LLM_RESPONSE, parent_scope, "好的", role="assistant", ts=_BASE + timedelta(seconds=1)), _ctx())

    # same-agent sibling Lily: start_task frame (back-dated) + her body (task layer, later) — agent+task layers
    await mem.ingest(_ev(T.AGENT_CONVERSATION_TURN, parent_scope, "", role="assistant",
                         ts=_BASE + timedelta(seconds=2),
                         md={"origin_task_id": parent_task_id,
                             "tool_calls": [{"id": "tc_lily", "name": "control:start_task", "input": {}}]}), _ctx())
    lily_scope = MemoryScope(session_id=sess, task_id="lily", agent_id=parent_agent)  # same agent
    await mem.ingest(_ev(T.USER_PROMPT, lily_scope, "请向 Lily 问好", role="user",
                         ts=_BASE + timedelta(seconds=5)), _ctx())

    # cross-agent sibling Amy: start_task frame + bubble (both agent layer, parent scope)
    await mem.ingest(_ev(T.AGENT_CONVERSATION_TURN, parent_scope, "", role="assistant",
                         ts=_BASE + timedelta(seconds=3),
                         md={"origin_task_id": parent_task_id,
                             "tool_calls": [{"id": "tc_amy", "name": "control:start_task", "input": {}}]}), _ctx())
    await mem.ingest(_ev(T.AGENT_CONVERSATION_TURN, parent_scope, "你好 Amy！👋", role="tool",
                         ts=_BASE + timedelta(seconds=3),
                         md={"origin_task_id": parent_task_id, "tool_call_id": "tc_amy"}), _ctx())

    parent_task = Task(id=parent_task_id, session_id=sess, status="SUSPENDED", tenant_id="default",
                       assigned_agent_id=parent_agent, creator_agent_id=parent_agent,
                       settings=NormalTaskSettings())
    child_task = Task(id="andy", session_id=sess, status="PENDING", tenant_id="default",
                      creator_agent_id=parent_agent, parent_task_id=parent_task_id,
                      settings=NormalTaskSettings())
    sub_agent = type("A", (), {"id": "agB"})()

    await _copy_memory_for_inherit(
        parent_task=parent_task, child_task=child_task, sub_agent=sub_agent,
        memory=mem, session_id=sess, tenant_id="default")

    child_scope = MemoryScope(session_id=sess, task_id="andy", agent_id="agB")
    turns = await mem.recall_recent(child_scope, [T.AGENT_CONVERSATION_TURN], 100, _ctx())
    # recall_recent returns newest-first; flip to chronological for ordering assertions
    chrono = list(reversed(turns))
    contents = [(t.role, t.content) for t in chrono]

    # cross-agent Amy bubble is present (was previously MISSING)
    assert any(role == "tool" and "你好 Amy" in c for role, c in contents), \
        f"cross-agent bubble must be inherited; got {contents}"

    # same-agent Lily body present AND preceded by a start_task frame (was previously a NAKED leak)
    lily_body_idx = next(i for i, (role, c) in enumerate(contents) if "请向 Lily 问好" in c)
    preceding_frames = [t for t in chrono[:lily_body_idx]
                        if t.role == "assistant"
                        and any(tc.get("id") == "tc_lily" for tc in (t.metadata.get("tool_calls") or []))]
    assert preceding_frames, "Lily's body must be preceded by its start_task frame (no naked leak)"

    # everything carries inherited_from_task_id
    assert all(t.metadata.get("inherited_from_task_id") == parent_task_id for t in chrono)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd ctx-weft && uv run pytest tests/unit/test_inherit_mirror.py -v`
Expected: FAIL — the cross-agent bubble assertion fails (current copy uses only `recall_recent_by_agent` with task-body types, so the agent-layer `tool` bubble for Amy is never copied; and Lily's body has no preceding frame).

- [ ] **Step 3: Locate and read the current implementation**

Open `src/ctx_weft/core/runtime.py:73-123` (`_copy_memory_for_inherit`). The body currently is a single `records = await memory.recall_recent_by_agent(...)` followed by `for r in reversed(records):` that ingests each into `child_scope`.

- [ ] **Step 4: Replace the recall + loop with the merged two-stream copy**

Replace the function body from the `records = await memory.recall_recent_by_agent(` call through the end of the `for r in reversed(records):` loop with:

```python
    # Mirror the parent agent's current recall view (spec Phase 2, 2026-06-30):
    #   (a) task-layer body — parent's own turns + same-agent siblings' bodies (by agent_id), and
    #   (b) agent-layer dispatch turns — the start_task frames, cross-agent bubbles, and same-agent
    #       finish pairs that Phase 1 writes into the parent agent scope.
    # Merging both by (timestamp, seq_no) means inherited same-agent sibling bodies arrive FRAMED
    # (their start_task frame precedes them, so no naked leak) and cross-agent siblings arrive as
    # bubbles. AGENT_COMPACT_SUMMARY is still excluded — the parent's folded black-box dispatch log
    # is of little use to a child.
    body_records = await memory.recall_recent_by_agent(
        agent_scope=parent_scope,
        types=[
            MemoryEventType.USER_PROMPT,
            MemoryEventType.LLM_RESPONSE,
            MemoryEventType.TOOL_RESULT,
            MemoryEventType.TASK_COMPACT_SUMMARY,
        ],
        limit=2000,
        ctx=ctx,
    )
    frame_records = await memory.recall_recent(
        scope=parent_scope,
        types=[MemoryEventType.AGENT_CONVERSATION_TURN],
        limit=2000,
        ctx=ctx,
    )
    combined = sorted(
        [*body_records, *frame_records],
        key=lambda r: (r.timestamp, r.metadata.get("seq_no", 0)),
    )
    child_scope = MemoryScope(session_id=session_id, task_id=child_task.id, agent_id=sub_agent.id)
    for r in combined:  # chronological → re-ingest preserves order via fresh per-scope seq_no
        md = {"inherited_from_task_id": parent_task.id}
        if r.role == "assistant" and r.metadata.get("tool_calls"):
            md["tool_calls"] = r.metadata["tool_calls"]
        if r.role == "tool" and r.metadata.get("tool_call_id"):
            md["tool_call_id"] = r.metadata["tool_call_id"]
        await memory.ingest(
            MemoryEvent(
                type=MemoryEventType.AGENT_CONVERSATION_TURN,
                scope=child_scope,
                content=r.content,
                timestamp=r.timestamp,
                role=r.role,
                metadata=md,
            ),
            ctx,
        )
```

- [ ] **Step 5: Update the function docstring/comment to reflect the mirrored view**

Replace the docstring + the `# Snapshots the parent agent's OPEN-task conversation only …` comment (runtime.py:81-88) with:

```python
    """spawn 时把 parent agent 的当前召回视图复制进 child agent scope（spec Phase 2 2026-06-30）。

    镜像父此刻 AgentRecallSource 的两路召回：task 层 body（父自身 + 同 agent 兄弟，按 agent_id 跨 task）
    + agent 层对话回合（Phase 1 写的 start_task 框 / 跨 agent bubble / 同 agent finish 对）。二者按
    (timestamp, seq_no) 归并后写入 child scope，作 child 的起始记忆；之后两边各自演进。
    同 agent 兄弟 body 因此带框（不再裸泄漏），跨 agent 兄弟以 bubble 呈现。
    """
    # AGENT_COMPACT_SUMMARY（父的黑盒折叠派发日志）仍排除——对子无用（沿用 2026-06-23 的窄化意图，
    # 只是现在改为 mirror 而非「仅 OPEN-task body」）。
```

- [ ] **Step 6: Run the new test to verify it passes**

Run: `cd ctx-weft && uv run pytest tests/unit/test_inherit_mirror.py -v`
Expected: PASS (Amy bubble inherited; Lily body framed; all tagged `inherited_from_task_id`).

- [ ] **Step 7: Find and update any existing `_copy_memory_for_inherit` tests, then run the affected suites**

Search for existing tests exercising the inherit copy (the change adds agent-layer turns to the copied output, so an existing test that asserts an exact copied-record set will need updating):

Run: `cd ctx-weft && uv run pytest tests/unit/ -k "inherit or copy_memory or open_closed or resume" -v`

If a test fails because it now sees additional (legitimately mirrored) agent-layer turns, update its assertions to expect the framed/bubble turns (do NOT weaken an assertion to vacuous). If a failure indicates a real regression (e.g. duplicated content, wrong order), stop and report it. Name every test you changed and why in your report.

- [ ] **Step 8: Run the broader memory/dispatch regression**

Run: `cd ctx-weft && uv run pytest tests/unit/test_capsule_golden.py tests/unit/test_subtask_nesting.py tests/unit/test_close_task.py tests/unit/test_compaction.py tests/unit/test_open_closed_recall.py tests/unit/test_inherit_mirror.py -q`
Expected: PASS. Investigate any failure before committing.

- [ ] **Step 9: Commit**

```bash
git add src/ctx_weft/core/runtime.py tests/unit/test_inherit_mirror.py
git commit -m "feat(inherit): mirror parent's recall view (frames+bubbles) into sub-agent scope

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

(If Step 7 required edits to existing test files, add those paths to the `git add` as well and mention them in the commit body.)

---

## Self-Review

**Spec coverage:**
- Copy parent's agent-layer turns in addition to task-body → Task 1 Step 4. ✓
- Same-agent siblings framed (no naked leak) → asserted in Task 1 Step 1 test. ✓
- Cross-agent siblings inherited as bubbles → asserted in Task 1 Step 1 test. ✓
- Merge by `(timestamp, seq_no)`; preserve role/content/timestamp/tool pairing; tag `inherited_from_task_id` → Task 1 Step 4 + Global Constraints. ✓
- `AGENT_COMPACT_SUMMARY` excluded; `origin_task_id` not re-keyed → Global Constraints + Step 4/5. ✓
- Blackboard removal explicitly deferred to Phase 3 with the open decisions named → Out of Scope. ✓

**Placeholder scan:** No TODO/TBD/"handle edge cases" placeholders; Step 4/5 carry full code; Step 7 names the concrete search command and the decision rule (update vs. report) rather than a vague "fix tests".

**Type consistency:** `body_records`/`frame_records`/`combined` are `MemoryRecord` lists; `_copy_memory_for_inherit` signature unchanged `(parent_task, child_task, sub_agent, memory, session_id, tenant_id)`; `child_scope` construction matches the pre-existing one; `r.metadata["seq_no"]` is exposed by both providers (verified Phase 1: in_memory + postgres).

---

## Execution Handoff

Phase 2 plan complete. Phase 3 (blackboard predecessor removal) requires resolving the two open decisions in "Out of Scope" first — recommend a short brainstorm before planning it.
