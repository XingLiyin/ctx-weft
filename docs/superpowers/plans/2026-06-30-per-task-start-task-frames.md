# Per-Task `start_task` Dispatch Frames Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Stop `delegate_plan` / `replan` sub-task results from collapsing onto one shared `tool_call_id`; give each child its own per-task dispatch frame (narrated as `start_task`) so each child pairs with its own result in the delegating agent's conversation.

**Architecture:** Each plan child gets a unique synthetic `origin_tool_call_id` at dispatch time. At each child's `finalize`, ensure a matching `start_task` assistant frame exists in the parent scope (minted + back-dated to the child's `created_at` when absent), then write the child's result (cross-agent bubble / same-agent ACK) paired to that frame's `tool_call_id`. `origin_task_id` stays the delegating (plan) task — the whole plan remains **one** fold unit (decision: 留父), so the cross-agent isolation contracts are preserved unchanged. The `delegate_plan` call itself returns a "plan submitted, starting tasks" envelope acknowledgement.

**Tech Stack:** Python 3.11, `ctx_weft` core, `InMemoryMemoryProvider`, pytest (`uv run pytest`).

## Global Constraints

- Tests run with `uv run pytest` from `ctx-weft/` (pyproject sets `pythonpath=["."]`).
- `origin_task_id` for every plan dispatch frame / bubble / ACK stays `task.parent_task_id` (the delegating plan task). Do **not** re-key to `child.id` — that is explicitly out of scope (留父).
- Synthetic tool-call ids use `generate_id("tcall")` (already the convention in `finalize._synthesize_dispatch_pair`).
- `start_task` is a **narrative-only** name (`qualify("control:start_task")`); it is never a registered/invokable capability — it appears only inside reconstructed-history `tool_calls`.
- This plan is **Phase 1**. Phase 2 (`_copy_memory_for_inherit` "mirror parent's view" so inheriting sub-agents see framed same-agent capsules + cross-agent bubbles, then delete the blackboard predecessor recall) is a **separate plan** that depends on this one landing first (the dry-run proved per-task frames must exist before the inherit copy can carry a legal conversation).

---

## File Structure

- `src/ctx_weft/core/orchestrator/control_capability.py` — `delegate_plan` / `replan` assign per-child synthetic `origin_tool_call_id`; `delegate_plan` returns the envelope ack text.
- `src/ctx_weft/core/loop/steps/finalize.py` — new `START_TASK_NAME` constant + `_ensure_dispatch_frame` helper; `_close_one` cross-agent & same-agent branches use it for frame-minting + adjacency.
- `src/ctx_weft/core/loop/capability_gateway.py` — for plan-type dispatch, write the envelope's paired ack tool turn so the `delegate_plan` frame is not dangling.
- `tests/unit/test_delegation.py` — flip the "shared id" assertion to "distinct per-child ids".
- `tests/unit/test_subtask_nesting.py` — add: plan child (synthetic id, no pre-seeded frame) gets a minted `start_task` frame paired to its bubble/ACK.

---

### Task 1: Per-child synthetic `origin_tool_call_id` for `delegate_plan` / `replan`

**Files:**
- Modify: `src/ctx_weft/core/orchestrator/control_capability.py:252` (delegate_plan), `:519` (replan)
- Test: `tests/unit/test_delegation.py:67-70`

**Interfaces:**
- Consumes: `generate_id` (already imported at `control_capability.py:18`).
- Produces: each staged plan/replan child has a `origin_tool_call_id` unique per child (value `tcall_…`), never equal to the delegating call's `ctx.tool_call_id`. `delegate_task` (single) is unchanged — it keeps `origin_tool_call_id = ctx.tool_call_id`.

- [ ] **Step 1: Update the failing test (replace the shared-id assertion)**

In `test_delegation.py`, replace `test_delegate_plan_records_origin_on_all_children` (lines 67-70) with:

```python
def test_delegate_plan_records_distinct_origin_per_child() -> None:
    tm = _FakeTM()
    delegate_plan(tasks=[{"title": "a"}, {"title": "b"}], ctx=_ctx(tm, "tc_99"))
    ids = [c.origin_tool_call_id for c in tm.staged]
    assert len(set(ids)) == 2, f"each plan child needs its own origin_tool_call_id, got {ids}"
    assert "tc_99" not in ids, "plan children must NOT reuse the delegate_plan call id"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd ctx-weft && uv run pytest tests/unit/test_delegation.py::test_delegate_plan_records_distinct_origin_per_child -v`
