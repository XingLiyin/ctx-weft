# Blackboard: Unsubscribe Predecessors + Move Subtask Review to the Observe Cue — Phase 3

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Stop feeding the actor/observer prompts from blackboard. Predecessor results come only via memory recall (Phase 2 already surfaces them in the conversation); the observer's confirm/reopen affordance for its own sub-tasks moves into the **observe cue prompt**, generated from `task_manager`, instead of from blackboard `ContextBlock`s.

**Architecture:** After Phase 2, both predecessor sibling results *and* own-children results are already present in the assembled conversation (the parent agent scope holds their `start_task` frames + bubbles/finish pairs). Blackboard recall is therefore redundant as a *data* source. What the conversation lacks is LLM-visible *actionable metadata* — the `task_id`/title/outcome handles the observer needs to reference a child in `task_reviews`. Phase 3 (a) adds that metadata to the observe cue from `task_manager`, then (b) stops creating predecessor and subtask blackboard subscriptions and drops the composer's blackboard block rendering. The blackboard *mechanism* itself (`subscribe_topic`, `recall_topic`, `BlackboardSource`, `BLACKBOARD_PUBLISH`) and the `tracking_task_ids` field are kept intact — only the subscription wiring and prompt rendering are removed.

**Tech Stack:** Python 3.11, `ctx_weft` core, pytest (`uv run pytest` from `ctx-weft/`).

## Global Constraints

- Tests run with `uv run pytest` from `ctx-weft/` (pyproject sets `pythonpath=["."]`).
- **KEEP** the `tracking_task_ids` field on `Task` and the blackboard *mechanism* — `MemoryProvider.subscribe_topic`/`recall_topic`, `BlackboardSource`, and `BLACKBOARD_PUBLISH` writes. Phase 3 removes only the *subscription creation* in the loop driver and the *prompt rendering* of blackboard blocks. Do not delete the mechanism code.
- The observer's subtask review affordance is generated from `task_manager` (the parent task's children → `task_id`, `title`, outcome) and surfaced in the **observe cue** (a trailing-user-message section), NOT from blackboard `ContextBlock`s. The result *content* is read by the observer from the conversation (Phase 2), so the cue carries only the actionable handle list, not the full results.
- Predecessor results are surfaced only through memory recall (no `## Task Background` blackboard section, no observer "Upstream task results" blackboard section).
- Depends on Phase 1 + Phase 2 (commits through `e4fcafa`) being present.

## Out of Scope

- `_flush_tracking_memory` (runtime.py) writes `OBSERVER_SUMMARY` records that no assembler source recalls; with predecessors now flowing via memory recall it is dead, but removing it is a separate cleanup — NOT in this plan. (A verification step below confirms the dead-ness for the record.)
- Phase 4+ (further blackboard mechanism teardown) — not planned; the mechanism stays.

---

## File Structure

- `src/ctx_weft/core/loop/steps/observe.py` — ObserveStep builds a `subtask_reviews` list from `ctx.task_manager` and passes it via `ContextRequest.extra`.
- `src/ctx_weft/core/assembler/composer.py` — `_build_observer_messages` renders the subtask-review clause from `request.extra["subtask_reviews"]` (Task 1); later drops the blackboard subtask/predecessor block rendering (Task 2).
- `src/ctx_weft/core/loop/driver.py` — `_ensure_blackboard_subscriptions` stops creating predecessor + subtask subscriptions (Task 2).
- Tests: `tests/unit/test_observer_subtask_cue.py` (new, Task 1); existing blackboard-rendering tests updated (Task 2).

---

### Task 1: Surface reviewable sub-tasks in the observe cue (additive)

**Files:**
- Modify: `src/ctx_weft/core/loop/steps/observe.py:293-302` (add `extra` to the `ContextRequest`)
- Modify: `src/ctx_weft/core/assembler/composer.py` `_build_observer_messages` (≈726-771)
- Test: `tests/unit/test_observer_subtask_cue.py` (new)

**Interfaces:**
- Consumes: `ctx.task_manager` (`LoopContext`, available in ObserveStep — used elsewhere in finalize/predispatch); `task_manager.children_of(task_id) -> set[str]`; `task_manager.get_task(id) -> Task | None` with `.id`, `.title`, `.status`.
- Produces: `ContextRequest.extra["subtask_reviews"]: list[dict]`, each `{"task_id": str, "title": str, "outcome": str}` (outcome = `task.status.lower()`). `_build_observer_messages` reads it and appends a cue section listing those handles, instructing review via `task_reviews`.

- [ ] **Step 1: Write the failing test**

