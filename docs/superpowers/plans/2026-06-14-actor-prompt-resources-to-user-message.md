# Actor Prompt: Move Resources into First User Message — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** In the `act` and `observe` stages, keep only soul + project background in the system prompt and relocate the resources block (skills/tools/sub-agents) plus skill instructions into the task's first user message, without persisting them to memory.

**Architecture:** All edits live in `DefaultComposer` (`loomex-core/src/loomex_core/core/assembler/composer.py`). The system builders stop emitting resources/directive; a new resources *preamble* is prepended to the first `role=="user"` message at compose time. Because memory ingestion of `USER_PROMPT` is built from `task.*` fields (not from `prompt.messages`), compose-time injection is inherently non-persistent.

**Tech Stack:** Python, pytest. Composer is a plain dataclass-message builder; `LLMMessage` is a dataclass (mutate via `dataclasses.replace`).

**Spec:** `docs/superpowers/specs/2026-06-14-actor-prompt-resources-to-user-message-design.md`

---

## File Structure

- Modify: `loomex-core/src/loomex_core/core/assembler/composer.py`
  - `_build_actor_system` — drop resources + directive.
  - `_build_actor_messages` — inject resources preamble into first user message.
  - `_build_observer_system` — drop resources + directive.
  - New helpers: `_build_resources_preamble`, `_inject_resources_preamble`.
  - Add `import dataclasses`.
- Create: `loomex-core/tests/unit/test_composer_resources_placement.py` — new behavior tests.

Note on existing behavior preserved: `_build_resources_section` already **omits** the `## Available Skills` list when a `directive` block is present (`has_directive` guard). The directive (skill instructions) replaces the skill list. Tests below respect this: the "Available Skills" assertion uses a no-directive block set; the directive assertion uses a with-directive block set.

---

## Task 1: Relocate resources from actor system prompt to first user message

**Files:**
- Modify: `loomex-core/src/loomex_core/core/assembler/composer.py`
- Test: `loomex-core/tests/unit/test_composer_resources_placement.py`

- [ ] **Step 1: Write the failing tests**

Create `loomex-core/tests/unit/test_composer_resources_placement.py`:

```python
"""Resources/directive relocated from actor system prompt → first user message."""

from __future__ import annotations

from types import SimpleNamespace

from loomex_core.core.assembler.assembler import ContextBlock
from loomex_core.core.assembler.composer import DefaultComposer


def _cap_block(name: str, kind: str, desc: str) -> ContextBlock:
    return ContextBlock(
        id=f"cap-{name}",
        source="capability",
        kind="capabilities",
        target="system",
        content=desc,
        priority=1,
        token_estimate=1,
        metadata={"capability_name": name, "capability_kind": kind},
    )


def _identity_block(text: str) -> ContextBlock:
    return ContextBlock(id="id", source="identity", kind="identity", target="system",
                        content=text, priority=0, token_estimate=1, metadata={})


def _background_block(text: str) -> ContextBlock:
    return ContextBlock(id="bg", source="memory", kind="background", target="system",
                        content=text, priority=1, token_estimate=1, metadata={})


def _directive_block(text: str) -> ContextBlock:
    return ContextBlock(id="dir", source="identity:skill", kind="directive", target="system",
                        content=text, priority=1, token_estimate=1,
                        metadata={"kind": "skill_instructions"})


def _fresh_task_request() -> SimpleNamespace:
    task = SimpleNamespace(user_prompt_in_memory=False, process_report=None,
                           title="T", description="D", user_prompt="hello world")
    return SimpleNamespace(task=task)


def test_actor_system_has_only_soul_and_background() -> None:
    blocks = [
        _identity_block("SOUL TEXT"),
        _background_block("BG TEXT"),
        _cap_block("web_search", "tool", "search the web"),
        _cap_block("researcher", "agent", "a research subagent"),
        _directive_block("Instructions for skill 'x':\nDo the thing"),
    ]
    system = DefaultComposer()._build_actor_system(blocks)
    assert "SOUL TEXT" in system
    assert "## Project Background" in system and "BG TEXT" in system
    assert "## Available Tools" not in system
    assert "## Available Sub-Agents" not in system
    assert "## Instructions for the current task" not in system
    assert "Do the thing" not in system


def test_actor_first_user_message_has_tools_agents_and_directive() -> None:
    blocks = [
        _identity_block("SOUL TEXT"),
        _background_block("BG TEXT"),
        _cap_block("web_search", "tool", "search the web"),
        _cap_block("researcher", "agent", "a research subagent"),
        _directive_block("Instructions for skill 'x':\nDo the thing"),
    ]
    msgs = DefaultComposer()._build_actor_messages(blocks, _fresh_task_request())
    user_msgs = [m for m in msgs if m.role == "user"]
    first = user_msgs[0].content
    assert "## Available Tools" in first
    assert "## Available Sub-Agents" in first
    assert "## Instructions for the current task" in first
    assert "Do the thing" in first
    # task context still present, after the resources preamble
    assert "## Current Task" in first
    assert "hello world" in first
    assert first.index("## Available Tools") < first.index("## Current Task")


def test_actor_first_user_message_has_skills_when_no_directive() -> None:
    blocks = [
        _identity_block("SOUL TEXT"),
        _background_block("BG TEXT"),
        _cap_block("code_runner", "skill", "run code"),
    ]
    msgs = DefaultComposer()._build_actor_messages(blocks, _fresh_task_request())
    first = [m for m in msgs if m.role == "user"][0].content
    assert "## Available Skills" in first
    assert "code_runner" in first


def test_actor_no_resources_is_noop() -> None:
    blocks = [_identity_block("SOUL TEXT"), _background_block("BG TEXT")]
    msgs = DefaultComposer()._build_actor_messages(blocks, _fresh_task_request())
    first = [m for m in msgs if m.role == "user"][0].content
    assert "---\n\n## Current Task" not in first  # no preamble separator injected
    assert "## Current Task" in first
```