Expected: FAIL — both ids are currently `"tc_99"` (so `len(set(ids)) == 1` and `"tc_99" in ids`).

- [ ] **Step 3: Implement per-child synthetic id in `delegate_plan`**

In `control_capability.py`, inside the `delegate_plan` `for spec in tasks:` loop, change the child construction at line 252 from:

```python
            origin_tool_call_id=ctx.tool_call_id or None,
```

to:

```python
            origin_tool_call_id=generate_id("tcall"),
```

- [ ] **Step 4: Implement the same change in `replan`**

In `control_capability.py`, the `replan` child-construction loop has the identical line at `:519`. Change it the same way:

```python
            origin_tool_call_id=generate_id("tcall"),
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `cd ctx-weft && uv run pytest tests/unit/test_delegation.py -v`
Expected: PASS (new test passes; `test_delegate_task_records_origin_tool_call_id` and `test_tool_call_id_threads_through_provider_extra` still pass — `delegate_task` unchanged).

- [ ] **Step 6: Commit**

```bash
git add src/ctx_weft/core/orchestrator/control_capability.py tests/unit/test_delegation.py
git commit -m "feat(dispatch): plan/replan children get per-child synthetic origin_tool_call_id

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 2: Mint per-child `start_task` frame at finalize, paired + back-dated

**Files:**
- Modify: `src/ctx_weft/core/loop/steps/finalize.py` (add constant + helper; rewire `_close_one` cross-agent `:141-152` and same-agent `:160-185`)
- Test: `tests/unit/test_subtask_nesting.py`

**Interfaces:**
- Consumes: child `Task` with `origin_tool_call_id` (synthetic for plan children, or the gateway-seeded call id for `delegate_task`), `parent_task_id`, `title`, `created_at`.
- Produces: `START_TASK_NAME = qualify("control:start_task")`; `async _ensure_dispatch_frame(memory, parent_scope, task, ctx) -> datetime` — returns the timestamp of the (found-or-minted) assistant frame whose `tool_calls[].id == task.origin_tool_call_id`. cross-agent bubble and same-agent ACK are written with `timestamp == that frame timestamp` so frame↔result stay adjacent. `origin_task_id` on the frame/bubble/ACK stays `task.parent_task_id`.

- [ ] **Step 1: Write the failing test (plan child with no pre-seeded frame gets one minted)**

Add to `test_subtask_nesting.py` (mirrors the existing same-agent test at line ~205, but seeds **no** delegate frame — simulating a plan child whose frame must be minted):

```python
async def test_plan_child_mints_start_task_frame_when_absent() -> None:
    """delegate_plan 子: parent scope 无预置框 → finalize 补铸 start_task 框，bubble/ACK 配对其 id。"""
    from ctx_weft.core.loop.steps.finalize import _close_one, START_TASK_NAME
    from datetime import timedelta

    mem = InMemoryMemoryProvider()
    parent_scope = _sc("p1", "ag1")
    child_scope = _sc("c1", "ag1")
    created = _BASE + timedelta(seconds=0)

    # NOTE: deliberately seed NO delegate frame in parent scope.
    child = Task(id="c1", session_id="s1", status="FINISHED", tenant_id="default",
                 assigned_agent_id="ag1", creator_agent_id="ag1", parent_task_id="p1",
                 origin_tool_call_id="tcall_plan_c1", title="向 Lily 问好",
                 user_prompt="hi lily", created_at=created, settings=NormalTaskSettings())
    state = _state(child, child_scope, LoopConfig())

    await _close_one(mem, state, child, "out\n\nProcess Report: r", "success", _loop_ctx(mem),
                     short=True, act_recap="本段做了 X", task_summary="整段总结")

    turns = await mem.recall_recent(parent_scope, [T.AGENT_CONVERSATION_TURN], 100, _ctx())

    # a minted start_task frame exists, carrying the child's origin_tool_call_id
    frame = [r for r in turns if r.role == "assistant"
             and any(tc.get("id") == "tcall_plan_c1" and tc.get("name") == START_TASK_NAME
                     for tc in (r.metadata.get("tool_calls") or []))]
    assert frame, "a start_task frame must be minted for the plan child"
    assert frame[0].metadata.get("origin_task_id") == "p1", "frame stays in delegating(p1) unit (留父)"
    assert frame[0].timestamp == created, "frame back-dated to child.created_at for adjacency"

    # the paired ACK result shares the frame's tool_call_id and timestamp
    ack = [r for r in turns if r.role == "tool" and r.metadata.get("tool_call_id") == "tcall_plan_c1"]
    assert ack, "paired result must carry the same tool_call_id"
    assert ack[0].timestamp == frame[0].timestamp, "result adjacent to its frame"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd ctx-weft && uv run pytest tests/unit/test_subtask_nesting.py::test_plan_child_mints_start_task_frame_when_absent -v`