Create `tests/unit/test_observer_subtask_cue.py`:

```python
"""Observer cue lists reviewable sub-tasks from task_manager (spec Phase 3 2026-06-30),
independent of any blackboard subscription."""
from ctx_weft.core.assembler.composer import DefaultComposer
from ctx_weft.core.assembler.assembler import ContextRequest, ContextBlock
from ctx_weft.core.state.models import Task, NormalTaskSettings
from ctx_weft.protocols import MemoryScope


def _req(extra):
    task = Task(id="p1", session_id="s1", status="RUNNING", tenant_id="default",
                title="parent", settings=NormalTaskSettings())
    agent = type("A", (), {"id": "ag1"})()
    session = type("S", (), {"id": "s1"})()
    return ContextRequest(
        purpose="observe", scope=MemoryScope(session_id="s1", task_id="p1", agent_id="ag1"),
        task=task, agent=agent, session=session, template=None, bound_capabilities=[],
        extra=extra, actor_transcript=[],
    )


def test_observer_cue_lists_subtask_reviews_from_extra():
    composer = DefaultComposer()
    # minimal history block so there is a user turn to anchor the trailing cue
    blocks = [ContextBlock(id="b1", source="t", kind="history", target="messages",
                           content="do the work", priority=3, token_estimate=3,
                           metadata={"role": "user", "type": "user_prompt", "timestamp": "2026-06-30T00:00:00+00:00"})]
    req = _req({"subtask_reviews": [
        {"task_id": "tsk_amy", "title": "向 Amy 问好", "outcome": "finished"},
        {"task_id": "tsk_lily", "title": "向 Lily 问好", "outcome": "failed"},
    ]})
    msgs = composer._build_observer_messages(blocks, req)
    cue = msgs[-1].content
    assert "tsk_amy" in cue and "tsk_lily" in cue, f"cue must list child task_ids; got: {cue}"
    assert "向 Amy 问好" in cue and "failed" in cue
    assert "task_reviews" in cue, "cue must tell the observer to review via task_reviews"


def test_observer_cue_no_subtask_section_when_no_children():
    composer = DefaultComposer()
    blocks = [ContextBlock(id="b1", source="t", kind="history", target="messages",
                           content="do the work", priority=3, token_estimate=3,
                           metadata={"role": "user", "type": "user_prompt", "timestamp": "2026-06-30T00:00:00+00:00"})]
    req = _req({"subtask_reviews": []})
    msgs = composer._build_observer_messages(blocks, req)
    assert "tsk_" not in msgs[-1].content
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd ctx-weft && uv run pytest tests/unit/test_observer_subtask_cue.py -v`
Expected: FAIL — `_build_observer_messages` does not yet read `extra["subtask_reviews"]`, so the task_ids are absent from the cue.

- [ ] **Step 3: Render the subtask-review clause in `_build_observer_messages`**

In `composer.py`, inside `_build_observer_messages`, locate the `extra_sections` assembly (the block that builds `subtask_blocks`/`pred_blocks` sections). Add, after the existing `extra_sections` are built (do NOT remove the existing blackboard sections in this task — additive):

```python
        # Phase 3: reviewable sub-tasks come from task_manager via request.extra (not blackboard).
        # The observer reads each child's RESULT from the conversation (Phase 2); this clause only
        # surfaces the actionable handles (task_id/title/outcome) so it can confirm/reopen via task_reviews.
        reviews = (getattr(request, "extra", {}) or {}).get("subtask_reviews") or []
        if reviews:
            lines = ["## Your sub-tasks (confirm / reopen via `task_reviews`, referencing the task_id):"]
            for r in reviews:
                lines.append(f"- {r['task_id']} — {r.get('title', '')} [{r.get('outcome', '')}]")
            extra_sections.append("\n".join(lines))
```

- [ ] **Step 4: Populate `subtask_reviews` in ObserveStep**

In `observe.py`, replace the `ContextRequest(...)` construction at lines 293-302 with one that computes and passes `subtask_reviews`:

```python
        subtask_reviews: list[dict] = []
        tm = ctx.task_manager
        if tm is not None:
            for cid in tm.children_of(state.task.id):
                child = tm.get_task(cid)
                if child is None:
                    continue
                subtask_reviews.append({
                    "task_id": child.id,
                    "title": child.title or "",
                    "outcome": (child.status or "").lower(),
                })
        request = ContextRequest(
            purpose="observe",
            scope=state.scope,
            task=state.task,
            agent=agent,
            session=state.session,
            template=state.extra.get("template"),
            bound_capabilities=bound_caps,
            actor_transcript=state.transcript,
            extra={"subtask_reviews": subtask_reviews},
        )
```

