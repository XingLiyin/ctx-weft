# compact & metadata_filler as directly-invoked steps (not tasks)

Date: 2026-06-14
Status: Approved (design)
Area: `loomex-core` — loop steps / assembler / runtime / task orchestrator

## Problem

`compact` and `metadata_filler` are each implemented as a single-step run wrapped
in a full `Task` + `StepDriver`:

- **compact** — `ReasonStep` dispatches one `CompactTaskSettings` sub-task per
  foldable layer via `task_manager.push_task`, sets the parent `SUSPENDED`, and the
  parent is resumed by `_try_resume_parent` after the compact sub-tasks finish. The
  compact sub-task instantiates a dedicated `memory-compactor` agent/template.
- **metadata_filler** — `runtime.start_session` calls
  `task_manager.spawn_metadata_filler`, which creates an `is_daemon=True` task and
  fire-and-forgets it via `_run_daemon` (bypassing the queue). It instantiates a
  dedicated `metadata_filler` agent/template and reads the target task's scope.

Both reuse the heavy Task/agent/driver machinery to run exactly one step
(`CompactStep` / `MetadataFillerStep`, each returning `next_step=None`). This is
indirection we don't need. We want to **trigger the step directly at the point of
need**, with no surrounding Task.

## Goal

Keep `CompactStep` and `MetadataFillerStep` as `Step` classes, but invoke them
**directly** — `compact` inline inside `ReasonStep`, `metadata_filler` from a
background coroutine at session start — reusing the current task's loop context and
the actor assembly. No `Task` objects, no daemon, no parent suspend/resume.

## Decisions (resolved during brainstorming)

1. **Assembly mirrors `observe`.** `compact` and `metadata_filler` reuse the
   act-stage system prompt (soul + `## Project Background`) and the act messages
   (full conversation), then append **one trailing user message** with the step's
   instruction. They reuse the current task's template — no dedicated
   `memory-compactor` / `metadata_filler` template instantiation.
2. **metadata_filler execution model:** a background coroutine that directly
   `await`s the step (keeps concurrency / does not delay the first response). No
   `Task`, no daemon.
3. **compact summarization:** ONE summary LLM call over the reused act context,
   applied to fold each over-budget layer (was: one call per layer).
4. **Minimal recovery via condition re-evaluation** (not Task restore):
   - compact re-triggers on the next `reason` if still over budget.
   - metadata_filler is (re)launched on session start **and on resume/restore**
     whenever `root_task.title` is empty.

## Architecture

### A. Assembler — `compact` / `metadata_filler` become observe-shaped purposes

In `core/assembler/composer.py`, `compose()` currently routes:
`act`/`metadata_filler` → actor builders; `observe` → observer builders;
`compact` → bespoke compact builders.

New routing: `compact` and `metadata_filler` are assembled like `observe` —
reuse the actor system + actor messages, then append a trailing instruction user
message (merged into the last user turn by `_merge_consecutive_messages`):

- **system** = `_build_actor_system(blocks)` (soul + `## Project Background`). For
  these purposes the `identity` block is the act soul (IdentitySource falls back to
  `identity["act"]` when the purpose facet is absent — already its behavior).