Expected: FAIL — `ImportError: cannot import name 'START_TASK_NAME'` (constant/helper not yet defined).

- [ ] **Step 3: Add the constant and helper to `finalize.py`**

After `_DISPATCH_ACK` (line 38) add:

```python
# 派发框的叙事工具名（仅出现在重建历史的 tool_calls 里，非可调用能力）。
START_TASK_NAME = qualify("control:start_task")


async def _ensure_dispatch_frame(memory, parent_scope, task, ctx):
    """确保 parent scope 有一条 tool_call id==task.origin_tool_call_id 的 assistant 派发框，返回其时间戳。

    delegate_task(单): gateway 执行前已写好框 → 找到即返回其 timestamp。
    delegate_plan/replan 子: gateway 只写了 plan 框、没有 per-child 框 → 此处补铸一条 start_task 框，
    时间戳回拨到 task.created_at，使其排在子 body 之前、与配对结果相邻。origin_task_id 留父(留 plan task)。
    """
    existing = await memory.recall_recent(
        parent_scope, [MemoryEventType.AGENT_CONVERSATION_TURN], 2000, ctx.provider_ctx)
    frame = next(
        (r for r in existing
         if r.role == "assistant"
         and any(tc.get("id") == task.origin_tool_call_id
                 for tc in (r.metadata.get("tool_calls") or []))),
        None,
    )
    if frame is not None:
        return frame.timestamp
    ts = task.created_at or now_utc()
    await memory.ingest(
        MemoryEvent(
            type=MemoryEventType.AGENT_CONVERSATION_TURN, scope=parent_scope,
            content="", timestamp=ts, role="assistant",
            metadata={"origin_task_id": task.parent_task_id,
                      "parent_task_id": task.parent_task_id,
                      "tool_calls": [{"id": task.origin_tool_call_id,
                                      "name": START_TASK_NAME,
                                      "input": {"title": task.title}}]},
        ),
        ctx.provider_ctx,
    )
    return ts
```

- [ ] **Step 4: Rewire the cross-agent branch to use the frame timestamp**

In `_close_one`, the cross-agent branch (lines 140-154) currently writes the bubble with `timestamp=now_utc()`. Replace the bubble ingest so it back-dates to the frame:

```python
        if cross_agent:
            # 跨 agent（spec 2026-06-28 §2.3）：dispatch result 写成 agent 层普通 conversation turn
            # （tool 回合），与 start_task / delegate 框靠 tool_call_id 配对、时间戳对齐保证相邻。
            report_prefix = "[outcome=fail] " if outcome == "fail" else ""
            frame_ts = await _ensure_dispatch_frame(memory, parent_scope, task, ctx)
            await memory.ingest(
                MemoryEvent(
                    type=MemoryEventType.AGENT_CONVERSATION_TURN,
                    scope=parent_scope,
                    content=f"{report_prefix}{mem_content}",
                    timestamp=frame_ts,
                    role="tool",
                    metadata={"origin_task_id": task.parent_task_id,
                              "tool_call_id": task.origin_tool_call_id},
                ),
                ctx.provider_ctx,
            )
            events.append(make_event(
                state, EventType.MEMORY_INGESTED,
                payload={"memory_event_type": MemoryEventType.AGENT_CONVERSATION_TURN.value,
                         "source": "dispatch_result", "content_length": len(mem_content)},
            ))
```

- [ ] **Step 5: Rewire the same-agent branch to use the helper**

In `_close_one`, replace the same-agent branch's manual delegate-turn lookup (lines 160-182, from `elif same_agent:` through the static-ACK ingest, **keeping** the `_synthesize_dispatch_pair` call that follows at line 184) with:

```python
        elif same_agent:
            # 同 agent（spec 2026-06-30 §2.5）：确保/补铸派发框，写一条配对静态 tool result，
            # 时间戳对齐框 → 严格相邻、排在子 body 之前。子真实产出由内联胶囊 body + 嵌套 finish 对承载。
            frame_ts = await _ensure_dispatch_frame(memory, parent_scope, task, ctx)
            await memory.ingest(
                MemoryEvent(
                    type=MemoryEventType.AGENT_CONVERSATION_TURN, scope=parent_scope,
                    content=_DISPATCH_ACK, timestamp=frame_ts, role="tool",
                    metadata={"origin_task_id": task.parent_task_id,
                              "tool_call_id": task.origin_tool_call_id},
                ),
                ctx.provider_ctx,
            )
            # 嵌套合成子自己的 finish 对（写进共享 agent scope，@close 时刻）
            await _synthesize_dispatch_pair(
                memory, parent_scope, task, act_recap, task_summary, outcome, ctx.provider_ctx)
```