- [ ] **Step 2: Run the tests to verify they fail**

Run (from repo root):
```
cd loomex-core && python -m pytest tests/unit/test_composer_resources_placement.py -v
```
Expected: `test_actor_system_has_only_soul_and_background` FAILS (resources currently in system) and `test_actor_first_user_message_*` FAIL (resources currently not in user message). `test_actor_no_resources_is_noop` may already pass.

- [ ] **Step 3: Add `import dataclasses`**

In `composer.py`, after `from __future__ import annotations` (line ~45), add the import alongside the existing ones:

```python
import dataclasses
```

- [ ] **Step 4: Simplify `_build_actor_system`**

Replace the whole method:

```python
    def _build_actor_system(self, blocks: list["ContextBlock"]) -> str:
        """soul + Project Background，--- 分隔。

        Resources（skills/tools/agents）与 skill 指令不再进 system，
        改由 _build_actor_messages 注入本 task 首条 user message（不入 memory）。
        """
        identity = self._first_kind(blocks, "identity")
        background = self._first_kind(blocks, "background")

        parts: list[str] = []
        if identity:
            parts.append(content_to_text(identity.content))
        if background:
            parts.append(f"## Project Background\n\n{content_to_text(background.content)}")

        return "\n\n---\n\n".join(parts)
```

- [ ] **Step 5: Add the preamble helpers**

Add two methods to `DefaultComposer` (place them right after `_build_resources_section`):

```python
    def _build_resources_preamble(self, blocks: list["ContextBlock"]) -> str:
        """Resources（skills/tools/agents）+ 当前 task 的 skill 指令，作为首条 user message 前缀。

        与 system 解耦：这些是 per-task 资源，随对话发送但不入 memory。
        directive 带显式标题，告诉模型这是「当前任务的指令」。
        """
        parts: list[str] = []
        resources_section = self._build_resources_section(blocks)
        if resources_section:
            parts.append(resources_section)
        directive = self._first_kind(blocks, "directive")
        if directive:
            parts.append(
                f"## Instructions for the current task\n\n{content_to_text(directive.content)}"
            )
        return "\n\n".join(parts)

    def _inject_resources_preamble(
        self, messages: list[LLMMessage], blocks: list["ContextBlock"]
    ) -> list[LLMMessage]:
        """把 resources 前缀拼到首条 user message 内容前（仅发送，不入 memory）。空前缀为 no-op。"""
        preamble = self._build_resources_preamble(blocks)
        if not preamble:
            return messages
        out = list(messages)
        for i, m in enumerate(out):
            if m.role == "user":
                base = m.content if isinstance(m.content, str) else content_to_text(m.content)
                out[i] = dataclasses.replace(m, content=f"{preamble}\n\n---\n\n{base}")
                return out
        return out
```

