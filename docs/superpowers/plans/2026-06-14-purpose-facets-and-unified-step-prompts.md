# Purpose Facets & Unified Step Prompts Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give `compact` and `metadata_filler` real per-purpose identity facets (sourced from the `default` template, inherited via a host-resolver merge), unify observe/compact/metadata_filler onto one trailing-facet prompt layout, and fold metadata_filler into the loop (concurrent with act, reusing reasoning's single capability resolution).

**Architecture:** The host file loader maps `COMPACT.md`/`METADATA.md` → `identity["compact"]`/`identity["metadata_filler"]`. `TemplateDirResolver.get()` fills any missing compact/metadata_filler facet from the configured default template. The core composer drops the purpose facet into the trailing user message (like observe) instead of system. metadata_filler stops being a session-start ephemeral-agent coroutine; it launches concurrently at act entry on a snapshot `LoopState`, reusing the bound capability set reasoning already resolved (`CapabilitySource` already purpose-filters tools, so no bespoke collector is needed).

**Tech Stack:** Python 3.11, pytest (`asyncio_mode = "auto"`), dataclasses. Core package: `loomex-core/` (run pytest from there). Host package: `src/loomex_host/` with tests in repo-root `tests/`.

---

## File Structure

**Host (`src/loomex_host/`):**
- `providers/templates/loader.py` — add COMPACT.md/METADATA.md → facet mapping.
- `providers/templates/resolver.py` — `merge_default_facets()` helper + default-merge in `TemplateDirResolver.get()`.
- `config.py` — add `default_template_id` setting.
- `cli.py` — pass `default_template_id` into the resolver.

**Resources:**
- `resources/agents/default/COMPACT.md` (new) — compaction persona.
- `resources/agents/default/METADATA.md` (new) — metadata persona.

**Core (`loomex-core/src/loomex_core/`):**
- `core/assembler/composer.py` — trailing-facet unification.
- `core/loop/steps/reason.py` — always stash `bound_capabilities` in `state.extra`.
- `core/loop/steps/metadata_filler.py` — rewrite step, add `should_fill_metadata()` + `launch_metadata_filler()`.
- `core/loop/steps/act.py` — launch metadata_filler concurrently at act entry.
- `core/runtime.py` — remove the ephemeral `_launch_metadata_filler` coroutine + both call sites.

**Tests:**
- `tests/test_template_loader_facets.py` (new, host)
- `tests/test_template_resolver_merge.py` (new, host)
- `tests/test_host_config.py` (modify, host)
- `loomex-core/tests/unit/test_composer_compact_metadata.py` (rewrite)
- `loomex-core/tests/unit/test_metadata_filler_fill.py` (rewrite)
- `loomex-core/tests/unit/test_metadata_filler_launch.py` (new)

---

## Task 1: Loader facet files

**Files:**
- Modify: `src/loomex_host/providers/templates/loader.py`
- Test: `tests/test_template_loader_facets.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_template_loader_facets.py`:

```python
from loomex_host.providers.templates.loader import TemplateLoader


def _soul(d):
    (d / "SOUL.md").write_text("---\nname: x\nversion: 1.0.0\n---\nact soul", encoding="utf-8")


def test_loads_compact_and_metadata_facets(tmp_path):
    d = tmp_path / "agt"
    d.mkdir()
    _soul(d)
    (d / "COMPACT.md").write_text("compact persona", encoding="utf-8")
    (d / "METADATA.md").write_text("metadata persona", encoding="utf-8")

    t = TemplateLoader().load(d)

    assert t.identity["act"].text == "act soul"
    assert t.identity["compact"].text == "compact persona"
    assert t.identity["metadata_filler"].text == "metadata persona"


def test_missing_facet_files_yield_no_facet(tmp_path):
    d = tmp_path / "agt"
    d.mkdir()
    _soul(d)

    t = TemplateLoader().load(d)

    assert "compact" not in t.identity
    assert "metadata_filler" not in t.identity


def test_facet_file_with_frontmatter_uses_body_only(tmp_path):
    d = tmp_path / "agt"
    d.mkdir()
    _soul(d)
    (d / "COMPACT.md").write_text("---\nfoo: bar\n---\njust the body", encoding="utf-8")

    t = TemplateLoader().load(d)

    assert t.identity["compact"].text == "just the body"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd loomex-core 2>/dev/null; cd ..; python -m pytest tests/test_template_loader_facets.py -v`
(Run from repo root.) Expected: FAIL — `KeyError: 'compact'`.

- [ ] **Step 3: Implement the facet mapping**

In `src/loomex_host/providers/templates/loader.py`, find the block that builds `identity` (currently ends after the `ROLE.md` handling, around line 86, just before `loop_cfg = _parse_loop_config(...)`). Add the extra facet files right after the ROLE block:

```python
        # 额外 purpose facet：body-only persona（不贡献 capability_refs）。
        for fname, purpose in (("COMPACT.md", "compact"), ("METADATA.md", "metadata_filler")):
            fpath = template_dir / fname
            if not fpath.exists():
                continue
            _, facet_body = _parse_md(fpath.read_text(encoding="utf-8"))
            if facet_body:
                identity[purpose] = IdentityFacet(text=facet_body)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_template_loader_facets.py -v`
Expected: PASS (3 passed).

- [ ] **Step 5: Commit**

```bash
git add src/loomex_host/providers/templates/loader.py tests/test_template_loader_facets.py
git commit -m "feat(host): loader maps COMPACT.md/METADATA.md to purpose facets"
```

---

## Task 2: Default template personas

