# compact & metadata_filler as directly-invoked steps — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Stop wrapping `compact` and `metadata_filler` in `Task`s; invoke `CompactStep` inline inside `ReasonStep` and run `metadata_filler` from a background coroutine, both reusing the actor assembly + a trailing instruction.

**Architecture:** (1) Composer assembles `compact`/`metadata_filler` like `observe` — actor system + actor messages + one trailing instruction user message. (2) `CompactStep` becomes self-contained (computes foldable layers, one summary call, folds each over-budget layer) and is called inline by `ReasonStep`. (3) `metadata_filler` launches as a tracked background coroutine that calls `MetadataFillerStep.execute` directly against the root task. (4) The daemon + compact-dispatch + dedicated-template machinery is removed; recovery is condition re-evaluation.

**Tech Stack:** Python, pytest, asyncio.

**Spec:** `docs/superpowers/specs/2026-06-14-compact-metadata-as-steps-design.md`

**Test runner:** from `loomex-core/`, `python -m pytest` (plain pytest; project does not use uv for tests).

---

## File Structure

- `loomex-core/src/loomex_core/core/assembler/composer.py` — compact/metadata assembly mirrors observe; drop dead compact builders.
- `loomex-core/src/loomex_core/core/loop/steps/compact.py` — `CompactStep` self-contained (layers + one summary + fold); drop `_resolve_system_prompt`.
- `loomex-core/src/loomex_core/core/loop/steps/reason.py` — call `CompactStep` inline; drop `_dispatch_compact` / `_compactable_layers`.
- `loomex-core/src/loomex_core/core/loop/steps/metadata_filler.py` — target = `state.task`; drop `MetadataFillerTaskSettings` lookup.
- `loomex-core/src/loomex_core/core/runtime.py` — `_launch_metadata_filler` background coroutine; launch on start + resume; drop daemon respawn + `_resolve` cases for the two settings.
- `loomex-core/src/loomex_core/core/orchestrator/task_manager.py` — drop `spawn_metadata_filler` / `_run_daemon`; rename `_daemon_asyncio_tasks` → `_background_asyncio_tasks` + `track_background()`; `restore()` skips obsolete compact/metadata tasks.
- Tests under `loomex-core/tests/`.

---

## Task 1: Composer — compact/metadata assembled like observe

**Files:**
- Modify: `loomex-core/src/loomex_core/core/assembler/composer.py`
- Test: `loomex-core/tests/unit/test_composer_compact_metadata.py`

- [ ] **Step 1: Write the failing tests**

Create `loomex-core/tests/unit/test_composer_compact_metadata.py`:

```python
"""compact/metadata_filler assembled like observe: actor system + messages + trailing instruction."""

from __future__ import annotations

from types import SimpleNamespace

from loomex_core.core.assembler.assembler import ContextBlock
from loomex_core.core.assembler.composer import DefaultComposer


def _identity(text):
    return ContextBlock(id="id", source="identity", kind="identity", target="system",
                        content=text, priority=0, token_estimate=1, metadata={})


def _bg(text):
    return ContextBlock(id="bg", source="memory", kind="background", target="system",
                        content=text, priority=1, token_estimate=1, metadata={})


def _tool(name, desc):
    return ContextBlock(id=f"c-{name}", source="capability", kind="capabilities", target="system",
                        content=desc, priority=1, token_estimate=1,
                        metadata={"capability_name": name, "capability_kind": "tool",
                                  "llm_tool": None})


def _req(purpose):
    task = SimpleNamespace(user_prompt_in_memory=False, process_report=None,
                           title="T", description="D", user_prompt="do the thing")
    return SimpleNamespace(purpose=purpose, task=task, template=None)


async def test_compact_reuses_actor_system_and_appends_instruction():
    blocks = [_identity("SOUL"), _bg("BG")]
    prompt = await DefaultComposer().compose(blocks, _req("compact"))
    assert "SOUL" in prompt.system and "## Project Background" in prompt.system
    last_user = [m for m in prompt.messages if m.role == "user"][-1].content
    assert "[Context so far]" in last_user  # compaction instruction present
    assert "do the thing" in last_user      # actor conversation reused
    assert prompt.tools == []


async def test_metadata_reuses_actor_system_and_appends_instruction():
    blocks = [_identity("SOUL"), _bg("BG"), _tool("update_task_metadata", "set title/desc")]
    prompt = await DefaultComposer().compose(blocks, _req("metadata_filler"))
    assert "SOUL" in prompt.system
    last_user = [m for m in prompt.messages if m.role == "user"][-1].content
    assert "update_task_metadata" in last_user  # metadata instruction names the tool
    assert any(t.name == "update_task_metadata" for t in prompt.tools)
```

- [ ] **Step 2: Run to verify they fail**

Run: `cd loomex-core && python -m pytest tests/unit/test_composer_compact_metadata.py -v`
Expected: FAIL (compact currently uses `_build_compact_*`; no trailing instruction).

- [ ] **Step 3: Add the instruction constants**

