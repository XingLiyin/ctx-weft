# Purpose Facets & Unified Step Prompts — Design

Date: 2026-06-14
Status: Approved (ready for implementation plan)
Scope: `loomex-core` + `src/loomex_host` (Python). TS/Java ports deferred.

## Problem

`compact` and `metadata_filler` currently have no per-purpose identity, even though
the protocol (`Purpose = Literal["act", "observe", "compact", "metadata_filler"]`,
`AgentTemplate.identity: dict[Purpose, IdentityFacet]`) allows a facet per purpose.

Today:

- The file-based `TemplateLoader` only ever populates `identity["act"]` (from `SOUL.md`)
  and `identity["observe"]` (from `ROLE.md`). It never produces `compact` /
  `metadata_filler` facets.
- `IdentitySource` resolves `template.identity.get(purpose) or template.identity.get("act")`,
  so for `compact`/`metadata_filler` it always falls back to the act SOUL.
- The detailed personas that *should* drive these steps live in dead standalone
  templates: `resources/agents/memory-compactor/SOUL.md` and
  `resources/agents/metadata_filler/SOUL.md`. Nothing dispatches to them.
- `metadata_filler` runs as a session-start **background coroutine** with an *ephemeral*
  cloned agent and its own bespoke capability collection (`_collect_mf_capabilities`),
  racing the root agent's first reasoning — an outlier that doesn't share the rest of
  the loop's single capability resolution.

## Principle

One agent, one template, one capability resolution per cycle. Every step shares the
**act-phase prompt skeleton**:

- system = act SOUL + Project Background
- messages = history + task context + resources
- tools = the single bound capability set, **purpose-filtered**

A step differs only by its `purpose` (which drives the tool filter *and* which facet is
used) and its trailing **instruction**. After this change, `agent template` is unchanged
across all steps in a cycle; only prompt organization varies.

### Key facts that make this work (verified in current code)

- `CapabilitySource` already filters tools by purpose:
  `if request.purpose in cap.purposes` (`assembler/sources/capability.py:35-38`).
  Passing the *full* bound set with `purpose="metadata_filler"` therefore yields only
  `update_task_metadata` automatically. No bespoke collector needed.
- `update_task_metadata` is a control tool (`@control_tool(purposes=["metadata_filler"])`)
  and is **always** in the agent's bound set + per-agent cache (control tools are added
  wholesale unless forbidden; `default` forbids none). So a step reusing that resolution
  can both *expose* it to the LLM (via purpose filter) and *route* the call (via cache)
  with no extra resolution and no `cache.put`.
- `compact` already reuses reasoning's bound set: `ReasonStep` stashes
  `bound_capabilities` into `state.extra` before inline-calling `CompactStep`
  (`reason.py:77`); `CompactStep` reads `state.extra.get("bound_capabilities", [])`.
- Per-cycle flow target:

  ```
  reason (resolve caps once, assemble, decide compact)
    -> [compact if triggered]
    -> act          --+ launched concurrently
       metadata_filler -+ (reuses reason's bound caps; non-blocking)
  ```

## Components

### 1. Loader: filename -> facet

File: `src/loomex_host/providers/templates/loader.py`

Generalize the current SOUL/ROLE handling to a filename -> purpose map:

| File         | Facet purpose     | cap_refs? | Required? |
|--------------|-------------------|-----------|-----------|
| `SOUL.md`    | `act`             | yes       | yes       |
| `ROLE.md`    | `observe`         | yes       | no        |
| `COMPACT.md` | `compact`         | no        | no        |
| `METADATA.md`| `metadata_filler` | no        | no        |

- `COMPACT.md` / `METADATA.md` are **body-only** personas. Optional frontmatter is
  tolerated but does not contribute `capability_refs` (only SOUL/ROLE do, preserving
  current behavior).
- A facet is created only when the file exists and has a non-empty body.

### 2. Default template personas

Dir: `resources/agents/default/`

- ADD `COMPACT.md` — content sourced from `resources/agents/memory-compactor/SOUL.md`
  (structured-summary compaction persona).
- ADD `METADATA.md` — content sourced from `resources/agents/metadata_filler/SOUL.md`
  (title / description / session_goal rules).
- The old standalone dirs (`memory-compactor/`, `metadata_filler/`) are **left in place**
  (user choice). They remain harmless duplicates; no runtime path dispatches to them.

### 3. Host resolver default-merge

Files: `src/loomex_host/providers/templates/resolver.py`, `src/loomex_host/config.py`

- Add `default_template_id: str = "default"` to host `Settings`, injected into
  `TemplateDirResolver` at construction.
- In `TemplateDirResolver.get()`: after loading the requested template, if its id is not
  the default, load the default template and **fill only missing** facets:
  `identity.setdefault("compact", default.identity["compact"])` and the same for
  `metadata_filler` (only when present in default).
  - Explicit per-template overrides win (`setdefault` does not clobber).
  - Skip self-merge (requested == default).
  - Missing default template or missing facet -> skip silently (debug log).
- Extract a pure helper `merge_default_facets(tmpl, default, purposes)` for reuse/testing.
- Scope of inheritance: `compact` + `metadata_filler` only. `observe` keeps its existing
  behavior (act fallback inside `IdentitySource`).