**Files:**
- Create: `resources/agents/default/COMPACT.md`
- Create: `resources/agents/default/METADATA.md`
- Test: `tests/test_template_loader_facets.py` (add a smoke test against the real dir)

- [ ] **Step 1: Write the failing smoke test**

Append to `tests/test_template_loader_facets.py`:

```python
def test_default_template_dir_has_compact_and_metadata_facets():
    from pathlib import Path
    default_dir = Path(__file__).resolve().parents[1] / "resources" / "agents" / "default"
    t = TemplateLoader().load(default_dir)
    assert t.identity["compact"].text.strip()
    assert t.identity["metadata_filler"].text.strip()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_template_loader_facets.py::test_default_template_dir_has_compact_and_metadata_facets -v`
Expected: FAIL — `KeyError: 'compact'` (files don't exist yet).

- [ ] **Step 3: Create `resources/agents/default/COMPACT.md`**

```markdown
你是一个记忆压缩代理。你的任务是阅读一段对话历史，并产出一份结构化摘要，供后续代理继续工作时使用。

你可以使用文件读取工具。当对话中提到了具体文件时，主动读取这些文件——这样你能在摘要中写入准确的当前内容，而不是依赖对话中可能已过时的描述。

只输出结构化摘要本身，不要任何前言或解释说明。

## 输出格式

### 会话目标
[一句话：整个会话试图完成什么]

### 已完成工作
[列表：每项已完成的任务、执行结果、产出内容]

### 关键产出
[列表：已创建或修改的重要文件——包含路径和一行内容说明]

### 当前状态
[正在进行或待处理的事项，以及已知的阻塞点]

### 重要上下文
[后续代理必须知道才能正确继续工作的事实、决策或约束——无则省略此节]
```

- [ ] **Step 4: Create `resources/agents/default/METADATA.md`**

```markdown
你是一个元数据填充助手，职责是为任务生成简洁的标题和描述，并维护 session 的整体目标。

**工作流程：**
1. 阅读任务描述中的指令，以及对话历史（如有）。
2. 针对当前任务生成：
   - `title`：≤20字，动词开头，概括当前任务的核心目标（如"分析…"、"生成…"、"实现…"）。
   - `description`：≤80字，说明当前任务要达成的结果；结合上下文补充必要背景，不涉及实现细节。
3. 判断 `session_goal`（见下方规则）。
4. 调用 `update_task_metadata(title, description, session_goal)` 保存结果。

**session_goal 规则：**

- 若任务描述中**未提及**当前 session goal（首次输入）：
  - 根据当前用户指令和对话历史，用≤60字概括用户在本 session 中想达到的**整体目的**。
  - 目标应反映用户的持久意图，而非单个操作步骤。
  - 将此理解填入 `session_goal`。

- 若任务描述中**已给出**当前 session goal：
  - 若用户的新指令表明**方向发生了转变**，你需要重新设置 session goal。
  - 判断标准：新消息是对原目标的延伸/细化 → 留空；新消息明确表示要做完全不同的事 → 填入新 goal。
  - 无法判断是否延续时，一律视为新任务

**约束：**
- 只调用一次 `update_task_metadata`，不做其他任何事。
- title、description、session_goal 使用与用户指令相同的语言。
- 不要在回复中解释思考过程。
```

- [ ] **Step 5: Run test to verify it passes**

Run: `python -m pytest tests/test_template_loader_facets.py::test_default_template_dir_has_compact_and_metadata_facets -v`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add resources/agents/default/COMPACT.md resources/agents/default/METADATA.md tests/test_template_loader_facets.py
git commit -m "feat(resources): add default compact + metadata personas as facet files"
```

---

## Task 3: Config + resolver default-merge

**Files:**
- Modify: `src/loomex_host/config.py` (add `default_template_id`)
- Modify: `src/loomex_host/providers/templates/resolver.py` (helper + merge in `get()`)
- Modify: `src/loomex_host/cli.py:69` (wire setting into resolver)
- Test: `tests/test_template_resolver_merge.py` (new), `tests/test_host_config.py` (modify)

- [ ] **Step 1: Write the failing config test**

In `tests/test_host_config.py`, add:

```python
def test_default_template_id_default(monkeypatch):
    _clear(monkeypatch)
    s = _fresh().Settings.from_env()
    assert s.default_template_id == "default"


def test_default_template_id_env_override(monkeypatch):
    _clear(monkeypatch)
    monkeypatch.setenv("LoomeX_DEFAULT_TEMPLATE_ID", "house")
    s = _fresh().Settings.from_env()
    assert s.default_template_id == "house"
```

- [ ] **Step 2: Run config test to verify it fails**

Run: `python -m pytest tests/test_host_config.py::test_default_template_id_default -v`
Expected: FAIL — `AttributeError: ... has no attribute 'default_template_id'`.

- [ ] **Step 3: Add the config field**

In `src/loomex_host/config.py`, add to the `Settings` dataclass (after `skill_pull_server_url: str`):

```python
    default_template_id: str
```

And in `from_env()` (after the `skill_pull_server_url=...` entry, before the closing `)`):

```python
            default_template_id=_str("LoomeX_DEFAULT_TEMPLATE_ID", "default") or "default",
```

- [ ] **Step 4: Run config test to verify it passes**

Run: `python -m pytest tests/test_host_config.py -v`
Expected: PASS (all, including the two new ones).

- [ ] **Step 5: Write the failing resolver test**

Create `tests/test_template_resolver_merge.py`:

```python
from loomex_core.protocols.template import (
    AgentTemplate,
    IdentityFacet,
    LoopConfig,
    MemoryConfig,
)
from loomex_host.providers.templates.loader import TemplateLoader
from loomex_host.providers.templates.resolver import (
    TemplateDirResolver,
    merge_default_facets,
)