In `composer.py`, after the `_OBSERVE_JUDGMENT_CUE` constant (around line 70), add:

```python
_COMPACTION_INSTRUCTION = (
    "Now act as a memory compactor. Summarize the conversation above into a concise "
    "[Context so far] section that preserves: key user intents, important facts discovered, "
    "decisions made, tool results, and any unfinished threads. Output only the summary text, "
    "no preamble."
)

_METADATA_INSTRUCTION = (
    "Now set this task's metadata: call `update_task_metadata` exactly once with a concise "
    "title and description (and the session goal if the direction is now clear), then stop. "
    "Call no other tools."
)
```

- [ ] **Step 4: Reroute `compose` and add the shared trailing-messages helper**

In `compose`, replace the routing block:

```python
        if request.purpose in ("act", "metadata_filler"):
            system = self._build_actor_system(blocks)
            messages = self._build_actor_messages(blocks, request)
            tools = self._collect_llm_tools(blocks)
        elif request.purpose == "observe":
            system = self._build_observer_system(blocks, request)
            messages = self._build_observer_messages(blocks, request)
            tools = self._collect_llm_tools(blocks)
        else:  # compact
            system = self._build_compact_system(blocks)
            messages = self._build_compact_messages(blocks, request)
            tools = []
```

with:

```python
        if request.purpose == "act":
            system = self._build_actor_system(blocks)
            messages = self._build_actor_messages(blocks, request)
            tools = self._collect_llm_tools(blocks)
        elif request.purpose == "observe":
            system = self._build_observer_system(blocks, request)
            messages = self._build_observer_messages(blocks, request)
            tools = self._collect_llm_tools(blocks)
        elif request.purpose == "metadata_filler":
            system = self._build_actor_system(blocks)
            messages = self._build_trailing_messages(blocks, request, _METADATA_INSTRUCTION)
            tools = self._collect_llm_tools(blocks)
        else:  # compact
            system = self._build_actor_system(blocks)
            messages = self._build_trailing_messages(blocks, request, _COMPACTION_INSTRUCTION)
            tools = []
```

Add this method right after `_build_actor_messages`:

```python
    def _build_trailing_messages(
        self,
        blocks: list["ContextBlock"],
        request: "ContextRequest",
        instruction: str,
    ) -> list[LLMMessage]:
        """act 会话 + 末尾一条指令 user message（observe 同构）。compact/metadata 用。

        指令并入最后一条 user 回合（_merge_consecutive_messages），避免连续 user。
        """
        messages = self._build_actor_messages(blocks, request)
        messages.append(LLMMessage(role="user", content=instruction))
        return _merge_consecutive_messages(messages)
```

- [ ] **Step 5: Delete the dead compact builders**