- [ ] **Step 6: Inject the preamble in `_build_actor_messages`**

Find the tail of `_build_actor_messages`:

```python
        if parts:
            messages.append(LLMMessage(role="user", content="\n\n".join(parts)))
        merged = _merge_consecutive_messages(messages)
        # 兜底：actor prompt 必须以 user 回合结尾——避免以 assistant/tool 结尾让模型困惑地续写自己。
        # 正常情况下 active/retry 的 Current Progress 已是末条 user；此处仅覆盖 summary 为空等边角。
        if merged and merged[-1].role != "user":
            merged.append(LLMMessage(role="user", content="Continue with the task above."))
        return merged
```

Replace with (add one line before `return`):

```python
        if parts:
            messages.append(LLMMessage(role="user", content="\n\n".join(parts)))
        merged = _merge_consecutive_messages(messages)
        # 兜底：actor prompt 必须以 user 回合结尾——避免以 assistant/tool 结尾让模型困惑地续写自己。
        # 正常情况下 active/retry 的 Current Progress 已是末条 user；此处仅覆盖 summary 为空等边角。
        if merged and merged[-1].role != "user":
            merged.append(LLMMessage(role="user", content="Continue with the task above."))
        # Resources（skills/tools/agents）+ skill 指令注入首条 user message（仅发送，不入 memory）。
        merged = self._inject_resources_preamble(merged, blocks)
        return merged
```

- [ ] **Step 7: Run the tests to verify they pass**

Run:
```
cd loomex-core && python -m pytest tests/unit/test_composer_resources_placement.py -v
```
Expected: all PASS.

- [ ] **Step 8: Commit**

```
git add loomex-core/src/loomex_core/core/assembler/composer.py loomex-core/tests/unit/test_composer_resources_placement.py
git commit -m "feat(core/assembler): move actor resources into first user message"
```

---

## Task 2: Drop resources/directive from the observer system prompt

**Files:**
- Modify: `loomex-core/src/loomex_core/core/assembler/composer.py`
- Test: `loomex-core/tests/unit/test_composer_resources_placement.py`

- [ ] **Step 1: Write the failing tests**

Append to `loomex-core/tests/unit/test_composer_resources_placement.py`:

```python
def test_observer_system_excludes_resources_and_directive() -> None:
    template = SimpleNamespace(identity={"act": SimpleNamespace(text="ACT SOUL")})
    request = SimpleNamespace(template=template)
    blocks = [
        _background_block("BG TEXT"),
        _cap_block("report_task_outcome", "tool", "report the outcome"),
        _directive_block("Instructions for skill 'x':\nDo the thing"),
    ]
    system = DefaultComposer()._build_observer_system(blocks, request)
    assert "ACT SOUL" in system
    assert "## Project Background" in system and "BG TEXT" in system
    assert "## Available Tools" not in system
    assert "## Instructions for the current task" not in system
    assert "Do the thing" not in system


def test_observer_messages_inject_resources_and_keep_role() -> None:
    blocks = [
        _identity_block("OBSERVER ROLE"),  # purpose=observe → identity block is the ROLE
        _cap_block("report_task_outcome", "tool", "report the outcome"),
    ]
    task = SimpleNamespace(title="T", description="d", user_prompt="up",
                           user_prompt_in_memory=False, process_report=None)
    request = SimpleNamespace(task=task)
    msgs = DefaultComposer()._build_observer_messages(blocks, request)
    user_msgs = [m for m in msgs if m.role == "user"]
    assert "## Available Tools" in user_msgs[0].content
    assert "report the outcome" in user_msgs[0].content
    joined = "\n".join(m.content for m in msgs if isinstance(m.content, str))
    assert "OBSERVER ROLE" in joined
```