def _tmpl(tid, identity):
    return AgentTemplate(
        id=tid,
        name=tid,
        version="1.0.0",
        identity=identity,
        capability_refs=[],
        memory_config=MemoryConfig(),
        loop_config=LoopConfig(),
    )


def test_merge_fills_missing_facets():
    default = _tmpl("default", {
        "act": IdentityFacet(text="d-act"),
        "compact": IdentityFacet(text="d-compact"),
        "metadata_filler": IdentityFacet(text="d-md"),
    })
    foo = _tmpl("foo", {"act": IdentityFacet(text="foo-act")})

    merge_default_facets(foo, default, ("compact", "metadata_filler"))

    assert foo.identity["compact"].text == "d-compact"
    assert foo.identity["metadata_filler"].text == "d-md"
    assert foo.identity["act"].text == "foo-act"  # untouched


def test_merge_does_not_clobber_explicit_facet():
    default = _tmpl("default", {"compact": IdentityFacet(text="d-compact")})
    foo = _tmpl("foo", {
        "act": IdentityFacet(text="foo-act"),
        "compact": IdentityFacet(text="own-compact"),
    })

    merge_default_facets(foo, default, ("compact", "metadata_filler"))

    assert foo.identity["compact"].text == "own-compact"


class _FakeStore:
    def __init__(self, mapping):
        self._m = mapping  # id -> Path

    async def get(self, tid):
        d = self._m.get(tid)
        return {"template_dir": str(d)} if d else None

    async def find_by_name(self, name):
        return await self.get(name)


def _make_dir(tmp_path, name, *, compact=None, metadata=None):
    d = tmp_path / name
    d.mkdir()
    (d / "SOUL.md").write_text(f"---\nname: {name}\nversion: 1.0.0\n---\n{name} soul", encoding="utf-8")
    if compact is not None:
        (d / "COMPACT.md").write_text(compact, encoding="utf-8")
    if metadata is not None:
        (d / "METADATA.md").write_text(metadata, encoding="utf-8")
    return d


async def test_resolver_inherits_default_facets(tmp_path):
    ddir = _make_dir(tmp_path, "default", compact="DEF-COMPACT", metadata="DEF-MD")
    fdir = _make_dir(tmp_path, "foo")  # no compact/metadata files
    resolver = TemplateDirResolver(
        _FakeStore({"default": ddir, "foo": fdir}),
        TemplateLoader(),
        default_template_id="default",
    )

    t = await resolver.get("foo", None, None)

    assert t.identity["compact"].text == "DEF-COMPACT"
    assert t.identity["metadata_filler"].text == "DEF-MD"


async def test_resolver_default_itself_not_self_merged(tmp_path):
    ddir = _make_dir(tmp_path, "default", compact="DEF-COMPACT", metadata="DEF-MD")
    resolver = TemplateDirResolver(
        _FakeStore({"default": ddir}),
        TemplateLoader(),
        default_template_id="default",
    )

    t = await resolver.get("default", None, None)

    assert t.identity["compact"].text == "DEF-COMPACT"  # from its own file
```

- [ ] **Step 6: Run resolver test to verify it fails**

Run: `python -m pytest tests/test_template_resolver_merge.py -v`
Expected: FAIL — `ImportError: cannot import name 'merge_default_facets'`.

- [ ] **Step 7: Implement helper + merge in resolver**

In `src/loomex_host/providers/templates/resolver.py`, add after the imports (before `# ── TemplateDirResolver ──`):

```python
DEFAULT_MERGE_PURPOSES = ("compact", "metadata_filler")


def merge_default_facets(template, default, purposes) -> None:
    """Fill the template's missing identity facets (in-place) from the default template."""
    for purpose in purposes:
        if purpose not in template.identity and purpose in default.identity:
            template.identity[purpose] = default.identity[purpose]
```

Change `TemplateDirResolver.__init__` to accept the default id:

```python
    def __init__(
        self,
        store: TemplateStore,
        loader: TemplateLoader | None = None,
        default_template_id: str = "default",
    ) -> None:
        self._store = store
        self._loader = loader or TemplateLoader()
        self._default_template_id = default_template_id
```

Replace the body of `get()` with:

```python
    async def get(
        self,
        template_id: str,
        version: str | None,
        ctx: ProviderContext,
    ) -> AgentTemplate:
        meta = await self._store.get(template_id)
        if meta is None:
            meta = await self._store.find_by_name(template_id)
        if meta is None:
            raise KeyError(f"Template not found: {template_id}")

        template = self._loader.load(Path(meta["template_dir"]))
        if template.id != self._default_template_id:
            default = await self._load_default()
            if default is not None:
                merge_default_facets(template, default, DEFAULT_MERGE_PURPOSES)
        return template

    async def _load_default(self) -> "AgentTemplate | None":
        try:
            meta = await self._store.get(self._default_template_id)
            if meta is None:
                meta = await self._store.find_by_name(self._default_template_id)
            if meta is None:
                return None
            return self._loader.load(Path(meta["template_dir"]))
        except Exception:
            logger.exception("TemplateDirResolver: failed to load default template")
            return None
```

- [ ] **Step 8: Run resolver test to verify it passes**

Run: `python -m pytest tests/test_template_resolver_merge.py -v`
Expected: PASS (4 passed).

- [ ] **Step 9: Wire the setting into the resolver**

In `src/loomex_host/cli.py:69`, change:

```python
    resolver = TemplateDirResolver(template_store, template_loader)
```

to:

```python
    resolver = TemplateDirResolver(
        template_store, template_loader, default_template_id=cfg.default_template_id
    )
```