### 4. Composer: trailing-facet unification

File: `loomex-core/src/loomex_core/core/assembler/composer.py`

Make `compact` and `metadata_filler` structurally identical to `observe`:

| purpose          | system                  | trailing user message (merged into last user turn)        |
|------------------|-------------------------|-----------------------------------------------------------|
| observe          | act SOUL + background   | observe facet (ROLE) + `_OBSERVE_JUDGMENT_CUE` + reviews  |
| compact          | act SOUL + background   | compact facet + `_COMPACTION_INSTRUCTION`                 |
| metadata_filler  | act SOUL + background   | metadata_filler facet + `_METADATA_INSTRUCTION`           |

- compact/metadata_filler system switches from `_build_actor_system(blocks)`
  (purpose-facet-as-system) to the act-SOUL builder observe already uses
  (`_build_observer_system`, generalized/renamed to a neutral name, e.g.
  `_build_act_system`): system = `request.template.identity.get("act")` + background.
- The purpose facet (the `identity` block `IdentitySource` yields for that purpose) moves
  into the trailing user message, prepended to the existing instruction cue, then folded
  into the last user turn via `_merge_consecutive_messages`.
- Unify observe/compact/metadata_filler onto one helper:
  `_build_facet_trailing_messages(blocks, request, cue, extra_sections=[])`. observe passes
  its sub-task / upstream review lists as `extra_sections`.
- `IdentitySource` is **unchanged**: it still yields the purpose facet block; the composer
  now consumes it from the message side (like observe) rather than as system.
- The short `_COMPACTION_INSTRUCTION` / `_METADATA_INSTRUCTION` constants remain as the
  "now do it" cue after the persona — same split observe uses (facet = who you are,
  cue = act now).

### 5. metadata_filler execution model

Files: `loomex-core/src/loomex_core/core/runtime.py`,
`loomex-core/src/loomex_core/core/loop/steps/reason.py`,
`loomex-core/src/loomex_core/core/loop/steps/act.py`,
`loomex-core/src/loomex_core/core/loop/steps/metadata_filler.py`

- Delete `_launch_metadata_filler` (ephemeral-agent coroutine) and both call sites
  (`runtime.py:604-609` session start, `runtime.py:834-840` resume / new user message).
- `ReasonStep` always stashes `bound_capabilities` into `state.extra` (today it only does
  so on the compact path).
- At **act entry** (`ActStep`, before the turn loop): when `state.task` is the root task
  (`parent_task_id is None`) and `not state.task.title`, spawn metadata_filler as a
  fire-and-forget `asyncio` task **concurrent with act**.
  - Use a lightweight **snapshot** `LoopState` (same `agent`, `task`, `scope`, `session`;
    `extra` carrying `bound_capabilities` + `template`) so the concurrent step shares no
    mutable state with the running act step.
  - Keep a reference to the task to avoid GC; errors are logged and ignored (current
    fire-and-forget semantics preserved).
  - Idempotent by construction: once title is set the condition is false next cycle, and
    `MetadataFillerStep` already self-skips when `task.title` is set.
- Rewrite `MetadataFillerStep`:
  - Drop `_collect_mf_capabilities` and the `capability_cache.put` call.
  - Read `bound_capabilities` from `state.extra`.
  - Assemble with `purpose="metadata_filler"`; rely on `CapabilitySource` purpose filter to
    expose only `update_task_metadata`.
  - Invoke the resulting tool call via the existing `capability_gateway` (routing through
    the agent's existing cache).
  - Update the file-header docstring to reflect the new model.

## Data flow (after)

1. `resolver.get("foo")` loads `foo`, fills `compact`/`metadata_filler` facets from
   `default`, returns a complete template.
2. `ReasonStep` resolves the bound capability set once, assembles the act prompt, stashes
   `bound_capabilities` in `state.extra`.
3. `ActStep` runs; if root + empty title, it concurrently launches `MetadataFillerStep`
   on a snapshot state.
4. Each of act / observe / compact / metadata_filler assembles around the same skeleton;
   `CapabilitySource` purpose-filters tools; the composer puts the purpose facet (when
   present) in the trailing message with the step's cue.

## Testing

- Loader: `COMPACT.md` / `METADATA.md` present -> facets created; absent -> no facet;
  body-only (no cap_refs from these files).
- Resolver: non-default template inherits default's `compact`/`metadata_filler` facets;
  default itself untouched; explicit per-template override not clobbered; no self-merge;
  missing default / facet handled gracefully.
- Composer: for observe/compact/metadata_filler, system = act SOUL + background and the
  purpose facet appears in the trailing user message, merged when the last turn is `user`.
- metadata_filler: reuses `state.extra["bound_capabilities"]`; assembled tools auto-filter
  to `update_task_metadata` only; concurrent-launch condition fires exactly on root task
  with empty title.
- Adjust existing metadata_filler / compact tests to the new APIs (no ephemeral agent, no
  `_collect_mf_capabilities`, trailing-facet layout).

## Out of scope

- TS (`loomex-ts`) and Java (`loomej`) ports — separate follow-up.
- Deleting the old standalone template dirs.
- `observe` -> default-template fallback.
- Any `IdentitySource` change.