- [ ] **Step 2: Run the new tests to verify they fail**

Run:
```
cd loomex-core && python -m pytest tests/unit/test_composer_resources_placement.py::test_observer_system_excludes_resources_and_directive tests/unit/test_composer_resources_placement.py::test_observer_messages_inject_resources_and_keep_role -v
```
Expected: `test_observer_system_excludes_resources_and_directive` FAILS (resources/directive currently in observer system). `test_observer_messages_inject_resources_and_keep_role` should already PASS (it inherits Task 1's `_build_actor_messages` injection).

- [ ] **Step 3: Simplify `_build_observer_system`**

Replace the whole method:

```python
    def _build_observer_system(
        self, blocks: list["ContextBlock"], request: "ContextRequest"
    ) -> str:
        """Observer system = act identity(SOUL) + Project Background。

        与 act 同构：resources（observe 工具）与 skill 指令不进 system，改由
        _build_observer_messages（经 _build_actor_messages）注入首条 user message。
        observe 的 ROLE 由 _build_observer_messages 放进尾部 user message。
        """
        parts: list[str] = []
        act_facet = request.template.identity.get("act") if request.template else None
        if act_facet and getattr(act_facet, "text", ""):
            parts.append(act_facet.text)
        background = self._first_kind(blocks, "background")
        if background:
            parts.append(f"## Project Background\n\n{content_to_text(background.content)}")
        return "\n\n---\n\n".join(parts)
```

(`_build_observer_messages` is unchanged — it already calls `_build_actor_messages`, which now injects the preamble.)

- [ ] **Step 4: Run the tests to verify they pass**

Run:
```
cd loomex-core && python -m pytest tests/unit/test_composer_resources_placement.py -v
```
Expected: all PASS.

- [ ] **Step 5: Commit**

```
git add loomex-core/src/loomex_core/core/assembler/composer.py loomex-core/tests/unit/test_composer_resources_placement.py
git commit -m "feat(core/assembler): drop resources from observer system prompt"
```

---

## Task 3: Full-suite regression check

**Files:** none (verification only)

- [ ] **Step 1: Run the whole unit + integration suite**

Run:
```
cd loomex-core && python -m pytest -q
```
Expected: PASS. Pay attention to:
- `tests/integration/test_minimal_loop.py` — asserts the soul (`"You are a helpful echo agent"`) is in `req.system`; still true.
- `tests/unit/test_assembler_reconstruction.py` — uses blocks with no capability/directive blocks, so the preamble is empty (no-op); still passes.

- [ ] **Step 2: If any test asserted resources in `req.system`, fix it**

If a previously-green test fails because it expected `## Available Tools`/`## Available Skills`/skill instructions inside `req.system`, update that assertion to look in the first user message instead (the resources moved there by design). Show the diff in the commit. If no such failure occurs, skip this step.

- [ ] **Step 3: Update the doc-split note if needed**

If `loomex-core/ARCHITECTURE.md` or `README.md` documents the actor system-prompt layout (search for "Available Skills" / "Project Background" / "system prompt"), update the description to reflect that resources/skill-instructions now live in the first user message. If no such documentation exists, skip.

- [ ] **Step 4: Commit any fixes**

```
git add -A
git commit -m "test(core): adjust assertions for relocated actor resources"
```

---

## Self-Review Notes

- **Spec coverage:** system-only-soul+background (Task 1 Step 4, Task 2 Step 3); resources→first user message (Task 1 Steps 5–6); directive heading `## Instructions for the current task` (Task 1 Step 5); both act + observe (Tasks 1 & 2); not-in-memory (architecturally guaranteed — no code persists `prompt.messages`; covered by reasoning, no test needed); existing-behavior `has_directive` skill-list skip respected (Task 1 Steps 1 & 3 split into two tests).
- **Placeholders:** none — all code blocks complete.
- **Type consistency:** helper names `_build_resources_preamble` / `_inject_resources_preamble` used consistently; `dataclasses.replace` on `LLMMessage` matches the existing pattern in `act.py:_inject_act_guidance`.
