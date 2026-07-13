# text_calls Dialect Refactor + `<tool_code>` Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Reorganize `src/ctx_weft/providers/llm/text_calls.py` into concern-separated sections behind a `TextToolCallDialect` abstraction, add `<tool_code>` support, and collapse `_finalize.py`'s triplicated tag-parsing branches into one `scan_text_tool_calls` loop.

**Architecture:** Parallel-change (expand → migrate → contract). Task 1 *adds* the dialect layer + `<tool_code>` alongside the existing API (suite stays green). Task 2 *migrates* the sole consumer (`_finalize.py`) to the new entry point. Task 3 *contracts* — deletes the now-dead `TextScan` and old `contains_*`/`parse_*` API and their tests. Every task commits with a green suite.

**Tech Stack:** Python 3.11, pytest (`asyncio_mode=auto`), stdlib `re`/`json`/`dataclasses`.

## Global Constraints

- Ruff line-length: **100**.
- Test runner: `pytest` from repo root; `testpaths=["tests"]`, `addopts="-ra -q --strict-markers"`.
- All new/changed source keeps the file's existing Chinese-comment docstring style.
- No new third-party dependencies (stdlib only).
- Behavior for every existing format (`<tool_call>` JSON/XML, `<function=>`, `<minimax:tool_call>`, `<think>`, streaming visible gate) MUST be preserved unchanged; the only new runtime behavior is `<tool_code>` support.
- `WRAPPED` dialect must precede `MINIMAX` in `DIALECTS` (preserves `_finalize`'s current detection order).

---

### Task 1: Add the dialect layer + `<tool_code>` support (additive)

Introduce the abstraction and `<tool_code>` parsing *without removing* any existing public function. The old `TextScan`/`parse_tool_calls_from_text`/`contains_*` stay in place this task so the suite and `_finalize.py` keep working; they are deleted in Task 3.

**Files:**
- Modify: `src/ctx_weft/providers/llm/text_calls.py`
- Test: `tests/unit/test_text_calls.py`

**Interfaces:**
- Consumes: existing `ParsedToolCall`, `_parse_single_tool_call`, `_parse_xml_tool_call`, minimax regexes.
- Produces (relied on by Tasks 2–3):
  - `class TextToolCallDialect` — frozen dataclass with `name: str`, `open_markers: tuple[str, ...]`, `parse: Callable[[str], list[ParsedToolCall]]`, method `detect(text: str) -> bool`.
  - `DIALECTS: tuple[TextToolCallDialect, ...]` = `(WRAPPED_DIALECT, MINIMAX_DIALECT)`.
  - `scan_text_tool_calls(text: str) -> tuple[str, list[ParsedToolCall]] | None` — first dialect whose `detect` matches returns `(dialect.name, dialect.parse(text))`; no match returns `None`. `(name, [])` means "tag present but zero parsed".
  - `_parse_wrapped(text) -> list[ParsedToolCall]`, `_parse_minimax(text) -> list[ParsedToolCall]`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/unit/test_text_calls.py`. First add `scan_text_tool_calls`, `DIALECTS`, `TextToolCallDialect` to the existing import block at the top of the file, then append these tests:

```python
# ── scan_text_tool_calls (dialect entry point) ─────────────────────────────────


def test_scan_wrapped_json_tool_call():
    name, calls = scan_text_tool_calls(
        'ok<tool_call>{"name": "read", "arguments": {"path": "/a"}}</tool_call>'
    )
    assert name == "wrapped"
    assert calls[0].name == "read"
    assert calls[0].arguments == {"path": "/a"}


def test_scan_tool_code_json_tool_and_args_keys():
    # <tool_code> uses {tool, args} instead of {name, arguments}.
    name, calls = scan_text_tool_calls(
        'ok<tool_code>{"tool": "read", "args": {"path": "/a"}}</tool_code>'
    )
    assert name == "wrapped"
    assert calls[0].name == "read"
    assert calls[0].arguments == {"path": "/a"}


def test_scan_tool_code_empty_args_kept():
    # args == {} is valid and must not be dropped by a truthiness check.
    _, calls = scan_text_tool_calls('<tool_code>{"tool": "ping", "args": {}}</tool_code>')
    assert calls[0].name == "ping"
    assert calls[0].arguments == {}


def test_scan_tool_code_xml_fallback_equivalent():
    # <tool_code> supports the same XML degradation path as <tool_call>.
    text = (
        "<tool_code><function=write>"
        "<parameter=path>/tmp/a</parameter>"
        "<parameter=content>hello</parameter>"
        "</function></tool_code>"
    )
    _, calls = scan_text_tool_calls(text)
    assert calls[0].name == "write"
    assert calls[0].arguments == {"path": "/tmp/a", "content": "hello"}


def test_scan_mixed_tool_call_and_tool_code_blocks():
    text = (
        '<tool_call>{"name": "a", "arguments": {}}</tool_call>'
        '<tool_code>{"tool": "b", "args": {}}</tool_code>'
    )
    _, calls = scan_text_tool_calls(text)
    assert [c.name for c in calls] == ["a", "b"]


def test_scan_minimax_dialect():
    name, calls = scan_text_tool_calls(_MINIMAX)
    assert name == "minimax"
    assert calls[0].name == "control__delegate_task"


def test_scan_no_tag_returns_none():
    assert scan_text_tool_calls("just a plain answer") is None


def test_scan_tag_present_but_zero_parsed():
    # Malformed content: dialect detects, parse yields nothing → (name, []).
    name, calls = scan_text_tool_calls("<tool_call>not json and not xml</tool_call>")
    assert name == "wrapped"
    assert calls == []


def test_clean_visible_cuts_tool_code():
    assert clean_visible("答案是\n<tool_code>{...}") == "答案是\n"
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `pytest tests/unit/test_text_calls.py -q`
Expected: FAIL — `ImportError: cannot import name 'scan_text_tool_calls'` (collection error).

- [ ] **Step 3: Update the shared JSON parser to accept both key sets**

In `src/ctx_weft/providers/llm/text_calls.py`, replace the `if isinstance(data, dict):` block inside `_parse_single_tool_call` (currently lines ~163–176) with:

```python
    if isinstance(data, dict):
        # <tool_call> uses name/arguments; <tool_code> uses tool/args. Accept both.
        name = data.get("name") or data.get("tool") or ""
        if not name:
            logger.warning("Text tool call missing 'name'/'tool': %.200s", stripped)
            return None
        if "arguments" in data:
            arguments = data["arguments"]
        elif "args" in data:
            arguments = data["args"]
        else:
            arguments = {}
        if isinstance(arguments, str):
            try:
                arguments = json.loads(arguments)
            except (json.JSONDecodeError, TypeError):
                arguments = {}
        if not isinstance(arguments, dict):
            arguments = {}
        return ParsedToolCall(name, arguments, json.dumps(arguments, ensure_ascii=False))
```

- [ ] **Step 4: Add the dialect layer**

Add `from collections.abc import Callable` to the imports near the top of the file (after `import re`).

Then, immediately **after** the `_MINIMAX_PARAM_RE` regex definition (currently ~line 89) and **before** `@dataclass class ParsedToolCall`, insert the wrapped-block regex:

```python
# <tool_code> 与 <tool_call> 完全对等（同一「wrapped」方言的两套 JSON 键别名）。
# 反向引用保证开闭标签配对，不会 <tool_call> 开、</tool_code> 闭混匹配。
_WRAPPED_BLOCK_RE = re.compile(r"<(tool_call|tool_code)>\s*(.*?)\s*</\1>", re.DOTALL)
```

Then, **after** `_parse_single_tool_call` (currently ends ~line 181) and **before** the old `def parse_tool_calls_from_text`, insert the dialect definitions:

```python
def _parse_wrapped(text: str) -> list[ParsedToolCall]:
    """抽取所有 <tool_call>/<tool_code> 块，每块按 JSON → 严格 XML → 宽松 XML 解析。"""
    calls: list[ParsedToolCall] = []
    for m in _WRAPPED_BLOCK_RE.finditer(text):
        parsed = _parse_single_tool_call(m.group(2))
        if parsed is not None:
            calls.append(parsed)
    return calls


def _parse_minimax(text: str) -> list[ParsedToolCall]:
    """抽取 <minimax:tool_call> 块里的所有 <invoke>（见 parse_minimax_tool_calls）。"""
    return parse_minimax_tool_calls(text)


@dataclass(frozen=True)
class TextToolCallDialect:
    """一种「工具调用写进正文文本」的方言：起始标签集 + 解析器。

    ``open_markers`` 一处三用：detect（是否命中本方言）、可见门 ``_VISIBLE_MARKERS``
    （从正文里扣掉标签）、以及块正则的锚点。加新方言只需往 ``DIALECTS`` 加一项。
    """

    name: str
    open_markers: tuple[str, ...]
    parse: Callable[[str], list[ParsedToolCall]]

    def detect(self, text: str) -> bool:
        return any(m in text for m in self.open_markers)


WRAPPED_DIALECT = TextToolCallDialect(
    "wrapped", ("<tool_call>", "<tool_code>", "<function="), _parse_wrapped
)
MINIMAX_DIALECT = TextToolCallDialect(
    "minimax", ("<minimax:tool_call>",), _parse_minimax
)
# 顺序即 _finalize 的检测优先级：wrapped 先于 minimax。
DIALECTS: tuple[TextToolCallDialect, ...] = (WRAPPED_DIALECT, MINIMAX_DIALECT)


def scan_text_tool_calls(text: str) -> tuple[str, list[ParsedToolCall]] | None:
    """首个 detect 命中的方言 → ``(name, calls)``；都不命中 → ``None``。

    ``(name, [])`` 表示「标签在但零解析」（截断/畸形）——供 finalize 判 outage 退避自愈。
    """
    for dialect in DIALECTS:
        if dialect.detect(text):
            return dialect.name, dialect.parse(text)
    return None
```

Note: `_parse_minimax` forward-references `parse_minimax_tool_calls`, which is defined later in the file. That is fine — `_parse_minimax`'s body runs only at call time, and `DIALECTS` stores the `_parse_minimax` function object, not its result.

- [ ] **Step 5: Run the new tests to verify they pass**

Run: `pytest tests/unit/test_text_calls.py -q`
Expected: PASS (all existing + 9 new tests green).

- [ ] **Step 6: Run the full suite to confirm nothing else broke**

Run: `pytest tests/unit/test_finalize.py tests/unit/test_text_calls.py -q`
Expected: PASS (old API still present, so `_finalize.py` untouched and green).

- [ ] **Step 7: Commit**

```bash
git add src/ctx_weft/providers/llm/text_calls.py tests/unit/test_text_calls.py
git commit -m "feat(llm): text_calls 方言抽象 + <tool_code> 支持（additive）

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 2: Migrate `_finalize.py` to `scan_text_tool_calls`

Collapse the triplicated `elif contains_X: parse_X → raise outage` chain into one loop. `tests/unit/test_finalize.py` is the regression guard (it exercises `<tool_call>`, `<minimax:tool_call>`, truncated, malformed via `content_text` — behavior-level, not the removed names).

**Files:**
- Modify: `src/ctx_weft/providers/llm/_finalize.py`
- Test (regression, unchanged): `tests/unit/test_finalize.py`

**Interfaces:**
- Consumes: `scan_text_tool_calls` from Task 1.
- Produces: no new public surface; `build_finalize_chunks` signature unchanged.

- [ ] **Step 1: Add a `<tool_code>` recovery test to lock the new path**

Append to `tests/unit/test_finalize.py`:

```python
def test_text_embedded_tool_code_is_recovered():
    # <tool_code> {tool, args} recovered into a tool_call chunk, same as <tool_call>.
    text = 'ok<tool_code>{"tool": "write", "args": {"p": "/a"}}</tool_code>'
    chunks = build_finalize_chunks(
        content_text=text,
        native_tool_calls=[],
        had_native_buffer=False,
        saw_terminal=True,
        usage=None,
        finish_reason="stop",
    )
    assert _kinds(chunks) == ["tool_call", "done"]
    assert chunks[0].tool_call.name == "write"
    assert chunks[0].tool_call.arguments == {"p": "/a"}
```

(If the existing `build_finalize_chunks(...)` calls in this file use different keyword names/order, copy them from `test_text_embedded_tool_call_is_recovered` in the same file rather than the snippet above.)

- [ ] **Step 2: Run the new test to verify it fails**

Run: `pytest tests/unit/test_finalize.py::test_text_embedded_tool_code_is_recovered -q`
Expected: FAIL — `<tool_code>` currently falls through (`contains_tool_call_tag` only checks `<tool_call>`/`<function=`), so no `tool_call` chunk is produced (kinds == `["token", "done"]` or `["done"]`).

- [ ] **Step 3: Swap the imports**

In `src/ctx_weft/providers/llm/_finalize.py`, replace the `text_calls` import block (currently lines ~17–25):

```python
from ctx_weft.providers.llm.text_calls import (
    clean_visible,
    extract_think,
    scan_text_tool_calls,
    unwrap_raw_arguments,
)
```

- [ ] **Step 4: Replace the elif chain with the dialect loop**

Replace the whole `if native_tool_calls: ... elif contains_minimax_tool_call(...): ...` block (currently lines ~90–135) with:

```python
    if native_tool_calls:
        out.extend(LLMChunk(kind="tool_call", tool_call=_unwrap_tc(tc)) for tc in native_tool_calls)
    else:
        # 正文里内联的文本 tool call（wrapped <tool_call>/<tool_code>/<function=>，或
        # <minimax:tool_call>）→ 收尾还原成规整 tool call。首个命中的方言负责解析。
        scan = scan_text_tool_calls(content_text)
        if scan is not None:
            dialect_name, parsed = scan
            # D2：标签出现但一个都没解析出来（截断的未闭合标签 / 畸形 JSON/XML / 缺 name）。
            # 若放行，上层会把这轮当「纯文本让位用户」误暂停、工具动作被静默吞掉、UI 卡 llm_pending。
            # 标 outage=True 走进程内退避重抽（格式抖动/流截断通常一两次即恢复），预算耗尽再转
            # LLMOutageError → session 可恢复 INTERRUPTED（靠 /resume 重驱动），而非整任务硬 FAILED。
            if not parsed:
                raise LLMCallError(
                    f"LLM emitted a {dialect_name} tool call tag that parsed to zero "
                    "tool calls (truncated or malformed text tool call)",
                    retriable=True,
                    outage=True,
                )
            out.extend(
                LLMChunk(
                    kind="tool_call",
                    tool_call=ToolCall(
                        id=generate_id("call"), name=p.name,
                        arguments=unwrap_raw_arguments(p.arguments),
                    ),
                )
                for p in parsed
            )
```

- [ ] **Step 5: Run the finalize suite to verify all pass**

Run: `pytest tests/unit/test_finalize.py tests/unit/test_adapter_outage_tagging.py tests/unit/test_llm_self_heal.py -q`
Expected: PASS — including the new `<tool_code>` test and the unchanged `<tool_call>`/minimax/truncated/malformed regression tests.

- [ ] **Step 6: Commit**

```bash
git add src/ctx_weft/providers/llm/_finalize.py tests/unit/test_finalize.py
git commit -m "refactor(llm): _finalize 三段 elif 链塌缩为 scan_text_tool_calls 循环

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 3: Delete `TextScan` + old `contains_*`/`parse_*` API (contract)

Remove the now-dead code. Nothing outside `text_calls.py` imports these anymore (Task 2 was the last consumer; adapters only use `ContentGate`/`merge_content`).

**Files:**
- Modify: `src/ctx_weft/providers/llm/text_calls.py`
- Test: `tests/unit/test_text_calls.py`

**Interfaces:**
- Removes: `TextScan`, `contains_tool_call_tag`, `contains_minimax_tool_call`, `parse_tool_calls_from_text`, `parse_minimax_tool_calls`, plus the `_TOOL_CALL_RE`, `TOOL_CALL_START`, `MINIMAX_TOOL_CALL_START`, `_MINIMAX_BLOCK_RE`... **only if unused after inlining** (see Step 2 caution).
- Keeps: everything else, including `scan_text_tool_calls`, `_parse_wrapped`, `_parse_minimax`.

- [ ] **Step 1: Inline the minimax parser and delete old public functions**

In `src/ctx_weft/providers/llm/text_calls.py`:

1. Move the body of `parse_minimax_tool_calls` into `_parse_minimax` (so `_parse_minimax` no longer forward-references a to-be-deleted function). Replace the current `_parse_minimax` stub and the `contains_minimax_tool_call`/`parse_minimax_tool_calls` functions with a single `_parse_minimax`:

```python
def _parse_minimax(text: str) -> list[ParsedToolCall]:
    """抽取 <minimax:tool_call> 块里的所有 <invoke>（每个含若干 <parameter>）。

    支持一个块内多个 invoke、多个块；块未闭合（流式截断）时取起始标签之后的内容兜底。
    """
    blocks = _MINIMAX_BLOCK_RE.findall(text)
    if not blocks:
        start = text.find(MINIMAX_TOOL_CALL_START)
        if start == -1:
            return []
        blocks = [text[start + len(MINIMAX_TOOL_CALL_START):]]

    calls: list[ParsedToolCall] = []
    for body in blocks:
        for inv in _MINIMAX_INVOKE_RE.finditer(body):
            name = inv.group(1).strip()
            if not name:
                continue
            arguments = {
                p.group(1).strip(): p.group(2).strip()
                for p in _MINIMAX_PARAM_RE.finditer(inv.group(2))
            }
            calls.append(ParsedToolCall(name, arguments, json.dumps(arguments, ensure_ascii=False)))
    if not calls:
        logger.warning("Found <minimax:tool_call> but parsed no invoke: %.200s", text)
    return calls
```

Keep `MINIMAX_TOOL_CALL_START`, `_MINIMAX_BLOCK_RE`, `_MINIMAX_INVOKE_RE`, `_MINIMAX_PARAM_RE` (still used above).

2. Delete `class TextScan` (lines ~101–107).
3. Delete `def contains_tool_call_tag` (lines ~110–112).
4. Delete `def parse_tool_calls_from_text` (lines ~184–206).
5. Delete `def contains_minimax_tool_call` (lines ~209–211).
6. Delete `TOOL_CALL_START = "<tool_call>"` and `_TOOL_CALL_RE` (now unused — `_parse_wrapped` uses `_WRAPPED_BLOCK_RE`).

- [ ] **Step 2: Verify `TOOL_CALL_START`/`_TOOL_CALL_RE` are truly unused before deleting**

Run: `grep -rn "TOOL_CALL_START\|_TOOL_CALL_RE\|parse_tool_calls_from_text\|contains_tool_call_tag\|contains_minimax_tool_call\|parse_minimax_tool_calls\|TextScan" src tests`
Expected: after edits, **zero** matches in `src/` outside the deletions themselves, and only the to-be-removed old tests in `tests/`. If `TOOL_CALL_START` still appears (e.g. some spot referenced it), keep that constant instead of deleting it.

- [ ] **Step 3: Remove the old-API tests**

In `tests/unit/test_text_calls.py`:

1. Trim the top import block to only symbols that still exist:

```python
from ctx_weft.providers.llm.text_calls import (
    ContentGate,
    DIALECTS,
    TextToolCallDialect,
    clean_visible,
    extract_think,
    merge_content,
    scan_text_tool_calls,
    unwrap_raw_arguments,
)
```

2. Delete these now-obsolete tests (they call removed functions):
   `test_contains_tool_call_tag_detects_tool_call`, `test_parse_json_tool_call`,
   `test_parse_json_tool_call_arguments_as_string`, `test_parse_strict_xml_tool_call`,
   `test_parse_lenient_xml_no_closing_tags`, `test_parse_multiple_tool_calls`,
   `test_unclosed_tool_call_tag_marks_open`, `test_no_tool_call_returns_text_as_before`,
   `test_contains_minimax_tool_call`, `test_parse_minimax_tool_call`,
   `test_parse_minimax_multiple_invokes`, `test_parse_minimax_unclosed_block_lenient`.

3. Re-add coverage for the two behaviors those deleted tests uniquely guarded, now via `scan_text_tool_calls`:

```python
def test_scan_json_arguments_as_string():
    # Some models put a JSON string in "arguments".
    _, calls = scan_text_tool_calls('<tool_call>{"name": "x", "arguments": "{\\"k\\": 1}"}</tool_call>')
    assert calls[0].arguments == {"k": 1}


def test_scan_lenient_xml_no_closing_tags():
    text = (
        "<tool_call><function=write>"
        "<parameter=path>/tmp/a"
        "<parameter=content>hello"
        "</tool_call>"
    )
    _, calls = scan_text_tool_calls(text)
    assert calls[0].name == "write"
    assert calls[0].arguments == {"path": "/tmp/a", "content": "hello"}


def test_scan_minimax_unclosed_block_lenient():
    # 流式截断：缺 </invoke> / </minimax:tool_call> 也要能解析出来
    name, calls = scan_text_tool_calls(
        '<minimax:tool_call><invoke name="f"><parameter name="p">v</parameter>'
    )
    assert name == "minimax"
    assert calls[0].name == "f"
    assert calls[0].arguments == {"p": "v"}
```

Keep the `clean_visible`, `ContentGate`, `merge_content`, `unwrap_raw_arguments`, `extract_think`, and `_MINIMAX`-based tests already present (note `test_parse_minimax_unclosed_block_lenient` — the *old* one — is in the delete list; the `clean_visible` asserts embedded in it, lines 202/205, must be preserved as their own tests if not already standalone — verify and re-home any orphaned asserts).

- [ ] **Step 4: Run the text_calls suite**

Run: `pytest tests/unit/test_text_calls.py -q`
Expected: PASS.

- [ ] **Step 5: Run the whole unit suite to confirm no dangling imports**

Run: `pytest tests/unit -q`
Expected: PASS — no `ImportError`/`AttributeError` from removed symbols.

- [ ] **Step 6: Lint**

Run: `ruff check src/ctx_weft/providers/llm/text_calls.py src/ctx_weft/providers/llm/_finalize.py`
Expected: no errors (no unused imports/names left behind).

- [ ] **Step 7: Commit**

```bash
git add src/ctx_weft/providers/llm/text_calls.py tests/unit/test_text_calls.py
git commit -m "refactor(llm): 删除 TextScan 及旧 contains_*/parse_* 死代码 API

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Self-Review

**Spec coverage:**
- 单文件 4 段重组 → Task 1 adds dialect section; Tasks 1+3 leave `merge_content`/`clean_visible`/`ContentGate`/`extract_think`/`_raw` sections intact and concern-separated. ✓
- 方言抽象 `(name, open_markers, parse)` + `detect`/`_VISIBLE_MARKERS` 派生 → Task 1. Note: `_VISIBLE_MARKERS` is already `(THINK_START, TOOL_CALL_START, "<function=", MINIMAX_TOOL_CALL_START)`; **derive it from DIALECTS in Task 1 Step 4** — see gap fix below. ✓ (with fix)
- `<tool_code>` = `<tool_call>` 对等（JSON tool/args + XML 退化）→ Task 1 Steps 3–4, tests Step 1. ✓
- `_finalize` elif 链塌缩 → Task 2. ✓
- 删 `TextScan` + 旧签名，测试改用 `scan_text_tool_calls` → Task 3. ✓

**Gap found & fixed:** The plan's Task 1 Step 4 adds `DIALECTS` but did not restate the `_VISIBLE_MARKERS` derivation the spec requires. Add this to **Task 1, Step 4** (after the `DIALECTS`/`scan_text_tool_calls` block, and delete the old `_VISIBLE_MARKERS` line ~242):

```python
# 可见门标记从方言表派生：<think> + 所有方言的起始标签。加方言即自动进门，
# 无需再手动同步这一处（根除「解析器认得、可见门不认、标签泄漏进正文」的 bug 类）。
_VISIBLE_MARKERS = (THINK_START, *(m for d in DIALECTS for m in d.open_markers))
```

Because `_VISIBLE_MARKERS` must be defined before `clean_visible` uses it, place this derivation right after `DIALECTS`/`scan_text_tool_calls` and **remove the original literal `_VISIBLE_MARKERS` assignment** (currently ~line 242). `clean_visible`/`ContentGate` bodies stay unchanged. This makes `<tool_code>` visible-gating (`test_clean_visible_cuts_tool_code` in Step 1) pass.

**Placeholder scan:** No TBD/TODO; every code step shows full code. ✓

**Type consistency:** `scan_text_tool_calls -> tuple[str, list[ParsedToolCall]] | None` used identically in Task 1 tests, Task 2 `_finalize`, Task 3 tests. `TextToolCallDialect` fields (`name`, `open_markers`, `parse`) + `detect` consistent across tasks. `_parse_wrapped`/`_parse_minimax` names consistent. ✓
