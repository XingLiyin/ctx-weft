# Prompt assets

This directory has two surfaces only:

- `steps/`: complete prompt programs for `act`, `observe`, `compact`, and
  `metadata`. They own all model-visible organization and wording. `_shared.*`
  contains locale-specific presentation macros; `_assemble.jinja` applies the
  common placement and trimming policy through `PromptBuilder`.
- `runtime/`: named Jinja macros for short messages emitted while the loop is
  already running. They are rendered through `runtime_prompt()`.

Python collectors and `PromptBuilder` pass structured facts. They must not render
headings, sections, or prompt fragments before invoking a purpose template.

## Task history records

`runtime/tasks.en.jinja` and `runtime/tasks.zh.jinja` define
`task_memory_record(record)`. Edit this macro to change the task-history labels;
Python supplies facts only. `record` has `kind` (`start`, `result`, `recap`,
`final_reply`, `finish`), `task_id`, `parent_task_id`, `title`, `description`,
`outcome`, and `body`. Attribute values are escaped; body text is preserved.
Framework records use the user role; a delivered final reply keeps the assistant
role. These are historical facts, not new user instructions or callable tools.

New AGENT conversation records persist the rendered text and
`metadata.task_memory = {version: 1, kind, group_id}`. Grouping and compaction use
metadata, never parse labels, so the macro can change freely. New records retain
their persisted presentation on read. Editing a template affects future writes.

For old AGENT records only, the assembler projects synthesized `start_task`,
per-child `delegate_task`, and `finish_task` calls/results through the same macro.
This preserves source IDs, timestamps and storage; no database migration or event
rewrite is needed. Template edits also apply to this legacy read projection.
TASK conversation records and real business tool calls remain unchanged.

Observe always attempts an LLM review at judgement boundaries, including when
ROLE is absent. ROLE supplies additional evaluation principles; the base observe
program still specifies judgement and `report_observation`. Failed or unusable
reviews fall back to success, without inventing a recap or deleting uncovered raw
history. A valid continue/fail judgement keeps its normal meaning. Cancellation,
user resumption and task suspension still take precedence over settlement.
