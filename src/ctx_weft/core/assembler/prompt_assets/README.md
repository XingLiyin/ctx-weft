# Prompt assets

This directory has two surfaces only:

- `steps/`: complete prompt programs for `act`, `observe`, `compact`,
  `metadata`, and the instruction-free `history` entry point. They own all model-visible organization and wording. `_shared.*`
  contains locale-specific presentation macros; `_assemble.jinja` applies the
  common placement and trimming policy through `PromptBuilder`.
- `runtime/`: named Jinja macros for short messages emitted while the loop is
  already running. They are rendered through `runtime_prompt()`.

Python collectors and `PromptBuilder` pass structured facts. They must not render
headings, sections, or prompt fragments before invoking a purpose template.

`sources/_history.py::history_facts()` retains every candidate record and describes
task ownership, closure completeness, reply-source validity, ancestry and logical
ordering. It does not select a visible view or construct replacement messages.
`steps/_history.jinja` owns full/minimal selection, reply deduplication and wrapping,
and registers alternatives through the generic `prompt.alternative(group)` call
block. Message anchors are updated only when that program emits a message.
`PromptBuilder` accounts for ordinary operations and atomic groups; it has no
task-specific visibility rules. `compile_history()` executes the same program
without instructions, image downgrading or budget trimming, for inherited
snapshots and compact token estimates.

`legacy_task_memory.py` only normalizes old synthetic calls into structured facts;
the history template renders those facts. It is the independently removable
compatibility layer. Current lifecycle metadata helpers remain with
`loop/task_memory_fold.py`, and writers invoke the runtime label macro directly.

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

Dispatch labels follow task lifecycle order: start, inline child execution and
closure, then terminal result before the parent resumes. Running acknowledgements
stay at the start until replaced. Terminal results use the committed completion
time, including after recovery; cross-agent results follow the same rule while
keeping the child's private execution in its own agent scope. Historical results
anchored to start are projected after a known inline child closure using task IDs
and metadata, without changing stored rows or parsing rendered tags. When no child
closure is available, historical timestamps are retained.

Closed tasks have mutually exclusive full and minimal prompt views. A full capsule
shows its retained task body, recap, final reply and a self-closing finish marker
with task ID, parent ID (if any) and outcome, without the report body. The stored
finish report retains its summary and alone forms the minimal view. This is a
template-only presentation choice; storage is unchanged. Short tasks select the original assistant answer using the persisted
`finish_policy.final_reply_record_id`, without copying its body into AGENT memory.
Legacy raw answers are projected when an unambiguous final prose turn survives.

The finish report is the minimal task capsule, retaining the goal, outcome and
essential result. Budget trimming first replaces an entire closed task subtree
with this alternative; it never independently drops the answer and leaves a recap.
If even the minimal view does not fit, normal group trimming applies. Required
current-task groups remain protected. This selection changes no stored rows.

Persistent demotion atomically replaces the task subtree's details with one finish
record and `finish_policy.capsule_state = "lean"`. Late reports update that minimal
record without resurrecting a recap or final reply. IDs, timestamps and metadata
provide identity and recovery; no logic parses the rendered labels.
Closures with `has_summary = false` keep raw evidence and do not offer an empty
finish as a compression alternative. Inherited snapshot rows follow the receiving
task's capsule lifetime, including snapshots written before this change.

Observe always attempts an LLM review at judgement boundaries, including when
ROLE is absent. ROLE supplies additional evaluation principles; the base observe
program still specifies judgement and `report_observation`. Failed or unusable
reviews fall back to success, without inventing a recap or deleting uncovered raw
history. A valid continue/fail judgement keeps its normal meaning. Cancellation,
user resumption and task suspension still take precedence over settlement.
