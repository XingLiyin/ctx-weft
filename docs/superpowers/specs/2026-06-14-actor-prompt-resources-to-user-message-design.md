# Move resources out of the actor system prompt into the task's first user message

Date: 2026-06-14
Status: Approved (design)
Area: `loomex-core` — context assembler / composer

## Problem

In the **act** and **observe** stages the system prompt currently mixes *fixed*
identity content with *per-task* resources. `DefaultComposer._build_actor_system`
renders, joined by `---`:

```
{soul}                       ← identity (act facet)
---
## Project Background
{background}
---
## Available Skills / ## Available Tools / ## Available Sub-Agents   ← resources_section
---
{skill_instructions}         ← directive
```

We want the system prompt to hold only the *fixed* layer (soul + project
background). Per-task resources (skills, tools, sub-agents) and the skill
instructions should move into the conversation as a preamble on the **first user
message** of the current task — and must **not** be persisted to memory.

## Goal / target layout (both `act` and `observe`)

```
SYSTEM:
  {soul}                       ← identity (act facet)
  ---
  ## Project Background
  {background}

USER (first user message of the task):
  ## Available Skills ...       ┐
  ## Available Tools ...        │ resources_section
  ## Available Sub-Agents ...   ┘
  ## Instructions for the current task
  {skill_instructions}          ← directive, with an explanatory heading
  ---
  {existing first-user-message content: Task Background / Current Task / Current Message}
```

Tool *function-calling definitions* (`prompt.tools`) are unchanged — every tool,
control tools included, stays callable. Only the descriptive **text** relocates.
Control capabilities therefore remain available to the model via the `tools`
array even though their text description now lives in the user message.

## Decisions (resolved during brainstorming)

- **Scope of relocation:** the *entire* `resources_section`
  (`## Available Skills` / `## Available Tools` / `## Available Sub-Agents`) moves
  to the user message — including the text for `control:*` tools. Nothing in the
  resources block stays in the system prompt.
- **What remains in the system prompt:** soul + `## Project Background` only.
- **`skill_instructions` (directive):** also moves to the user message, rendered
  under an explanatory heading (`## Instructions for the current task`) so the
  model knows this block is the instruction set for the task it is executing.
- **Stages affected:** both `act` (and the act-path `metadata_filler`) and
  `observe`.
- **Placement:** injected at compose time into the first `role == "user"` message
  of the assembled list. Never written to memory.

## Changes — all in `loomex-core/src/loomex_core/core/assembler/composer.py`

1. **`_build_actor_system`** — drop `resources_section` and `directive`; keep
   `identity` + `## Project Background` only (joined by `---`).

2. **`_build_actor_messages`** — build a *resources preamble*:
   - `resources_section` (unchanged `_build_resources_section` output), plus
   - the `directive` block (if present) rendered as
     `## Instructions for the current task\n\n{directive content}`.

   If the preamble is non-empty, prepend it followed by `---` to the **first**
   `role == "user"` message of the assembled message list. The prepend happens
   after `_merge_consecutive_messages` and the "must end with user" fallback, so
   there is always at least one user message to attach to. An empty preamble is a
   no-op (no `---`, no change).

3. **`_build_observer_system`** — drop `resources_section` and `directive`; keep
   act-soul (`template.identity['act']`) + `## Project Background`.

4. **`_build_observer_messages`** — no change required. It already calls
   `_build_actor_messages`, so the preamble is injected automatically; the trailing
   observe ROLE / judgment message is unaffected.

`_build_resources_section` is unchanged in behavior; it is simply now called from
the message builder rather than the system builder. The directive heading is
applied in the composer (a presentation concern), not in
`reason._load_skill_instructions`.

## Why "not in memory" holds automatically

Memory ingestion of `USER_PROMPT` builds its own content string from `task.*`
fields and never reads `prompt.messages`:

- `core/loop/driver.py:204` — ingests `## Current Task` / `## Current Message`
  from `task.title/description/user_prompt` at task start.
- `core/loop/steps/suspend.py:32` and `core/runtime.py:904` — same pattern on
  resume / HITL paths.

The composer only assembles what is *sent* to the LLM; it does not persist. So a
compose-time injection into the first user message is inherently non-persistent —
the same pattern already used by `_inject_act_guidance` in `core/loop/steps/act.py`
("仅发送，不入 memory").

## Placement nuance

- **Fresh task:** the first user message *is* the task-context message, so
  resources sit directly above `## Current Task` (matches the approved layout).
- **Resumed task:** the first user message is the reconstructed `user_prompt` from
  history, so resources attach there as a task preamble, while `## Current Progress`
  stays on the trailing user message. Because each compose reconstructs history
  from memory and re-injects the preamble fresh, the resources are present on every
  turn and never accumulate in memory.

## Testing

Existing tests are unaffected (verified):
- `tests/integration/test_minimal_loop.py:164` asserts the soul stays in
  `req.system` — it does.
- `tests/unit/test_assembler_reconstruction.py` calls `_build_actor_messages` /
  `_build_observer_messages` with blocks that contain no capability/directive
  blocks, so the preamble is empty (no-op).

New unit tests (in `tests/unit/`, exercising `DefaultComposer` with capability +
directive blocks):
1. **Actor system** contains the soul and `## Project Background` but does **not**
   contain `## Available Tools` / `## Available Skills` / the directive text.
2. **Actor first user message** contains the `resources_section` headings and the
   `## Instructions for the current task` directive block.
3. **Observer system** likewise excludes resources/directive; **observer messages**
   include them on the first user message (and still end with the observe ROLE).
4. Empty-capability case stays a no-op (covers the existing reconstruction tests).

## Out of scope

- No change to `prompt.tools` (function-calling definitions).
- No change to `compact` assembly.
- No change to memory ingestion paths.
- No change to `_build_resources_section` rendering format.