- **messages** = `_build_actor_messages(blocks, request)` + trailing user message:
  - `compact` → `_COMPACTION_INSTRUCTION` (the "summarize into `[Context so far]`,
    preserve intents/facts/decisions/tool-results/unfinished-threads, output only
    the summary" text, lifted from the current `_COMPACTION_SYSTEM_BASE`).
  - `metadata_filler` → `_METADATA_INSTRUCTION` ("set the current task's title and
    description (and optionally session goal) by calling `update_task_metadata`
    exactly once, then stop").
- **tools**:
  - `compact` → `[]` (expects a text reply).
  - `metadata_filler` → `_collect_llm_tools(blocks)` (the `update_task_metadata`
    tool), unchanged from today's actor-path behavior.

**Removed from composer:** `_build_compact_system` (already dead — `CompactStep`
overrode it) and `_build_compact_messages` (the `_format_history` blob). compact now
sees the real multi-turn conversation. A small shared helper
`_build_with_trailing(blocks, request, instruction)` renders system + actor messages
+ trailing instruction; `observe`, `compact`, and `metadata_filler` all use it (the
observe trailing message keeps its extra ROLE + review-list sections).

### B. compact — inline inside `ReasonStep`

`CompactStep` is refactored to be **self-contained and context-driven** (no
`CompactTaskSettings`):

- `CompactStep.execute(state, ctx)`:
  1. Compute foldable layers from `state.scope` (move `_compactable_layers` logic
     here; `keep_last = state.agent.loop_config.compact_keep_last`).
  2. If none foldable → no-op (`next_step=None`, no events beyond a skip log).
  3. Assemble `purpose="compact"` once (reused act context + compaction
     instruction), stream one summary via `ctx.llm`. On LLM failure fall back to
     `"[Context compacted]"` (truncation-only), as today.
  4. For each foldable layer, `ctx.memory.apply_compact(scope=state.scope,
     summary=..., keep_last=..., layer=...)`.
  5. Emit `MEMORY_COMPACT_STARTED` / `MEMORY_COMPACTED` (per layer). Return
     `StepOutcome(next_step=None, events=...)`.

`ReasonStep.execute` uses it as a subroutine instead of dispatching:
1. estimate tokens; resolve capabilities; load skill instructions.
2. **if `should_compact`** → `await CompactStep().execute(state, ctx)` inline and
   emit its events. (No parent suspend, no task dispatch.)
3. assemble the act prompt over the now-compacted memory.
4. `next_step="act"`.

`ReasonStep._dispatch_compact` and `_compactable_layers` (as a ReasonStep method)
are removed; the foldable-layer logic moves into `CompactStep`. `scope.task_id` is
naturally `state.task.id` (the task being compacted), replacing the old
`parent_task_id` scope construction.

### C. metadata_filler — background coroutine at session start

Replace `task_manager.spawn_metadata_filler` + `_run_daemon` with a runtime helper
that launches a background `asyncio` coroutine:

- `runtime._launch_metadata_filler(session, root_task, root_template, ...)`:
  - Builds a `loop_ctx` (via the existing `_build_*` helpers) and a `LoopState`
    bound to `root_task` + `root_template`, scoped to the root task's memory.
  - Uses an **ephemeral agent** cloned from the root agent/template with a fresh
    `agent_id` so the concurrent metadata run does not collide with the root task's
    primary run on `capability_cache` / `loop_guard` (the metadata run reads the
    root scope but writes no conversation memory — it only sets `title`/`description`
    on the target task object via `update_task_metadata`).
  - `await MetadataFillerStep().execute(state, loop_ctx)` directly.
  - Tracked in a background-task set that the runtime/host awaits before closing the
    SSE stream (preserving the current "emit MetadataFiller events before close"
    guarantee).
- `MetadataFillerStep` keeps its current responsibility (skip if title already set;
  resolve `metadata_filler`-purpose tools; assemble `purpose="metadata_filler"`;
  one LLM call; route the `update_task_metadata` tool call through the gateway), but
  no longer reads `MetadataFillerTaskSettings` for the target — the target is the
  `state.task` (root task) passed in.

### D. Minimal recovery

- **compact:** no persisted state. The next `reason` re-evaluates the token budget;
  if still over and layers remain foldable, it compacts again. Self-healing.
- **metadata_filler:** the launch is condition-gated on `root_task.title == ""`.
  `runtime.start_session` launches it on a fresh session; the session
  resume/restore path re-launches it when the restored root task still has an empty
  title. The trigger condition *is* the recovery guarantee — no daemon restore.

## Removals

- `task_manager.spawn_metadata_filler`, `_run_daemon`, `_daemon_asyncio_tasks`, and
  the `restore()` daemon-resumable collection + `runtime` daemon re-spawn loop.
- `ReasonStep._dispatch_compact` (task creation) and the per-task compact dispatch.
- `runtime._resolve` cases for `CompactTaskSettings` / `MetadataFillerTaskSettings`
  and the `memory-compactor` / `metadata_filler` template instantiation for them.
- `composer._build_compact_system`, `composer._build_compact_messages`,
  `compact.py:_resolve_system_prompt`, `_COMPACTION_SYSTEM_BASE`.

**Back-compat for event replay:** keep the `CompactTaskSettings` /
`MetadataFillerTaskSettings` dataclasses and `deserialize_settings` handling so old
event streams still parse; only the live creation/scheduling paths are removed. The
restore path will ignore any historical in-flight compact/metadata task (it simply
won't be re-scheduled — acceptable for these ephemeral helpers). This is the one
behavior edge to confirm during planning.

## Error handling

- compact LLM failure → truncation-only fold (unchanged).
- metadata_filler failure (LLM or tool) → logged and ignored; title stays empty and
  is retried on the next session resume (condition re-evaluation).
- The background metadata coroutine catches and logs all exceptions so it can never
  fail the session (matching the current `_run_daemon` "ignored" semantics).

## Testing

New / updated tests (in `loomex-core/tests/`):
1. **Composer** — `purpose="compact"`: system = soul + background; messages = act
   conversation + trailing compaction instruction; `tools == []`. `purpose="metadata_filler"`:
   trailing metadata instruction present; `update_task_metadata` in tools.
2. **CompactStep inline** — given a scope with > keep_last foldable events in a
   layer, `execute` produces a summary and folds that layer; no-op when nothing
   foldable; one summary LLM call applied to multiple over-budget layers.
3. **ReasonStep** — when over budget, compaction runs inline (memory folded) and the
   step still routes to `act`; no compact Task is pushed to the TaskManager.
4. **metadata_filler coroutine** — launched on session start when root title empty;
   sets title/description; skipped when title present; relaunched on resume when
   title still empty; never raises into the session.
5. Existing compact-dispatch / daemon / `test_minimal_loop` expectations that
   asserted these as Tasks are rewritten for the new inline/coroutine behavior.

## Out of scope

- The memory layering model itself (agent/task layers, `apply_compact` semantics)
  is unchanged — only the trigger and the summarization prompt change.
- No change to `observe`'s trailing-message content (only the shared helper is
  extracted).
- No change to the actor assembly (resources-in-first-user-message behavior stays).