- [ ] **Step 5: Run the new test + observer suite**

Run: `cd ctx-weft && uv run pytest tests/unit/test_observer_subtask_cue.py -v`
Expected: PASS.
Run: `cd ctx-weft && uv run pytest tests/unit/ -k "observ or composer" -q`
Expected: PASS (existing observer/composer tests unaffected — this task is additive).

- [ ] **Step 6: Commit**

```bash
git add src/ctx_weft/core/loop/steps/observe.py src/ctx_weft/core/assembler/composer.py tests/unit/test_observer_subtask_cue.py
git commit -m "feat(observe): list reviewable sub-tasks in the observe cue from task_manager

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 2: Stop predecessor + subtask subscriptions; drop blackboard prompt rendering

**Files:**
- Modify: `src/ctx_weft/core/loop/driver.py:202-229` (`_ensure_blackboard_subscriptions` + its call site)
- Modify: `src/ctx_weft/core/assembler/composer.py` `_build_observer_messages` (remove blackboard `subtask_blocks`/`pred_blocks` sections) and `_build_actor_messages` (the `bb_blocks` → `## Task Background` rendering)
- Test: update/remove existing tests asserting blackboard predecessor/subtask rendering; add a driver test that no subscriptions are created.

**Interfaces:**
- Consumes: nothing new.
- Produces: `_ensure_blackboard_subscriptions` no longer creates any subscription (predecessor or subtask); `BlackboardSource` therefore yields no blocks, so `_build_actor_messages` produces no `## Task Background` and `_build_observer_messages` produces no blackboard-sourced sections. `tracking_task_ids`, `subscribe_topic`, `recall_topic`, `BlackboardSource`, and `BLACKBOARD_PUBLISH` remain defined and callable.

- [ ] **Step 1: Write the failing test (no subscriptions created)**

Add to a new or existing driver test file, e.g. `tests/unit/test_no_blackboard_subscriptions.py`:

```python
"""Phase 3: the loop driver no longer creates predecessor/subtask blackboard subscriptions."""
import pytest
from ctx_weft.core.loop.driver import LoopDriver  # adjust to the actual class holding _ensure_blackboard_subscriptions


@pytest.mark.asyncio
async def test_no_blackboard_subscriptions_created(monkeypatch):
    calls = []

    class FakeMem:
        async def subscribe_topic(self, **kw):
            calls.append(kw)

    class FakeTM:
        def children_of(self, tid):
            return {"c1", "c2"}

    # Construct the minimal state/ctx the method needs (mirror how other driver unit tests build them).
    # Drive _ensure_blackboard_subscriptions and assert subscribe_topic was never called.
    # ... (implementer: build state with task.tracking_task_ids={"p0"}, ctx.task_manager=FakeTM(), ctx.memory=FakeMem())
    driver = LoopDriver()  # adjust constructor to match existing driver tests
    # await driver._ensure_blackboard_subscriptions(state, ctx)
    assert calls == [], "no blackboard subscriptions should be created in Phase 3"
```

> **Implementer note:** match the construction of `state`/`ctx` to how existing `driver` unit tests build them (search `tests/unit` for `_ensure_blackboard_subscriptions` or `LoopDriver`/`Driver` usage). If `_ensure_blackboard_subscriptions` is removed entirely (call site deleted), replace this test with one asserting the call site is gone is not feasible — instead keep the method as a no-op that returns immediately and assert it creates no subscriptions (below). Prefer keeping the method as an explicit no-op with a docstring over deleting it, so the "mechanism preserved, wiring removed" intent is legible.

- [ ] **Step 2: Run test to verify it fails**

Run: `cd ctx-weft && uv run pytest tests/unit/test_no_blackboard_subscriptions.py -v`
Expected: FAIL — current `_ensure_blackboard_subscriptions` calls `subscribe_topic` for `subtask` children (and predecessors).

- [ ] **Step 3: Make `_ensure_blackboard_subscriptions` a no-op**

In `driver.py`, replace the body of `_ensure_blackboard_subscriptions` (lines 202-229) with:

```python
    async def _ensure_blackboard_subscriptions(self, state: LoopState, ctx: LoopContext) -> None:
        """No-op since Phase 3 (2026-06-30).

        Predecessor results now reach a task via memory recall (Phase 2 inherit/recall), and the
        observer's own-children review affordance is surfaced in the observe cue from task_manager
        (see ObserveStep). The blackboard mechanism (subscribe_topic/recall_topic/BlackboardSource/
        BLACKBOARD_PUBLISH) and `tracking_task_ids` are intentionally kept; only the subscription
        wiring is removed.
        """
        return
```