- [ ] **Step 6: Run the new test + the existing nesting/golden suites**

Run: `cd ctx-weft && uv run pytest tests/unit/test_subtask_nesting.py tests/unit/test_capsule_golden.py -v`
Expected: PASS. The existing same-agent test (`test_subtask_nesting.py:~205`) seeds a delegate frame with id `oc1`, so `_ensure_dispatch_frame` **finds** it (no mint) and reuses its timestamp — behaviour identical to before. The cross-agent golden (`test_capsule_golden.py:625/631`) still passes because `origin_task_id` stays `parent_id` and no `child.id` capsule is added in parent scope (the minted frame is `origin=parent`).

- [ ] **Step 7: Commit**

```bash
git add src/ctx_weft/core/loop/steps/finalize.py tests/unit/test_subtask_nesting.py
git commit -m "feat(finalize): mint per-child start_task frame, pair+back-date dispatch result

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 3: `delegate_plan` / `replan` envelope acknowledgement

**Files:**
- Modify: `src/ctx_weft/core/orchestrator/control_capability.py:234,275` (delegate_plan return text), `:526`-ish (replan return text)
- Modify: `src/ctx_weft/core/loop/capability_gateway.py:254-272` (write paired ack result for plan-type dispatch)
- Test: `tests/unit/test_delegation.py`

**Interfaces:**
- Consumes: `DELEGATE_PLAN_NAME` (`control_capability.py:52`) and the qualified replan name `qualify(f"{PROVIDER_NAME}:replan")`.
- Produces: after a `delegate_plan` / `replan` dispatch, the parent agent scope contains an `AGENT_CONVERSATION_TURN` tool turn paired (`tool_call_id`) to the plan call, content `_PLAN_DISPATCH_ACK`, so the gateway's `delegate_plan` frame is not dangling. The actor-visible `ControlResult.content` is the same ack text.

- [ ] **Step 1: Write the failing test**

Add to `test_delegation.py` (uses the existing `provider.invoke` harness from `test_tool_call_id_threads_through_provider_extra`):

```python
@pytest.mark.asyncio
async def test_delegate_plan_writes_envelope_ack() -> None:
    from ctx_weft.core.loop.capability_gateway import _PLAN_DISPATCH_ACK  # defined in Step 3
    tm = _FakeTM()
    parent = _parent()
    tm.get_task = lambda tid: parent  # type: ignore[assignment]
    session = Session(id="s1", tenant_id="default", user_prompt="x", status="RUNNING")
    provider = ControlCapabilityProvider()
    provider.register_session("s1", tm, session)
    ctx = ProviderContext(
        session_id="s1", tenant_id="default", task_id="p1", agent_id="a1",
        extra={"tool_call_id": "tc_plan"},
    )
    async for _ev in provider.invoke(
        f"{PROVIDER_NAME}:delegate_plan", {"tasks": [{"title": "a"}, {"title": "b"}]}, ctx,
    ):
        pass
    # envelope ack paired to the plan call id
    parent_scope = MemoryScope(session_id="s1", task_id="p1", agent_id="a1")
    turns = await provider  # placeholder — see note
```

> **Implementer note:** the `ControlCapabilityProvider` test harness here does not expose the memory provider directly; assert the **actor-visible** ack instead, which is simpler and sufficient for this task:

```python
@pytest.mark.asyncio
async def test_delegate_plan_returns_envelope_ack() -> None:
    from ctx_weft.core.orchestrator.control_capability import delegate_plan, _PLAN_DISPATCH_ACK
    tm = _FakeTM()
    res = delegate_plan(tasks=[{"title": "a"}, {"title": "b"}], ctx=_ctx(tm, "tc_plan"))
    assert res.content == _PLAN_DISPATCH_ACK