Remove the methods `_build_compact_system` and `_build_compact_messages` from `composer.py` entirely (the `## Compact` section). `_format_history` stays (it's still used by tests/other paths — verify with grep before removing anything else; do NOT remove `_format_history`).

- [ ] **Step 6: Run tests to verify pass + full suite**

Run: `cd loomex-core && python -m pytest tests/unit/test_composer_compact_metadata.py -v`
Expected: PASS.
Then: `cd loomex-core && python -m pytest -q` — note any failures referencing compact/metadata assembly; they are addressed in later tasks, but composer-level tests must pass now.

- [ ] **Step 7: Commit**

```
git add loomex-core/src/loomex_core/core/assembler/composer.py loomex-core/tests/unit/test_composer_compact_metadata.py
git commit -m "feat(core/assembler): assemble compact/metadata_filler like observe"
```

---

## Task 2: CompactStep — self-contained (layers + one summary + fold)

**Files:**
- Modify: `loomex-core/src/loomex_core/core/loop/steps/compact.py`
- Test: `loomex-core/tests/unit/test_compact_step_inline.py`

- [ ] **Step 1: Write the failing test**

Create `loomex-core/tests/unit/test_compact_step_inline.py`:

```python
"""CompactStep runs standalone against state.scope: folds over-budget layers with one summary."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from loomex_core.core.loop.steps.compact import CompactStep
from loomex_core.protocols import MemoryEventType as T, MemoryScope


class _FakeMemory:
    def __init__(self, counts):
        self._counts = counts  # {frozenset(types): n}
        self.applied = []  # (layer, summary)

    async def count_recent(self, scope, types, ctx):
        return self._counts.get(frozenset(types), 0)

    async def apply_compact(self, scope, summary, keep_last, ctx, layer):
        self.applied.append((layer.value, summary))
        return SimpleNamespace(events_before=10, events_after=keep_last,
                               summary_event_id="s1")


class _FakeAssembler:
    async def assemble(self, request):
        return SimpleNamespace(system="SYS", messages=[], tools=[])


class _FakeLLM:
    async def complete(self, request, stream=True):
        async def gen():
            yield SimpleNamespace(kind="token", text="SUMMARY", usage=None, tool_call=None)
        return gen()


def _state():
    agent = SimpleNamespace(id="agt1", loop_config=SimpleNamespace(compact_keep_last=2),
                            runtime={"llm_model": "mock"})
    return SimpleNamespace(
        agent=agent,
        session=SimpleNamespace(id="s1"),
        task=SimpleNamespace(id="t1"),
        scope=MemoryScope(session_id="s1", task_id="t1", agent_id="agt1"),
        transcript=[],
        sequence_counter=0,
        extra={"template": None, "bound_capabilities": []},
    )


def _ctx(memory):
    return SimpleNamespace(memory=memory, assembler=_FakeAssembler(), llm=_FakeLLM(),
                           provider_ctx=SimpleNamespace())


async def test_compact_folds_overbudget_layers_with_one_summary():
    # task layer has 5 foldable (> keep_last=2); agent layer has 0
    mem = _FakeMemory({
        frozenset([T.USER_PROMPT, T.LLM_RESPONSE, T.TOOL_RESULT]): 5,
        frozenset([T.TASK_DISPATCH, T.TASK_DISPATCH_RESULT]): 0,
    })
    outcome = await CompactStep().execute(_state(), _ctx(mem))
    assert outcome.next_step is None
    assert [layer for layer, _ in mem.applied] == ["task"]
    assert mem.applied[0][1] == "SUMMARY"


async def test_compact_noop_when_nothing_foldable():
    mem = _FakeMemory({
        frozenset([T.USER_PROMPT, T.LLM_RESPONSE, T.TOOL_RESULT]): 1,
        frozenset([T.TASK_DISPATCH, T.TASK_DISPATCH_RESULT]): 0,
    })
    outcome = await CompactStep().execute(_state(), _ctx(mem))
    assert outcome.next_step is None
    assert mem.applied == []
```

- [ ] **Step 2: Run to verify it fails**

Run: `cd loomex-core && python -m pytest tests/unit/test_compact_step_inline.py -v`
Expected: FAIL (current `CompactStep` requires `CompactTaskSettings`).

- [ ] **Step 3: Rewrite `compact.py`**

Replace the entire body of `loomex-core/src/loomex_core/core/loop/steps/compact.py` with:

```python
"""CompactStep：长对话压缩，由 ReasonStep 内联直调（不再以 task 形式调度）。

  - 作用域 = 当前 state.scope（当前 task + agent）。
  - 计算可折叠层（agent 派发日志 / task 对话），任一层 active 条数 > keep_last 才折。
  - 复用 act 装配内容 + 末尾压缩指令（composer purpose="compact"），一次 summary。
  - 对每个超额层 apply_compact 同一份 summary。
"""

from __future__ import annotations

import logging
from typing import Any

from loomex_core.core.assembler.assembler import ContextRequest
from loomex_core.core.events import EventType
from loomex_core.core.loop.driver import LoopContext, LoopState, Step, StepOutcome, make_event
from loomex_core.protocols import MemoryEventType, MemoryLayer

logger = logging.getLogger(__name__)

# 每层「可折叠」的对话类型；某层 active 条数 > keep_last 才值得 compact（空层守卫）。
_AGENT_COMPACT_TYPES = [MemoryEventType.TASK_DISPATCH, MemoryEventType.TASK_DISPATCH_RESULT]
_TASK_COMPACT_TYPES = [
    MemoryEventType.USER_PROMPT,
    MemoryEventType.LLM_RESPONSE,
    MemoryEventType.TOOL_RESULT,
]


class CompactStep(Step):
    """Standalone compaction over state.scope. Invoked inline by ReasonStep."""

    name = "compact"

    async def execute(self, state: LoopState, ctx: LoopContext) -> StepOutcome:
        agent = state.agent
        keep_last = agent.loop_config.compact_keep_last
        layers = await self._foldable_layers(state, ctx, keep_last)
        if not layers:
            return StepOutcome(next_step=None, events=[])

        events: list[Any] = [make_event(state, EventType.MEMORY_COMPACT_STARTED, payload={
            "task_id": state.task.id,
            "agent_id": agent.id,
            "keep_last": keep_last,
            "layers": list(layers),
        })]

        # 复用 act 装配 + 末尾压缩指令（composer purpose="compact"）。
        request = ContextRequest(
            purpose="compact",
            scope=state.scope,
            task=state.task,
            agent=agent,
            session=state.session,
            template=state.extra.get("template"),
            bound_capabilities=state.extra.get("bound_capabilities", []),
            actor_transcript=state.transcript,
        )
        compact_prompt = await ctx.assembler.assemble(request)

        summary_text = ""
        try:
            from loomex_core.protocols import LLMRequest
            llm_request = LLMRequest(
                model=agent.runtime.get("llm_model", "mock"),
                system=compact_prompt.system,
                messages=compact_prompt.messages,
                tools=[],
            )
            async for chunk in ctx.llm.complete(llm_request, stream=True):
                if chunk.kind == "token":
                    summary_text += chunk.text
        except Exception:
            logger.exception("CompactStep: LLM failed for agent %s, truncation-only", agent.id)

        for layer_name in layers:
            layer = MemoryLayer(layer_name)
            result = await ctx.memory.apply_compact(
                scope=state.scope,
                summary=summary_text or "[Context compacted]",
                keep_last=keep_last,
                ctx=ctx.provider_ctx,
                layer=layer,
            )
            events.append(make_event(state, EventType.MEMORY_COMPACTED, payload={
                "events_before": result.events_before,
                "events_after": result.events_after,
                "summary_event_id": result.summary_event_id,
                "summary_length": len(summary_text),
                "used_llm": bool(summary_text),
                "layer": layer_name,
            }))

        logger.info("CompactStep: agent=%s task=%s folded layers=%s summary_len=%d",
                    agent.id, state.task.id, layers, len(summary_text))
        return StepOutcome(next_step=None, events=events)

    async def _foldable_layers(
        self, state: LoopState, ctx: LoopContext, keep_last: int
    ) -> list[str]:
        """有足够内容可折叠（active 条数 > keep_last）的层。"""
        layers: list[str] = []
        for layer, types in (("agent", _AGENT_COMPACT_TYPES), ("task", _TASK_COMPACT_TYPES)):
            try:
                n = await ctx.memory.count_recent(scope=state.scope, types=types, ctx=ctx.provider_ctx)
            except Exception:
                n = 0
            if n > keep_last:
                layers.append(layer)
        return layers
```

Note: `EventType.MEMORY_COMPACT_STARTED` / `MEMORY_COMPACTED` already exist (used by the old CompactStep). The old `TASK_FINISHED` emission is dropped — there is no task anymore.

- [ ] **Step 4: Run the test to verify pass**

Run: `cd loomex-core && python -m pytest tests/unit/test_compact_step_inline.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```
git add loomex-core/src/loomex_core/core/loop/steps/compact.py loomex-core/tests/unit/test_compact_step_inline.py
git commit -m "feat(core/loop): make CompactStep self-contained for inline use"
```

---

## Task 3: ReasonStep — run compaction inline instead of dispatching a task

**Files:**
- Modify: `loomex-core/src/loomex_core/core/loop/steps/reason.py`
- Test: `loomex-core/tests/unit/test_reason_inline_compact.py`

- [ ] **Step 1: Write the failing test**

Create `loomex-core/tests/unit/test_reason_inline_compact.py`:

```python
"""ReasonStep compacts inline (no compact Task pushed) and still routes to act."""

from __future__ import annotations

from types import SimpleNamespace

from loomex_core.core.loop.steps.reason import ReasonStep


class _SpyCompact:
    def __init__(self):
        self.called = False

    async def execute(self, state, ctx):
        from loomex_core.core.loop.driver import StepOutcome
        self.called = True
        return StepOutcome(next_step=None, events=[])


async def test_reason_runs_compact_inline_and_routes_to_act(monkeypatch):
    spy = _SpyCompact()
    monkeypatch.setattr("loomex_core.core.loop.steps.reason.CompactStep", lambda: spy)

    pushed = []

    rs = ReasonStep()
    # force the should_compact path; stub the helpers ReasonStep calls
    async def _est(state, ctx):
        return 100000, True
    async def _resolve(state, ctx):
        return []
    async def _skill(state, ctx, name):
        return ""
    async def _should(state, ctx, est):
        return True

    monkeypatch.setattr(rs, "_estimate_tokens", _est)
    monkeypatch.setattr("loomex_core.core.loop.steps.reason.resolve_and_bind", _resolve)
    monkeypatch.setattr(rs, "_load_skill_instructions", _skill)
    monkeypatch.setattr(rs, "_should_compact", _should)

    agent = SimpleNamespace(
        id="a1",
        loop_config=SimpleNamespace(compact_keep_last=2, compact_token_ratio=0.8,
                                    compact_message_delta=0),
        loop_guard=SimpleNamespace(context_limit=1000, context_tokens=0,
                                   context_message_count=0, last_compact_at_message=0),
    )
    session = SimpleNamespace(id="s1", token_budget=0, token_used=0, context_limit=1000)
    task = SimpleNamespace(id="t1", settings=SimpleNamespace(skill_name="", purpose="act"))
    state = SimpleNamespace(agent=agent, session=session, task=task,
                            scope=SimpleNamespace(), extra={"template": None},
                            sequence_counter=0)

    class _Assembler:
        async def assemble(self, request):
            return SimpleNamespace(token_count=10, system="", messages=[], tools=[])

    class _TM:
        async def push_task(self, *a, **k):
            pushed.append(a)

    class _Bus:
        async def emit(self, ev):
            pass

    ctx = SimpleNamespace(assembler=_Assembler(), task_manager=_TM(), event_bus=_Bus(),
                          memory=SimpleNamespace(), provider_ctx=SimpleNamespace())

    outcome = await rs.execute(state, ctx)
    assert spy.called is True          # compaction ran inline
    assert pushed == []                # no compact Task pushed
    assert outcome.next_step == "act"  # still proceeds to act
```

- [ ] **Step 2: Run to verify it fails**

Run: `cd loomex-core && python -m pytest tests/unit/test_reason_inline_compact.py -v`
Expected: FAIL (current ReasonStep dispatches a compact task and returns `next_step=None`).

- [ ] **Step 3: Rewrite `ReasonStep.execute` compaction path**

In `reason.py`:

(a) Add an import near the top (after the existing `from loomex_core.core.loop.steps._capabilities import resolve_and_bind`):
```python
from loomex_core.core.loop.steps.compact import CompactStep
```

(b) Replace the body from the `# ── 4. 装配 prompt` section through the end of `execute` (the compact-dispatch branch and the final return) with:

```python
        # ── 4. compact 触发检查（命中则内联直调 CompactStep，不再派 task）──────────
        purpose = settings.purpose if isinstance(settings, NormalTaskSettings) else "act"
        # 先算一版 token_estimate 用于阈值判断（无基线时用装配结果兜底）
        probe = await ctx.assembler.assemble(ContextRequest(
            purpose=purpose, scope=state.scope, task=state.task, agent=agent, session=session,
            template=template, bound_capabilities=bound_capabilities,
            extra={"skill_instructions": skill_instructions},
        ))
        if token_estimate == 0:
            token_estimate = probe.token_count

        should_compact = await self._should_compact(state, ctx, token_estimate)
        if should_compact:
            state.extra["bound_capabilities"] = bound_capabilities
            compact_outcome = await CompactStep().execute(state, ctx)
            for ev in compact_outcome.events:
                await ctx.event_bus.emit(ev)

        # ── 5. 装配最终 prompt（在压缩后的 memory 之上）──────────────────────────
        prompt = await ctx.assembler.assemble(ContextRequest(
            purpose=purpose, scope=state.scope, task=state.task, agent=agent, session=session,
            template=template, bound_capabilities=bound_capabilities,
            extra={"skill_instructions": skill_instructions},
        ))

        return StepOutcome(
            next_step="act",
            state_patch={"assembled_prompt": prompt},
            events=[
                make_event(state, EventType.CONTEXT_TOKENS_ESTIMATED, payload={
                    "estimated_tokens": token_estimate,
                    "assembled_tokens": prompt.token_count,
                    "has_baseline": has_baseline,
                }),
                make_event(state, EventType.CONTEXT_ASSEMBLED, payload={"token_count": prompt.token_count}),
                make_event(state, EventType.REASON_COMPLETED, payload={
                    "estimated_tokens": token_estimate,
                    "assembled_token_count": prompt.token_count,
                }),
            ],
        )
```

(c) Delete the methods `_dispatch_compact` and `_compactable_layers` from `reason.py`, and remove the now-unused module constants `_AGENT_COMPACT_TYPES` / `_TASK_COMPACT_TYPES` (they now live in `compact.py`) and any now-unused imports (`CompactTaskSettings`, `generate_id`, `Task as _Task`). Keep `_should_compact`, `_estimate_tokens`, `_load_skill_instructions`, `NormalTaskSettings`.

Note on `has_baseline`: it comes from `_estimate_tokens` at the top of `execute` (`token_estimate, has_baseline = await self._estimate_tokens(state, ctx)`) — leave that line as-is.

- [ ] **Step 4: Run the test + full suite**

Run: `cd loomex-core && python -m pytest tests/unit/test_reason_inline_compact.py -v` → PASS.
Run: `cd loomex-core && python -m pytest -q` — compact-dispatch tests will now fail; note them for Task 6.

- [ ] **Step 5: Commit**

```
git add loomex-core/src/loomex_core/core/loop/steps/reason.py loomex-core/tests/unit/test_reason_inline_compact.py
git commit -m "feat(core/loop): run compaction inline in ReasonStep"
```

---

## Task 4: metadata_filler — background coroutine against the root task

**Files:**
- Modify: `loomex-core/src/loomex_core/core/loop/steps/metadata_filler.py`
- Modify: `loomex-core/src/loomex_core/core/runtime.py`
- Modify: `loomex-core/src/loomex_core/core/orchestrator/task_manager.py`
- Test: `loomex-core/tests/unit/test_metadata_filler_target.py`

- [ ] **Step 1: Write the failing test (step targets state.task directly)**

Create `loomex-core/tests/unit/test_metadata_filler_target.py`:

```python
"""MetadataFillerStep targets state.task directly (no MetadataFillerTaskSettings)."""

from __future__ import annotations

from types import SimpleNamespace

from loomex_core.core.loop.steps.metadata_filler import MetadataFillerStep


async def test_skips_when_title_present():
    state = SimpleNamespace(
        task=SimpleNamespace(id="t1", title="already set", settings=SimpleNamespace()),
        agent=SimpleNamespace(id="a1"),
        session=SimpleNamespace(id="s1"),
        scope=SimpleNamespace(session_id="s1", task_id="t1", agent_id="a1"),
        extra={"template": None},
    )
    emitted = []
    ctx = SimpleNamespace(
        task_manager=None,
        event_bus=SimpleNamespace(emit=lambda ev: emitted.append(ev) or _noop()),
    )
    # emit must be awaitable
    async def _emit(ev):
        emitted.append(ev)
    ctx.event_bus = SimpleNamespace(emit=_emit)
    outcome = await MetadataFillerStep().execute(state, ctx)
    assert outcome.next_step is None


def _noop():
    return None
```

(If your harness lacks `_make_event` plumbing for the skip path, the assertion is only that it returns `next_step=None` without raising.)

- [ ] **Step 2: Run to verify it fails**

Run: `cd loomex-core && python -m pytest tests/unit/test_metadata_filler_target.py -v`
Expected: FAIL (current step reads `MetadataFillerTaskSettings.target_task_id`).

- [ ] **Step 3: Update `MetadataFillerStep` to target `state.task`**

In `metadata_filler.py`, replace the target-resolution block at the top of `execute`:

```python
        from loomex_core.core.state.models import MetadataFillerTaskSettings

        s = state.task.settings
        target_task_id = s.target_task_id if isinstance(s, MetadataFillerTaskSettings) else ""
        target_task = (
            ctx.task_manager.get_task(target_task_id)
            if target_task_id and ctx.task_manager
            else state.task
        )
```

with:

```python
        target_task = state.task  # 直调：当前 state.task 即填充目标（root task）
        target_task_id = target_task.id
```

Then, in the assembler request and the `history_scope`, use `state.scope` directly:

```python
        request = ContextRequest(
            purpose="metadata_filler",
            scope=state.scope,
            task=target_task,
            agent=state.agent,
            session=state.session,
            template=state.extra.get("template"),
            bound_capabilities=list(mf_caps),
        )
```

Remove the `history_scope` construction and the `MemoryScope` import if it becomes unused. Keep everything else (skip-if-title, `_collect_mf_capabilities`, LLM call, gateway invoke, events) unchanged.

- [ ] **Step 4: Add the background launcher in `runtime.py`**

Add this method to the runtime class (near `_execute_task`), modeled on `_execute_task`'s context construction:

```python
    def _launch_metadata_filler(
        self,
        *,
        session: "Session",
        root_task: "Task",
        template: "AgentTemplate",
        memory: MemoryProvider,
        llm_account: str | None,
        llm_model: str | None,
        task_manager: "TaskManager",
    ) -> None:
        """旁路后台协程：直调 MetadataFillerStep 填充 root task 元数据（不包 Task / daemon）。

        用一个临时 agent（克隆 template）避免与 root task 主运行在 capability_cache 上冲突；
        只读 root scope、不写对话 memory（仅经 update_task_metadata 设 title/description）。
        """
        import dataclasses as _dc
        ephemeral = Agent(
            id=generate_id("agt"),
            session_id=session.id,
            template_id=template.id,
            template_version=template.version,
            status="RUNNING",
            tenant_id=session.tenant_id,
            loop_guard=LoopGuard(context_limit=session.context_limit),
            memory_config=template.memory_config,
            loop_config=template.loop_config,
            created_at=now_utc(),
        )
        provider_ctx = self._build_provider_ctx(session, root_task, ephemeral)
        skill_index = self._skill_provider_index()
        assembler = self._build_assembler(memory, provider_ctx, skill_index)
        gateway = self._build_gateway(memory)
        llm = self._resolve_llm(llm_account, llm_model)
        loop_ctx = self._build_loop_ctx(
            assembler, llm, memory, provider_ctx, gateway, skill_index, None, task_manager
        )
        # 读 root scope（root agent 的记忆），但用 ephemeral agent 跑这一步。
        scope = MemoryScope(session_id=session.id, task_id=root_task.id,
                            agent_id=session.root_agent_id or ephemeral.id)
        state = LoopState(
            run_id=generate_id("run"),
            session=session,
            task=root_task,
            agent=ephemeral,
            scope=scope,
            extra={"template": template},
        )

        async def _run() -> None:
            try:
                await MetadataFillerStep().execute(state, loop_ctx)
            except Exception:
                logger.exception("metadata_filler coroutine failed (ignored) for session %s", session.id)
            finally:
                self._capability_cache.evict(ephemeral.id)

        task_manager.track_background(asyncio.create_task(_run()))
```

Confirm the exact names of `_build_provider_ctx` / `_build_assembler` / `_build_gateway` / `_resolve_llm` / `_build_loop_ctx` / `_skill_provider_index` against `_execute_task` (lines ~1189-1196) and match their signatures. Ensure `Agent`, `LoopGuard`, `MemoryScope`, `LoopState`, `generate_id`, `now_utc`, `MetadataFillerStep`, `logger` are imported in `runtime.py` (most already are).

- [ ] **Step 5: Replace the start_session + resume launch sites**

In `start_session` (currently around line 604):

```python
        if not root_task.title:
            await task_manager.spawn_metadata_filler(
                session_id=session.id,
                target_task_id=root_task.id,
                user_prompt=params.user_prompt,
                tenant_id=params.tenant_id,
            )
```

replace with:

```python
        if not root_task.title:
            self._launch_metadata_filler(
                session=session, root_task=root_task, template=template, memory=memory,
                llm_account=params.llm_account, llm_model=params.llm_model,
                task_manager=task_manager,
            )
```

In the resume path (`_register_and_drain` is called at ~871; the resume function has `session`, `template`, `task_manager`, `self.providers.get_memory()`, `session.llm_provider`, `session.llm_model`, and the restored root task). After `set_runner` (~866) and before `_register_and_drain`, add:

```python
        root_task = task_manager.get_task(session.root_task_id) if hasattr(session, "root_task_id") else None
        if root_task is None:
            root_task = next((t for t in task_manager.all_tasks() if not t.parent_task_id), None)
        if root_task is not None and not root_task.title:
            self._launch_metadata_filler(
                session=session, root_task=root_task, template=template,
                memory=self.providers.get_memory(),
                llm_account=session.llm_provider, llm_model=session.llm_model,
                task_manager=task_manager,
            )
```

(The implementer should verify how the resume path identifies the root task — prefer an existing field if one exists; the parent-less fallback is the safe default.)

- [ ] **Step 6: Add `track_background` and drop daemon spawn in `task_manager.py`**

In `task_manager.py`:
- Rename the field `self._daemon_asyncio_tasks` → `self._background_asyncio_tasks` (line ~68) and update the await-before-close site (lines ~501-503) and any other references.
- Add:
```python
    def track_background(self, t: "asyncio.Task") -> None:
        """Track a fire-and-forget background coroutine so the session awaits it before close."""
        self._background_asyncio_tasks.add(t)
        t.add_done_callback(self._background_asyncio_tasks.discard)
```
- Delete `spawn_metadata_filler` (lines ~547-576) and `_run_daemon` (lines ~578-595).

- [ ] **Step 7: Run tests + full suite**

Run: `cd loomex-core && python -m pytest tests/unit/test_metadata_filler_target.py -v` → PASS.
Run: `cd loomex-core && python -m pytest -q` — daemon/restore references will fail to import or assert; addressed in Task 5/6.

- [ ] **Step 8: Commit**

```
git add loomex-core/src/loomex_core/core/loop/steps/metadata_filler.py loomex-core/src/loomex_core/core/runtime.py loomex-core/src/loomex_core/core/orchestrator/task_manager.py loomex-core/tests/unit/test_metadata_filler_target.py
git commit -m "feat(core): run metadata_filler as a background coroutine"
```

---

## Task 5: Remove daemon-restore + compact/metadata task scheduling paths

**Files:**
- Modify: `loomex-core/src/loomex_core/core/orchestrator/task_manager.py`
- Modify: `loomex-core/src/loomex_core/core/runtime.py`

- [ ] **Step 1: `restore()` — drop daemon-resumable, skip obsolete helper tasks**

In `task_manager.restore` (lines ~109-134), replace the daemon collection + return with logic that ignores compact/metadata helper tasks and returns nothing daemon-related:

```python
        from loomex_core.core.state.models import CompactTaskSettings, MetadataFillerTaskSettings
        for t in all_tasks:
            if t.status in _TERMINAL:
                continue
            if isinstance(t.settings, (CompactTaskSettings, MetadataFillerTaskSettings)):
                continue  # obsolete ephemeral helpers — never re-scheduled (recovery is condition-based)
            if t.status == "SUSPENDED":
                if t.id in parked:
                    continue
                children = self._children_of.get(t.id, set())
                if all(cid in terminal_ids for cid in children):
                    t.status = "PENDING"
                    self._queue.push(QueueEntry(
                        task_id=t.id, session_id=self._session_id, priority=t.priority,
                    ))
            else:
                t.status = "PENDING"
                t.retry_count = 0
                blocked = {dep for dep in (t.dag_deps or []) if dep not in terminal_ids}
                self._queue.push(QueueEntry(
                    task_id=t.id, session_id=self._session_id,
                    priority=t.priority, blocked_by=blocked,
                ))
```

Change the signature/return: `restore(...)` now returns `None`. Update its docstring (drop "Returns daemon tasks"). Update the caller in `runtime.py` (~838): `task_manager.restore(all_tasks, terminal_ids, parked_task_ids=parked_task_ids)` (drop the `daemon_resumable =` assignment).

- [ ] **Step 2: `_register_and_drain` — drop daemon re-spawn loop**

In `runtime.py` `_register_and_drain` (lines ~617-648): remove the `daemon_resumable` parameter and the `for t in (daemon_resumable or []):` loop (lines ~642-646). The body keeps the provider registration, `_on_done`, `set_session_done_callback`, and `asyncio.create_task(task_manager.drain())`. Update both call sites: `start_session` (`self._register_and_drain(session, task_manager)` — already that form) and the resume path (`self._register_and_drain(session, task_manager)` — drop the `daemon_resumable` arg at ~871).

- [ ] **Step 3: Remove the `_resolve` cases for the two settings**

In `runtime.py` `_make_task_runner._resolve` (lines ~699-724): delete the three `case CompactTaskSettings(...)` / `case CompactTaskSettings()` / `case MetadataFillerTaskSettings()` branches. These task types are no longer scheduled (restore skips them; nothing creates them). Leave the `NormalTaskSettings` cases intact. Keep the `CompactTaskSettings` / `MetadataFillerTaskSettings` dataclasses and `deserialize_settings` in `state/models.py` untouched (event-replay back-compat).

- [ ] **Step 4: Grep for stragglers**

Run (note any remaining references to remove or adjust):
```
cd loomex-core && python - <<'PY'
import subprocess
for pat in ["_run_daemon", "spawn_metadata_filler", "_daemon_asyncio_tasks", "daemon_resumable", "_dispatch_compact", "_build_compact_system", "_build_compact_messages"]:
    print("==", pat)
    print(subprocess.run(["git","grep","-n",pat],capture_output=True,text=True).stdout)
PY
```
Expected: no references in `src/` (matches only in this plan/spec docs are fine).

- [ ] **Step 5: Run full suite**

Run: `cd loomex-core && python -m pytest -q`. Import errors must be gone. Behavioral failures in old compact/metadata/daemon tests are addressed in Task 6.

- [ ] **Step 6: Commit**

```
git add loomex-core/src/loomex_core/core/orchestrator/task_manager.py loomex-core/src/loomex_core/core/runtime.py
git commit -m "refactor(core): remove daemon + compact/metadata task scheduling paths"
```

---

## Task 6: Test sweep + regression green

**Files:** tests under `loomex-core/tests/` (varies), no `src/` changes expected.

- [ ] **Step 1: Find tests coupled to the old task forms**

Run:
```
cd loomex-core && git grep -n -E "spawn_metadata_filler|_run_daemon|CompactTaskSettings|MetadataFillerTaskSettings|_dispatch_compact|COMPACT_DISPATCHED|daemon" tests/
```

- [ ] **Step 2: Rewrite each hit for the new behavior**

For each failing/now-invalid test, update its expectation:
- compact-dispatch tests (expecting a pushed `CompactTaskSettings` task / `COMPACT_DISPATCHED` / parent SUSPENDED) → assert inline behavior instead: after `ReasonStep` over budget, memory is folded (`apply_compact` called) and the step routes to `act`; no task pushed. Reuse the `_FakeMemory` pattern from `tests/unit/test_compact_step_inline.py`.
- metadata daemon tests (expecting `spawn_metadata_filler` / daemon task) → assert `MetadataFillerStep.execute` against a root task sets title/description and is skipped when a title exists.
- `test_minimal_loop.py` and integration tests that asserted compact/metadata as tasks → adjust to the inline/coroutine flow; keep assertions that still hold (e.g., soul in `req.system`).

Do not weaken meaningful assertions; rewrite them to the new contract. If a test only existed to cover removed plumbing (e.g., daemon restore re-spawn), delete it and note the deletion in the commit message.

- [ ] **Step 3: Full suite green**

Run: `cd loomex-core && python -m pytest -q`
Expected: all pass.

- [ ] **Step 4: Update docs**

In `loomex-core/ARCHITECTURE.md` (and `README.md` if it describes these), update any text that calls compact a dispatched sub-task or metadata_filler a daemon task, to: compact runs inline in `reason`; metadata_filler runs as a background coroutine; both reuse the actor assembly + a trailing instruction. Search terms: "compact", "daemon", "metadata", "spawn".

- [ ] **Step 5: Commit**

```
git add -A
git commit -m "test(core): align compact/metadata tests with inline/coroutine model; docs"
```

---

## Self-Review

- **Spec coverage:** A=Task 1; B=Tasks 2-3; C=Task 4; D (recovery)=Task 4 Step 5 (start+resume condition launch) + Task 2/3 (compact re-trigger inherent); Removals=Task 5; back-compat (keep dataclasses/deserialize, restore skips)=Task 5 Steps 1 & 3; Testing=Tasks 1-6.
- **Placeholders:** none — code provided for every code step. The resume-path root-task identification (Task 4 Step 5) is flagged for implementer verification with a safe default, not a TODO.
- **Type consistency:** `_build_trailing_messages`, `_foldable_layers`, `_launch_metadata_filler`, `track_background`, `_background_asyncio_tasks` named consistently across tasks. `CompactStep().execute` / `MetadataFillerStep().execute` signatures `(state, ctx)` match the `Step` protocol. `restore()` return type change (→ None) is propagated to its single caller in Task 5 Step 1.
- **Risk note:** Task 4 (concurrency) and Task 5 (cross-file removals) are the highest-risk; each ends on a full-suite run, and Task 6 closes regressions.