- [ ] **Step 10: Commit**

```bash
git add src/loomex_host/config.py src/loomex_host/providers/templates/resolver.py src/loomex_host/cli.py tests/test_template_resolver_merge.py tests/test_host_config.py
git commit -m "feat(host): inherit compact/metadata facets from default template at resolve time"
```

---

## Task 4: Composer trailing-facet unification

**Files:**
- Modify: `loomex-core/src/loomex_core/core/assembler/composer.py`
- Test: `loomex-core/tests/unit/test_composer_compact_metadata.py` (rewrite)

- [ ] **Step 1: Rewrite the composer test**

Replace the entire contents of `loomex-core/tests/unit/test_composer_compact_metadata.py`:

```python
"""compact/metadata_filler assembled like observe: act SOUL in system, purpose facet in trailing message."""

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
    template = SimpleNamespace(identity={"act": SimpleNamespace(text="ACT-SOUL", style=None)})
    return SimpleNamespace(purpose=purpose, task=task, template=template)


async def test_compact_system_is_act_facet_and_persona_in_trailing():
    blocks = [_identity("COMPACT-PERSONA"), _bg("BG")]
    prompt = await DefaultComposer().compose(blocks, _req("compact"))

    assert "ACT-SOUL" in prompt.system
    assert "## Project Background" in prompt.system
    assert "COMPACT-PERSONA" not in prompt.system

    last_user = [m for m in prompt.messages if m.role == "user"][-1].content
    assert "COMPACT-PERSONA" in last_user           # persona rides the trailing message
    assert "[Context so far]" in last_user          # compaction cue present
    assert "do the thing" in last_user              # actor conversation reused
    assert prompt.tools == []


async def test_metadata_system_is_act_facet_and_persona_in_trailing():
    blocks = [_identity("MD-PERSONA"), _bg("BG"), _tool("update_task_metadata", "set title/desc")]
    prompt = await DefaultComposer().compose(blocks, _req("metadata_filler"))

    assert "ACT-SOUL" in prompt.system
    assert "MD-PERSONA" not in prompt.system

    last_user = [m for m in prompt.messages if m.role == "user"][-1].content
    assert "MD-PERSONA" in last_user
    assert "update_task_metadata" in last_user      # metadata cue names the tool
    assert any(t.name == "update_task_metadata" for t in prompt.tools)
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `cd loomex-core && python -m pytest tests/unit/test_composer_compact_metadata.py -v`
Expected: FAIL — `COMPACT-PERSONA` still appears in `prompt.system` (old behavior puts the facet in system).

- [ ] **Step 3: Rename `_build_observer_system` → `_build_act_system`**

In `loomex-core/src/loomex_core/core/assembler/composer.py`, rename the method `_build_observer_system` to `_build_act_system` (its body is unchanged). It currently reads:

```python
    def _build_act_system(
        self, blocks: list["ContextBlock"], request: "ContextRequest"
    ) -> str:
        """system = act identity(SOUL) + Project Background。observe/compact/metadata 共用。"""
        parts: list[str] = []
        act_facet = request.template.identity.get("act") if request.template else None
        if act_facet and getattr(act_facet, "text", ""):
            parts.append(act_facet.text)
        background = self._first_kind(blocks, "background")
        if background:
            parts.append(f"## Project Background\n\n{content_to_text(background.content)}")
        return "\n\n---\n\n".join(parts)
```

- [ ] **Step 4: Add the unified trailing-facet helper; remove `_build_trailing_messages`**

Replace the existing `_build_trailing_messages` method with this `_build_facet_trailing_messages`:

```python
    def _build_facet_trailing_messages(
        self,
        blocks: list["ContextBlock"],
        request: "ContextRequest",
        cue: str,
        extra_sections: list[str] | None = None,
        facet_fallback: str = "",
    ) -> list[LLMMessage]:
        """act 会话 + 末尾一条 user message：purpose facet + cue（+ 可选附加段）。

        observe / compact / metadata_filler 共用。facet 取 purpose 对应的 identity block
        （IdentitySource 已按 purpose 产出），并入最后一条 user 回合（避免连续 user）。
        """
        messages = self._build_actor_messages(blocks, request)
        facet = self._first_kind(blocks, "identity")
        facet_text = content_to_text(facet.content) if facet else ""
        if not facet_text:
            facet_text = facet_fallback

        sections: list[str] = []
        if facet_text:
            sections.extend([facet_text, "---"])
        sections.append(cue)
        if extra_sections:
            sections.extend(extra_sections)

        messages.append(LLMMessage(role="user", content="\n\n".join(sections)))
        return _merge_consecutive_messages(messages)
```

- [ ] **Step 5: Point compact/metadata at the new helpers in `compose()`**

In `compose()`, update the `observe`, `metadata_filler`, and `compact` branches:

```python
        elif request.purpose == "observe":
            system = self._build_act_system(blocks, request)
            messages = self._build_observer_messages(blocks, request)
            tools = self._collect_llm_tools(blocks)
        elif request.purpose == "metadata_filler":
            system = self._build_act_system(blocks, request)
            messages = self._build_facet_trailing_messages(blocks, request, _METADATA_INSTRUCTION)
            tools = self._collect_llm_tools(blocks)
        else:  # compact
            system = self._build_act_system(blocks, request)
            messages = self._build_facet_trailing_messages(blocks, request, _COMPACTION_INSTRUCTION)
            tools = []