```

Use the second form (`test_delegate_plan_returns_envelope_ack`) as the actual test; delete the first sketch.

- [ ] **Step 2: Run test to verify it fails**

Run: `cd ctx-weft && uv run pytest tests/unit/test_delegation.py::test_delegate_plan_returns_envelope_ack -v`
Expected: FAIL — `_PLAN_DISPATCH_ACK` not defined; current content is `"Plan with 2 task(s) submitted."`.

- [ ] **Step 3: Define the ack and return it from `delegate_plan` / `replan`**

In `control_capability.py`, after the imports / near the other module constants (e.g. after line 52), add:

```python
_PLAN_DISPATCH_ACK = "计划已生成，接下来会通过 start_task 逐个启动各子任务。"
```

In `delegate_plan`, change the early-return guard text at `:234` and the final return at `:275` to use it:

```python
    if ctx is None or ctx.task_manager is None or ctx.task is None:
        return ControlResult(content=_PLAN_DISPATCH_ACK)
```

```python
    ctx.task.status = "SUSPENDED"
    ctx.task.actor_done = True
    return ControlResult(content=_PLAN_DISPATCH_ACK)
```

In `replan`, change its final `ControlResult(content=...)` (the submit-confirmation return) to `ControlResult(content=_PLAN_DISPATCH_ACK)` as well.

- [ ] **Step 4: Write the gateway paired-ack result for plan dispatch**

In `capability_gateway.py`, add a module constant near the other dispatch constants (around line 42):

```python
_PLAN_DISPATCH_ACK = "计划已生成，接下来会通过 start_task 逐个启动各子任务。"
_PLAN_DISPATCH_TOOLS = frozenset({
    qualify("control:delegate_plan"), qualify("control:replan"),
})
```

(Reuse the existing `qualify` import in this module; if absent, add `from ctx_weft.protocols.capability import qualify`.)

In `_record_invocation`, inside the `if is_dispatch:` block, **after** the existing frame ingest (after line 272), append:

```python
            if tool_name in _PLAN_DISPATCH_TOOLS:
                # envelope: 给 plan 框写一条配对的 ack tool result，避免该框悬挂(被 legalize 剥掉)。
                await self._memory.ingest(
                    MemoryEvent(
                        type=MemoryEventType.AGENT_CONVERSATION_TURN,
                        scope=_tool_scope(state),
                        content=_PLAN_DISPATCH_ACK,
                        timestamp=now_utc(),
                        role="tool",
                        metadata={"origin_task_id": state.task.id,
                                  "parent_task_id": state.task.parent_task_id,
                                  "tool_call_id": tool_call_id},
                    ),
                    ctx.provider_ctx,
                )
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `cd ctx-weft && uv run pytest tests/unit/test_delegation.py -v`
Expected: PASS.

- [ ] **Step 6: Run the full dispatch/capsule regression**

Run: `cd ctx-weft && uv run pytest tests/unit/test_delegation.py tests/unit/test_subtask_nesting.py tests/unit/test_capsule_golden.py tests/unit/test_close_task.py tests/unit/test_compaction.py tests/unit/test_open_closed_recall.py -v`
Expected: PASS. Investigate any failure before continuing — these are the suites that pin the dispatch/recall contracts.

- [ ] **Step 7: Commit**

```bash
git add src/ctx_weft/core/orchestrator/control_capability.py src/ctx_weft/core/loop/capability_gateway.py tests/unit/test_delegation.py
git commit -m "feat(dispatch): delegate_plan/replan envelope ack + paired result turn

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Self-Review

**Spec coverage:**
- Per-child synthetic `origin_tool_call_id` → Task 1. ✓
- Per-task `start_task` frame minted + paired + back-dated → Task 2. ✓
- `origin_task_id` stays parent (留父) → enforced in Task 2 helper metadata + Global Constraints; golden contracts unchanged. ✓
- `delegate_plan` result = "plan generated, will start tasks" → Task 3. ✓
- Updated test contracts: shared-id assertion flipped (Task 1), new mint test (Task 2), envelope ack test (Task 3). ✓
- Out of scope (Phase 2): `_copy_memory_for_inherit` mirror + blackboard removal — flagged in Global Constraints. ✓

**Placeholder scan:** Task 3 Step 1 contains a deliberately-discarded sketch with an inline `> note`; the actionable test is the second `test_delegate_plan_returns_envelope_ack`. All other steps carry concrete code/commands.

**Type consistency:** `_ensure_dispatch_frame(memory, parent_scope, task, ctx)` returns a timestamp consumed by both branches in Task 2; `START_TASK_NAME` / `_PLAN_DISPATCH_ACK` / `_PLAN_DISPATCH_TOOLS` referenced consistently across finalize, control_capability, gateway, and tests. `origin_tool_call_id` is set in Task 1 and read in Task 2.

---

## Execution Handoff

Phase 1 plan complete. Phase 2 (inherit mirror + blackboard removal) is a separate plan that depends on this landing.