- [ ] **Step 4: Remove the blackboard sections from the composer prompts**

In `composer.py` `_build_observer_messages`, remove the `bb_blocks`/`subtask_blocks`/`pred_blocks` computation and the two `extra_sections.append(...)` for "Your sub-task results (...)" and "Upstream task results (...)". Keep the Phase-1/2 `pre_cue_sections` (final output) and the Task-1 `subtask_reviews` clause. The `extra_sections` list now contains only the Task-1 subtask-review clause (when present).

In `composer.py` `_build_actor_messages`, remove the `bb_blocks` computation (the `[b for b in blocks if b.kind == "blackboard" and ... != "subtask"]`) and the `## Task Background` section it produced. (With no subscriptions there are no blackboard blocks; this removes the now-dead rendering rather than leaving an always-empty branch.)

- [ ] **Step 5: Update existing tests that asserted blackboard rendering**

Run: `cd ctx-weft && uv run pytest tests/unit/ -k "blackboard or task_background or predecessor or subtask" -v`

For each failure: if a test asserted a `## Task Background` predecessor section or an observer blackboard subtask/upstream section, update it to the new reality (predecessor → not in prompt as a blackboard section; subtask review → the cue clause from `extra["subtask_reviews"]`). Do NOT weaken an assertion to vacuous. Name every test changed and why in your report. If a test exercises the blackboard *mechanism* itself (`subscribe_topic`/`recall_topic`/`BlackboardSource`/publish), it must still pass unchanged — if one breaks, you removed mechanism you should have kept; stop and report.

- [ ] **Step 6: Verify `_flush_tracking_memory` OBSERVER_SUMMARY is unconsumed (record only, no change)**

Run: `cd ctx-weft && uv run pytest tests/unit/ -q` (full unit suite) and, for the record, confirm no assembler source recalls `OBSERVER_SUMMARY`:
Search the assembler sources for `OBSERVER_SUMMARY` (Grep `OBSERVER_SUMMARY` under `src/ctx_weft/core/assembler/`). Note the finding in your report (expected: no matches → it is dead, deferred to a separate cleanup). Make NO code change for this step.

- [ ] **Step 7: Run the broad regression**

Run: `cd ctx-weft && uv run pytest tests/unit/test_capsule_golden.py tests/unit/test_subtask_nesting.py tests/unit/test_inherit_mirror.py tests/unit/test_observer_subtask_cue.py tests/unit/test_no_blackboard_subscriptions.py tests/unit/test_close_task.py -q`
Expected: PASS. Investigate any failure before committing.

- [ ] **Step 8: Commit**

```bash
git add src/ctx_weft/core/loop/driver.py src/ctx_weft/core/assembler/composer.py tests/unit/
git commit -m "feat(blackboard): stop predecessor+subtask subscriptions, drop blackboard prompt rendering

Predecessors now surface via memory recall (Phase 2); subtask review via the observe cue.
Mechanism (subscribe_topic/recall_topic/BlackboardSource/publish) and tracking_task_ids kept.

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Self-Review

**Spec coverage:**
- Keep `tracking_task_ids` + blackboard mechanism → Global Constraints; Task 2 Step 3 keeps the APIs, only no-ops the wiring. ✓
- Stop subscribing predecessors (memory-only) → Task 2 Step 3. ✓
- Subtask review in the observe cue from task_manager, not blackboard → Task 1. ✓
- Drop blackboard prompt rendering (actor Task Background + observer sections) → Task 2 Step 4. ✓
- `_flush_tracking_memory`/OBSERVER_SUMMARY deferred, verified dead → Out of Scope + Task 2 Step 6. ✓

**Placeholder scan:** Task 2 Step 1 carries an implementer note (matching existing driver-test construction) rather than fabricated `state`/`ctx` scaffolding whose exact shape I cannot guarantee — the note names the concrete search to ground it. All code-change steps (Task 1 Steps 3-4, Task 2 Steps 3-4) carry full code.

**Type consistency:** `subtask_reviews` is `list[dict]` with keys `task_id`/`title`/`outcome` produced in observe.py Step 4 and consumed in composer Step 3 with the same keys; `ContextRequest.extra` is the existing channel (already used for `observe_boundary`).

---

## Execution Handoff

Phase 3 plan complete. After this lands, the session's three-phase arc (per-task frames → inherit mirror → blackboard unwire) is done; consider superpowers:finishing-a-development-branch only when the whole `feat/interaction-preserving-capsule` feature (beyond these three phases) is ready to integrate toward `sync/upstream-agent-memory-compaction`.