```

- [ ] **Step 6: Rewrite `_build_observer_messages` to use the shared helper**

Replace the body of `_build_observer_messages` with:

```python
    def _build_observer_messages(
        self,
        blocks: list["ContextBlock"],
        request: "ContextRequest",
    ) -> list[LLMMessage]:
        """act 风格完整会话 + 尾部一条 observe user message（仅发送，不入 memory）。"""
        bb_blocks = [b for b in blocks if b.kind == "blackboard"]
        subtask_blocks = [b for b in bb_blocks if b.metadata.get("intent") == "subtask"]
        pred_blocks = [b for b in bb_blocks if b.metadata.get("intent") == "predecessor"]

        extra_sections: list[str] = []
        if subtask_blocks:
            extra_sections.append(
                "Your sub-task results (you may confirm / reopen these):\n"
                + self._render_bb(subtask_blocks)
            )
        if pred_blocks:
            extra_sections.append(
                "Upstream task results (read-only context):\n" + self._render_bb(pred_blocks)
            )

        return self._build_facet_trailing_messages(
            blocks,
            request,
            _OBSERVE_JUDGMENT_CUE,
            extra_sections=extra_sections,
            facet_fallback=_OBSERVER_ROLE_FALLBACK,
        )
```

- [ ] **Step 7: Run the composer + observer tests to verify they pass**

Run: `cd loomex-core && python -m pytest tests/unit/test_composer_compact_metadata.py tests/unit/ -k "composer or observ" -v`
Expected: PASS. If any observe assembler test referenced the old method name directly, update it to `_build_act_system` (search first: `grep -rn "_build_observer_system\|_build_trailing_messages" tests/`).

- [ ] **Step 8: Run the full core suite to catch regressions**

Run: `cd loomex-core && python -m pytest -q`
Expected: PASS (composer/observe/compact tests green; reason/metadata tests still pass — they're addressed in later tasks).

- [ ] **Step 9: Commit**

```bash
git add loomex-core/src/loomex_core/core/assembler/composer.py loomex-core/tests/unit/test_composer_compact_metadata.py
git commit -m "refactor(core): unify observe/compact/metadata onto trailing-facet prompt layout"
```

---

## Task 5: ReasonStep always stashes bound capabilities

**Files:**
- Modify: `loomex-core/src/loomex_core/core/loop/steps/reason.py:48-77`
- Test: `loomex-core/tests/unit/test_reason_inline_compact.py` (add assertion) — verify the existing test file's state object first.

- [ ] **Step 1: Write/extend the failing test**

Inspect `loomex-core/tests/unit/test_reason_inline_compact.py` to reuse its fixtures. Add a test asserting `state.extra["bound_capabilities"]` is populated after a non-compacting reason run. Minimal standalone version (adjust imports/fixtures to match the existing file's helpers if they differ):

```python
async def test_reason_stashes_bound_capabilities(monkeypatch):
    import loomex_core.core.loop.steps.reason as reason_mod
    from loomex_core.core.loop.steps.reason import ReasonStep

    sentinel = [object()]

    async def _fake_bind(state, ctx):
        return sentinel

    monkeypatch.setattr(reason_mod, "resolve_and_bind", _fake_bind)

    state, ctx = _make_reason_state_and_ctx()  # reuse this file's existing builder
    await ReasonStep().execute(state, ctx)

    assert state.extra["bound_capabilities"] is sentinel
```

If the existing file has no reusable builder, model `_make_reason_state_and_ctx()` on the setup already used by `test_reason_inline_compact.py` (same fakes for `assembler`, `llm`, `memory`, `event_bus`, `provider_ctx`, a `NormalTaskSettings` task, and an agent with `loop_config`/`loop_guard`).

- [ ] **Step 2: Run the test to verify it fails**

Run: `cd loomex-core && python -m pytest tests/unit/test_reason_inline_compact.py::test_reason_stashes_bound_capabilities -v`
Expected: FAIL — `KeyError: 'bound_capabilities'`.

- [ ] **Step 3: Always stash in ReasonStep**

In `reason.py`, right after line 48 (`bound_capabilities = await resolve_and_bind(state, ctx)`), add:

```python
        state.extra["bound_capabilities"] = bound_capabilities
```

Then remove the now-redundant assignment inside the compact branch (the existing `state.extra["bound_capabilities"] = bound_capabilities` line just before `compact_outcome = await CompactStep().execute(state, ctx)`).

- [ ] **Step 4: Run the test to verify it passes**

Run: `cd loomex-core && python -m pytest tests/unit/test_reason_inline_compact.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add loomex-core/src/loomex_core/core/loop/steps/reason.py loomex-core/tests/unit/test_reason_inline_compact.py
git commit -m "refactor(core): reason always stashes bound_capabilities for downstream steps"
```

---

## Task 6: metadata_filler — reuse bound caps, launch concurrently, drop ephemeral coroutine

**Files:**
- Modify: `loomex-core/src/loomex_core/core/loop/steps/metadata_filler.py` (rewrite step; add `should_fill_metadata`, `launch_metadata_filler`)
- Modify: `loomex-core/src/loomex_core/core/loop/steps/act.py` (launch at act entry)
- Modify: `loomex-core/src/loomex_core/core/runtime.py` (remove `_launch_metadata_filler` + 2 call sites)
- Test: `loomex-core/tests/unit/test_metadata_filler_fill.py` (rewrite), `loomex-core/tests/unit/test_metadata_filler_launch.py` (new)

- [ ] **Step 1: Rewrite the fill test**

Replace the entire contents of `loomex-core/tests/unit/test_metadata_filler_fill.py`:

```python
"""MetadataFillerStep fill path: reuses state.extra['bound_capabilities']; routes update_task_metadata."""

from __future__ import annotations

from types import SimpleNamespace

