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