from loomex_core.core.loop.steps.metadata_filler import MetadataFillerStep
from loomex_core.protocols import LLMChunk, ToolCall
from loomex_core.protocols.capability import ToolCapability


class _FakeAssembler:
    def __init__(self):
        self.calls = []

    async def assemble(self, request):
        self.calls.append(request)
        return SimpleNamespace(system="SYS", messages=[], tools=[])


class _FakeLLM:
    def __init__(self, args):
        self._args = args

    async def complete(self, request, stream=True):
        yield LLMChunk(
            kind="tool_call",
            tool_call=ToolCall(id="tc1", name="update_task_metadata", arguments=self._args),
        )


class _FakeGateway:
    def __init__(self):
        self.invocations = []

    async def invoke(self, *, tool_name, arguments, state, ctx):
        self.invocations.append({"tool_name": tool_name, "arguments": arguments})


def _state(bound):
    return SimpleNamespace(
        run_id="r1",
        sequence_counter=0,
        agent=SimpleNamespace(id="a1", runtime={"llm_model": "mock"}),
        session=SimpleNamespace(id="s1", tenant_id="te1"),
        task=SimpleNamespace(id="t1", title="", settings=SimpleNamespace()),
        scope=SimpleNamespace(session_id="s1", task_id="t1", agent_id="a1"),
        extra={"template": SimpleNamespace(), "bound_capabilities": bound},
    )


async def test_fills_metadata_via_tool_call():
    args = {"title": "Build the thing", "description": "A clear description", "session_goal": "Ship it"}
    cap = ToolCapability(id="cap1", name="update_task_metadata", purposes=["metadata_filler"])

    emitted = []

    async def _emit(ev):
        emitted.append(ev)

    gateway = _FakeGateway()
    ctx = SimpleNamespace(
        provider_ctx=SimpleNamespace(),
        capability_cache=None,
        assembler=_FakeAssembler(),
        llm=_FakeLLM(args),
        capability_gateway=gateway,
        event_bus=SimpleNamespace(emit=_emit),
    )

    state = _state([cap])
    outcome = await MetadataFillerStep().execute(state, ctx)

    assert outcome.next_step is None
    assert len(gateway.invocations) == 1
    assert gateway.invocations[0]["tool_name"] == "update_task_metadata"
    assert gateway.invocations[0]["arguments"] == args

    types = [ev.type for ev in emitted]
    assert "MetadataFillerStarted" in types
    assert "MetadataFillerToolCall" in types
    assert "MetadataFillerCompleted" in types
    assert "MetadataFillerSkipped" not in types

    # The assemble request carried the full bound set with the metadata_filler purpose.
    req = ctx.assembler.calls[0]
    assert req.purpose == "metadata_filler"
    assert req.bound_capabilities == [cap]


async def test_skips_when_no_metadata_tool_in_bound_set():
    emitted = []

    async def _emit(ev):
        emitted.append(ev)

    ctx = SimpleNamespace(
        provider_ctx=SimpleNamespace(),
        capability_cache=None,
        assembler=_FakeAssembler(),
        llm=_FakeLLM({}),
        capability_gateway=_FakeGateway(),
        event_bus=SimpleNamespace(emit=_emit),
    )
    state = _state([])  # no caps at all
    outcome = await MetadataFillerStep().execute(state, ctx)

    assert outcome.next_step is None
    assert "MetadataFillerSkipped" in [ev.type for ev in emitted]
    assert ctx.assembler.calls == []  # never assembled
```

- [ ] **Step 2: Write the new launch/predicate test**

Create `loomex-core/tests/unit/test_metadata_filler_launch.py`:

```python
"""should_fill_metadata predicate + launch_metadata_filler concurrency."""

from __future__ import annotations

from types import SimpleNamespace

import loomex_core.core.loop.steps.metadata_filler as mf
from loomex_core.core.loop.driver import LoopState
from loomex_core.core.loop.steps.metadata_filler import (
    MetadataFillerStep,
    should_fill_metadata,
    launch_metadata_filler,
)


def test_should_fill_root_with_empty_title():
    assert should_fill_metadata(SimpleNamespace(parent_task_id=None, title=""))


def test_skip_when_title_present():
    assert not should_fill_metadata(SimpleNamespace(parent_task_id=None, title="x"))


def test_skip_when_subtask():
    assert not should_fill_metadata(SimpleNamespace(parent_task_id="p", title=""))


async def test_launch_runs_step_on_snapshot(monkeypatch):
    seen = {}

    async def _fake_execute(self, state, ctx):
        seen["run_id"] = state.run_id
        seen["bound"] = state.extra.get("bound_capabilities")
        from loomex_core.core.loop.driver import StepOutcome
        return StepOutcome(next_step=None)

    monkeypatch.setattr(MetadataFillerStep, "execute", _fake_execute)

    state = SimpleNamespace(
        run_id="orig-run",
        sequence_counter=5,
        session=SimpleNamespace(id="s1"),
        task=SimpleNamespace(id="t1", parent_task_id=None, title=""),
        agent=SimpleNamespace(id="a1"),
        scope=SimpleNamespace(),
        assembled_prompt=object(),
        transcript=[object()],
        verdict=object(),
        extra={"template": SimpleNamespace(), "bound_capabilities": ["CAP"]},
    )
    ctx = SimpleNamespace(task_manager=None)

    task = launch_metadata_filler(state, ctx)
    await task

    assert seen["bound"] == ["CAP"]
    assert seen["run_id"] != "orig-run"  # ran on a fresh snapshot, not the live state
```

Note: `launch_metadata_filler` builds its snapshot with `dataclasses.replace`, which requires a real `LoopState`. The test above uses a `SimpleNamespace`; the helper must therefore build the snapshot via explicit field copy rather than `dataclasses.replace` (see Step 4) so it works on any state-like object. The import of `LoopState` above is only to keep the module reference; it can be dropped if unused.

- [ ] **Step 3: Run both tests to verify they fail**

Run: `cd loomex-core && python -m pytest tests/unit/test_metadata_filler_fill.py tests/unit/test_metadata_filler_launch.py -v`
Expected: FAIL — `ImportError: cannot import name 'should_fill_metadata'` and fill test errors on old `_collect_mf_capabilities` path.

- [ ] **Step 4: Rewrite `metadata_filler.py`**

Replace the entire contents of `loomex-core/src/loomex_core/core/loop/steps/metadata_filler.py`:

```python
"""MetadataFillerStep: single-shot step that fills task title/description and session goal.

Launched concurrently with ActStep (see launch_metadata_filler) on a snapshot of the loop
state, reusing the capability set ReasonStep already bound. Assembles with
purpose="metadata_filler"; CapabilitySource auto-filters tools to update_task_metadata.
Skips entirely if the title is already set or no metadata_filler tool is bound.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from loomex_core.core.loop.driver import LoopContext, LoopState, Step, StepOutcome, make_event
from loomex_core.core.events import EventType
from loomex_core.core.utils import generate_id
from loomex_core.protocols.capability import ToolCapability

logger = logging.getLogger(__name__)

# Strong refs to fire-and-forget tasks when no TaskManager is available (avoids GC).
_background_tasks: set[asyncio.Task] = set()


def should_fill_metadata(task: Any) -> bool:
    """True when this is the root task (no parent) and still lacks a title."""
    return getattr(task, "parent_task_id", None) is None and not getattr(task, "title", "")


def launch_metadata_filler(state: LoopState, ctx: LoopContext) -> asyncio.Task:
    """Fire-and-forget: run MetadataFillerStep concurrently with act on a snapshot state.

    The snapshot shares agent/task/scope/session (read-mostly) but has its own run_id,
    sequence_counter and a copied extra dict, so it never corrupts the live act state.
    Returns the created asyncio.Task (callers may ignore it).
    """
    snapshot = LoopState(
        run_id=generate_id("run"),
        session=state.session,
        task=state.task,
        agent=state.agent,
        scope=state.scope,
        extra=dict(state.extra),
    )

    async def _run() -> None:
        try:
            await MetadataFillerStep().execute(snapshot, ctx)
        except Exception:
            logger.exception("metadata_filler concurrent run failed (ignored)")

    task = asyncio.create_task(_run())
    tm = getattr(ctx, "task_manager", None)
    if tm is not None and hasattr(tm, "track_background"):
        tm.track_background(task)
    else:
        _background_tasks.add(task)
        task.add_done_callback(_background_tasks.discard)
    return task


class MetadataFillerStep(Step):
    """Single-shot LLM step that enriches task metadata and session goal."""

    name = "metadata_filler"

    async def execute(self, state: LoopState, ctx: LoopContext) -> StepOutcome:
        target_task = state.task
        target_task_id = target_task.id

        if target_task and target_task.title:
            await ctx.event_bus.emit(make_event(state, EventType.METADATA_FILLER_SKIPPED, payload={
                "task_id": target_task_id,
                "target_task_id": target_task_id,
            }))
            return StepOutcome(next_step=None)

        bound = list(state.extra.get("bound_capabilities", []))
        has_mf_tool = any(
            isinstance(c, ToolCapability) and "metadata_filler" in c.purposes for c in bound
        )
        if not has_mf_tool:
            logger.warning("MetadataFillerStep: no metadata_filler tool bound for task %s", target_task_id)
            await ctx.event_bus.emit(make_event(state, EventType.METADATA_FILLER_SKIPPED, payload={
                "task_id": target_task_id,
                "target_task_id": target_task_id,
                "reason": "no_tools",
            }))
            return StepOutcome(next_step=None)

        # ── Assemble prompt via shared assembler (CapabilitySource filters by purpose) ──
        from loomex_core.core.assembler.assembler import ContextRequest

        request = ContextRequest(
            purpose="metadata_filler",
            scope=state.scope,
            task=target_task,
            agent=state.agent,
            session=state.session,
            template=state.extra.get("template"),
            bound_capabilities=bound,
        )
        prompt = await ctx.assembler.assemble(request)

        from loomex_core.protocols import LLMRequest

        llm_request = LLMRequest(
            model=state.agent.runtime.get("llm_model", "mock"),
            system=prompt.system,
            messages=prompt.messages,
            tools=prompt.tools,
        )

        await ctx.event_bus.emit(make_event(state, EventType.METADATA_FILLER_STARTED, payload={
            "task_id": target_task_id,
            "target_task_id": target_task_id,
        }))
        await ctx.event_bus.emit(make_event(state, EventType.METADATA_FILLER_LLM_PROMPT, payload={
            "system": prompt.system,
            "messages": [
                {"role": m.role, "content": m.content if isinstance(m.content, str) else ""}
                for m in prompt.messages
            ],
            "tool_names": [t.name for t in prompt.tools],
        }))

        tool_name = ""
        tool_args: dict[str, Any] = {}
        try:
            async for chunk in ctx.llm.complete(llm_request, stream=True):
                if chunk.kind == "tool_call" and chunk.tool_call:
                    tool_name = chunk.tool_call.name
                    tool_args = chunk.tool_call.arguments
        except Exception as exc:
            if getattr(exc, "retriable", False):
                logger.warning("MetadataFillerStep: LLM call failed for task %s: %s", target_task_id, exc)
            else:
                logger.exception("MetadataFillerStep: LLM call failed for task %s", target_task_id)
            return StepOutcome(next_step=None)

        if tool_name and ctx.capability_gateway is not None:
            await ctx.capability_gateway.invoke(
                tool_name=tool_name,
                arguments=tool_args,
                state=state,
                ctx=ctx,
            )

        await ctx.event_bus.emit(make_event(state, EventType.METADATA_FILLER_TOOL_CALL, payload={
            "title": tool_args.get("title", ""),
            "description": tool_args.get("description", ""),
            "session_goal": tool_args.get("session_goal", ""),
        }))
        await ctx.event_bus.emit(make_event(state, EventType.METADATA_FILLER_COMPLETED, payload={
            "title": tool_args.get("title", ""),
            "description": tool_args.get("description", ""),
            "session_goal": tool_args.get("session_goal", ""),
        }))

        return StepOutcome(next_step=None)
```

- [ ] **Step 5: Run the metadata_filler unit tests**

Run: `cd loomex-core && python -m pytest tests/unit/test_metadata_filler_fill.py tests/unit/test_metadata_filler_launch.py tests/unit/test_metadata_filler_target.py -v`
Expected: PASS. (`test_metadata_filler_target.py::test_skips_when_title_present` still passes — title-present returns before touching bound caps.)

- [ ] **Step 6: Launch metadata_filler at act entry**

In `loomex-core/src/loomex_core/core/loop/steps/act.py`, add the import near the other step/control imports (top of file):

```python
from loomex_core.core.loop.steps.metadata_filler import launch_metadata_filler, should_fill_metadata
```

Then in `ActStep.execute`, right after the `prompt` guard (after `current_messages = _inject_act_guidance(current_messages, state, ctx)`, before the `for turn_num in range(...)` loop), add:

```python
        # 元数据填充与 act 并发：root task 首轮（无标题）时旁路一个快照运行（不阻塞 act）。
        if should_fill_metadata(state.task) and state.extra.get("bound_capabilities"):
            launch_metadata_filler(state, ctx)
```

- [ ] **Step 7: Remove the ephemeral launcher + call sites in `runtime.py`**

In `loomex-core/src/loomex_core/core/runtime.py`:

1. Delete the call site around line 604-609 (the `if not root_task.title:` block calling `self._launch_metadata_filler(...)` just before `self._register_and_drain(session, task_manager)`).
2. Delete the call site around line 833-840 (the `root_task = next(...)` + `if root_task is not None and not root_task.title:` block calling `self._launch_metadata_filler(...)`).
3. Delete the entire `_launch_metadata_filler` method (def around line 1145 through its `task_manager.track_background(asyncio.create_task(_run()))` at ~1200).

- [ ] **Step 8: Run the full core suite**

Run: `cd loomex-core && python -m pytest -q`
Expected: PASS. If a test imported `_launch_metadata_filler` or `_collect_mf_capabilities`, update or remove it (search: `grep -rn "_launch_metadata_filler\|_collect_mf_capabilities" loomex-core/`).

- [ ] **Step 9: Lint changed core files**

Run: `cd loomex-core && python -m ruff check src/loomex_core/core/runtime.py src/loomex_core/core/loop/steps/act.py src/loomex_core/core/loop/steps/metadata_filler.py src/loomex_core/core/loop/steps/reason.py src/loomex_core/core/assembler/composer.py`
Expected: no errors. Remove any now-unused imports ruff flags in `runtime.py`.

- [ ] **Step 10: Commit**

```bash
git add loomex-core/src/loomex_core/core/loop/steps/metadata_filler.py loomex-core/src/loomex_core/core/loop/steps/act.py loomex-core/src/loomex_core/core/runtime.py loomex-core/tests/unit/test_metadata_filler_fill.py loomex-core/tests/unit/test_metadata_filler_launch.py
git commit -m "feat(core): run metadata_filler concurrently with act, reusing bound capabilities"
```

---

## Task 7: Full verification

- [ ] **Step 1: Run the host suite**

Run (from repo root): `python -m pytest tests/ -q`
Expected: PASS.

- [ ] **Step 2: Run the core suite**

Run: `cd loomex-core && python -m pytest -q`
Expected: PASS.

- [ ] **Step 3: Confirm no dangling references**

Run: `grep -rn "_launch_metadata_filler\|_collect_mf_capabilities\|_build_observer_system\|_build_trailing_messages" loomex-core/src loomex-core/tests src tests`
Expected: no matches (all renamed/removed).

- [ ] **Step 4: Final commit if anything changed**

```bash
git add -A
git commit -m "test: verification pass for purpose facets + unified step prompts" || echo "nothing to commit"
```

---

## Self-Review Notes

- **Spec coverage:** Component 1 → Task 1; Component 2 → Task 2; Component 3 → Task 3; Component 4 → Task 4; Component 5 → Tasks 5+6. Testing section → Tasks 1-6 + Task 7. All spec components mapped.
- **Out of scope** (TS/Java ports, deleting old standalone dirs, observe→default fallback, IdentitySource changes) — untouched, as specified.
- **Type/name consistency:** `_build_act_system` (renamed from `_build_observer_system`), `_build_facet_trailing_messages` (replaces `_build_trailing_messages`), `merge_default_facets`, `DEFAULT_MERGE_PURPOSES`, `should_fill_metadata`, `launch_metadata_filler` used consistently across tasks.
- **Note for executor:** `launch_metadata_filler` builds its snapshot by explicit `LoopState(...)` construction (not `dataclasses.replace`) so the launch unit test can pass a `SimpleNamespace` live state.
```
