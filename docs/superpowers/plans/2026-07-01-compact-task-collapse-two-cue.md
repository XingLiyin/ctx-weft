# Compact 拆分：task 坍缩 + 两 cue（task/agent）Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让 compact 步骤的两个职责各自独立——task compact 改为把当前 task 层早期回合坍缩成一条含「原始消息 + 执行摘要」两节的 USER_PROMPT；agent compact 用独立 cue 生成派发经验摘要；两者共用中性 COMPACT.md 人格、仅 cue 不同。

**Architecture:** 纯 core 改动（`compact.py` + `composer.py` + `COMPACT.md`），不动 provider 协议/postgres/DB。task 坍缩用现有 `recall_recent + supersede + ingest` 三原语实现。`apply_compact` 保持不变，继续服务 observe(max_turns) 与 background_observe 两条「observer 形成胶囊」路径。cue 通过 `ContextRequest.extra["compact_scope"]` 选择，IdentitySource 与 `Purpose` 类型不动。

**Tech Stack:** Python 3.11 / async / pytest（`uv run pytest`，pyproject 已配 `pythonpath=["."]`）。in_memory blackboard provider 作单测后端。

## Global Constraints

- 测试运行器：`cd ctx-weft && uv run pytest ...`（不要用裸 `pytest`；`VIRTUAL_ENV` 警告可忽略）。
- 不改 `apply_compact`（provider 协议、in_memory、postgres 均不动）。
- 不改 observe 的 `_maybe_compact_task`、`background_observe`——它们是 observer 形成胶囊，保持写 assistant `## Progress So Far`。
- `Purpose` 类型、`IdentitySource` 不动；cue 选择只走 `ContextRequest.extra["compact_scope"]`。
- ROLE.md 的 act_recap 逐段契约不动（observer 专用），不要把它挪进 COMPACT.md。
- 提交信息结尾附：`Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>`。

---

## 设计决策（评审时可否决）

- **D1**：task 坍缩是纯 core 函数（`collapse_task_layer`），用 `recall_recent/supersede/ingest`；无 provider/DB 改动。
- **D2**：坍缩产物是一条 **`USER_PROMPT` 事件**（role=user，scope=当前 task），内容 = `原始消息` + 分隔标记 + `执行摘要`。这样 composer 的 `_current_task_user_index`（找 `mtype=="user_prompt"` + task_id）仍能定位它、照贴 `## Current Task/## Current Message` 框。
- **D3**：坍缩**不保留**旧的原始 USER_PROMPT（连同 observer 的 `## Progress So Far` 一起 supersede），改由新 USER_PROMPT 的「原始消息」节承载原文。
- **D4**：task cue 是**整体式**（覆盖整段 task-so-far，因为它替掉原始 prompt + 之前所有 `## Progress So Far`）。这**部分回退**了本仓早前把 `_COMPACTION_INSTRUCTION` 对齐 act_recap（逐段）的改动——逐段契约留在 ROLE.md 给 observer 用。
- **D5**：再坍缩用分隔标记 `COLLAPSE_DELIM` 从上一条坍缩 USER_PROMPT 里切出「原始消息」节，保持该节有界、不随次数膨胀。
- **D6**：`_compact_scope` 按需产两份摘要——`fold_task` 时产 task 摘要、`fold_root` 时产 agent 摘要；两层都折时 = 2 次 LLM 调用（可接受，已确认）。
- **D7**：COMPACT.md 退回中性人格（「你是压缩者，只输出摘要文本、无工具」）；task/agent 差异全在 cue。
- **D8**：task 坍缩与 agent 折叠用**不同阈值**——新增 `LoopConfig.collapse_keep_last`（task 坍缩保留最近 N 条回合，默认 **3**）；`compact_keep_last`（默认 6）回归注释本义、仅供 agent 折叠算胶囊数（`fold_root`）。observe 的 `_maybe_compact_task`（观察者形成胶囊、非 task 坍缩）仍沿用 `compact_keep_last`，不改（D2）。若日后想让 observe 也走独立 task-turn 阈值，属后续项，本计划不含。

---

## File Structure

- `src/ctx_weft/core/assembler/composer.py` — 两条 cue 常量 + compact 分支按 `extra["compact_scope"]` 选 cue。
- `resources/agents/default/COMPACT.md` — 中性压缩者人格。
- `src/ctx_weft/core/loop/steps/compact.py` — `summarize_for_compact` 加 `scope` 参数；新增 `collapse_task_layer`；`_compact_scope` 改用坍缩 + 独立 agent 摘要 + 双阈值。
- `src/ctx_weft/protocols/template.py`（`LoopConfig`）+ `src/ipmastercowork/providers/templates/loader.py`（`_parse_loop_config`）— 新增 `collapse_keep_last`（task 坍缩阈值），与 agent 折叠的 `compact_keep_last` 分离。
- 测试：`tests/unit/test_task_collapse.py`（新）、`test_compact_cue_scope.py`（新）、既有 `test_composer_compact_metadata.py` / `test_step_prompt_consistency.py` 保持绿。

---

### Task 1: 中性人格 + 两 cue + cue 选择

**Files:**
- Modify: `src/ctx_weft/core/assembler/composer.py`（cue 常量约 83-88；compact 分支约 244-247）
- Modify: `resources/agents/default/COMPACT.md`
- Test: `tests/unit/test_compact_cue_scope.py`（新）
- Test（回归）: `tests/unit/test_composer_compact_metadata.py`、`test_step_prompt_consistency.py`

**Interfaces:**
- Produces: 模块常量 `_COMPACTION_INSTRUCTION`（task，整体式）、`_AGENT_COMPACTION_INSTRUCTION`（agent）；compact 分支据 `request.extra.get("compact_scope")` 选 cue（默认 `"task"`）。两条 cue 均含稳定子串 `"act as a memory compactor"`。

- [ ] **Step 1: 写失败测试**

新建 `tests/unit/test_compact_cue_scope.py`：

```python
from types import SimpleNamespace

import pytest

from ctx_weft.core.assembler.composer import (
    DefaultComposer, _AGENT_COMPACTION_INSTRUCTION, _COMPACTION_INSTRUCTION,
)
from ctx_weft.core.assembler.assembler import ContextBlock
from ctx_weft.core.utils import content_to_text

pytestmark = pytest.mark.asyncio


def _identity(text: str) -> ContextBlock:
    return ContextBlock(id="id", source="identity", kind="identity", target="system",
                        content=text, priority=0, token_estimate=1, metadata={})


def _user(text: str) -> ContextBlock:
    return ContextBlock(id="u", source="task_conversation", kind="history", target="messages",
                        content=text, priority=3, token_estimate=1,
                        metadata={"role": "user", "type": "user_prompt", "timestamp": "1",
                                  "task_id": "t1"})


def _req(scope: str | None):
    task = SimpleNamespace(id="t1", title="T", description="D", user_prompt="do X",
                           user_prompt_in_memory=True, process_report=None, outputs=None)
    extra = {"compact_scope": scope} if scope is not None else {}
    tmpl = SimpleNamespace(identity={"act": SimpleNamespace(text="SOUL", style=None),
                                     "compact": SimpleNamespace(text="COMPACTOR", style=None)})
    return SimpleNamespace(purpose="compact", task=task, template=tmpl, extra=extra)


async def _last_user(scope):
    prompt = await DefaultComposer().compose([_identity("COMPACTOR"), _user("do X")], _req(scope))
    return content_to_text([m for m in prompt.messages if m.role == "user"][-1].content)


async def test_task_scope_uses_task_cue():
    text = await _last_user("task")
    assert _COMPACTION_INSTRUCTION in text
    assert _AGENT_COMPACTION_INSTRUCTION not in text


async def test_agent_scope_uses_agent_cue():
    text = await _last_user("agent")
    assert _AGENT_COMPACTION_INSTRUCTION in text
    assert _COMPACTION_INSTRUCTION not in text


async def test_default_scope_is_task():
    text = await _last_user(None)
    assert _COMPACTION_INSTRUCTION in text
```

- [ ] **Step 2: 运行确认失败**

Run: `cd ctx-weft && uv run pytest tests/unit/test_compact_cue_scope.py -q`
Expected: FAIL（`_AGENT_COMPACTION_INSTRUCTION` ImportError / agent 分支未选）

- [ ] **Step 3: 改 composer 的 cue 常量与 compact 分支**

把 `_COMPACTION_INSTRUCTION` 改回整体式，并新增 agent cue（`src/ctx_weft/core/assembler/composer.py`，替换现有 `_COMPACTION_INSTRUCTION` 定义块）：

```python
# task compact cue：整体式——坍缩会替掉原始 prompt + 之前所有 `## Progress So Far`，故须概括
# 整段 task-so-far（不是逐段 recap；逐段 act_recap 契约在 ROLE.md，属 observer）。
_COMPACTION_INSTRUCTION = (
    "Now act as a memory compactor. Summarize the ENTIRE task execution so far — from the "
    "user's original request through everything done since — into one concise progress digest "
    "a future turn can continue from. Preserve: the task goal, key facts discovered, decisions "
    "made, important tool results, current state, and any unfinished threads. This replaces the "
    "earlier turns, so fold in whatever matters. Output only the digest text, no preamble."
)

# agent compact cue：概括本 agent 的派发历史（每个子任务做了什么、结果/关键产出/教训），
# 忽略当前 task 自身的执行细节，只压派发记录。
_AGENT_COMPACTION_INSTRUCTION = (
    "Now act as a memory compactor for this agent's delegation history. Summarize the dispatched "
    "sub-tasks so far — for each: what it was asked to do and its outcome / key results / lessons "
    "— into one concise digest the agent can rely on later. Ignore the current task's own "
    "execution detail; focus on the delegation record. Output only the digest text, no preamble."
)
```

再改 `compose` 的 compact 分支（`else:  # compact` 那段）：

```python
        else:  # compact
            system = self._build_act_system(blocks, request)
            cue = (_AGENT_COMPACTION_INSTRUCTION
                   if request.extra.get("compact_scope") == "agent"
                   else _COMPACTION_INSTRUCTION)
            messages = self._build_facet_trailing_messages(blocks, request, cue)
            tools = []
```

- [ ] **Step 4: COMPACT.md 退回中性**

覆盖 `resources/agents/default/COMPACT.md`：

```markdown
你是记忆压缩者。你的任务是把一段较长的对话/经验压缩成一份简洁摘要，供后续继续工作时参考。

你没有任何可用工具，也不要尝试调用工具；只输出摘要文本本身，不要前言、不要解释。

末尾的提示会告诉你这次要压缩的范围与侧重。在该范围内做到：高信噪、基于证据、不臆测、不堆砌无关篇幅；区分「实际完成」与「仅尝试」，保留后续继续工作真正需要的信息。
```

- [ ] **Step 5: 运行新测试 + 回归**

Run: `cd ctx-weft && uv run pytest tests/unit/test_compact_cue_scope.py tests/unit/test_composer_compact_metadata.py tests/unit/test_step_prompt_consistency.py -q`
Expected: PASS（`test_composer_compact_metadata.py:45` 已断言 `"act as a memory compactor"`，两条 cue 均含此子串，保持绿）

- [ ] **Step 6: Commit**

```bash
git add src/ctx_weft/core/assembler/composer.py resources/agents/default/COMPACT.md tests/unit/test_compact_cue_scope.py
git commit -m "feat(compact): 中性压缩人格 + task/agent 两条 cue 按 scope 选择"
```

---

### Task 2: `summarize_for_compact` 加 `scope` 参数

**Files:**
- Modify: `src/ctx_weft/core/loop/steps/compact.py`（`summarize_for_compact`，约 42-71）
- Test: `tests/unit/test_compact_cue_scope.py`（追加）

**Interfaces:**
- Produces: `summarize_for_compact(state, ctx, *, scope: str = "task") -> str`，将 `scope` 透传为 `ContextRequest.extra["compact_scope"]`。默认 `"task"`（observe 兜底与既有调用行为不变）。

- [ ] **Step 1: 写失败测试（追加到 test_compact_cue_scope.py）**

```python
async def test_summarize_for_compact_threads_scope(monkeypatch):
    """summarize_for_compact 把 scope 透传成 extra['compact_scope']。"""
    from ctx_weft.core.loop.steps import compact as compact_mod

    seen = {}

    class _FakeAssembler:
        async def assemble(self, request):
            seen["compact_scope"] = request.extra.get("compact_scope")
            return SimpleNamespace(system="", messages=[])

    async def _fake_stream(ctx, state, req):
        if False:
            yield None  # 空流
        return

    agent = SimpleNamespace(runtime={}, )
    state = SimpleNamespace(
        agent=agent, scope=SimpleNamespace(), task=SimpleNamespace(), session=SimpleNamespace(),
        extra={})
    ctx = SimpleNamespace(assembler=_FakeAssembler())
    monkeypatch.setattr(compact_mod, "stream_llm_resilient", _fake_stream)

    await compact_mod.summarize_for_compact(state, ctx, scope="agent")
    assert seen["compact_scope"] == "agent"
```

- [ ] **Step 2: 运行确认失败**

Run: `cd ctx-weft && uv run pytest tests/unit/test_compact_cue_scope.py::test_summarize_for_compact_threads_scope -q`
Expected: FAIL（`summarize_for_compact` 尚无 `scope` 关键字参数 → TypeError）

- [ ] **Step 3: 加 scope 参数**

改 `src/ctx_weft/core/loop/steps/compact.py` 的 `summarize_for_compact` 签名与 `ContextRequest` 构造：

```python
async def summarize_for_compact(
    state: LoopState, ctx: LoopContext, *, scope: str = "task"
) -> str:
    """装配 purpose="compact" 上下文 + 一次 LLM 摘要，返回摘要文本。

    scope 选 cue（composer 据 extra["compact_scope"] 分流）："task"=整段执行摘要（默认，
    observe 兜底与坍缩共用）；"agent"=派发经验摘要。
    ...（原 docstring 其余保留）...
    """
    agent = state.agent
    request = ContextRequest(
        purpose="compact",
        scope=state.scope,
        task=state.task,
        agent=agent,
        session=state.session,
        template=state.extra.get("template"),
        bound_capabilities=state.extra.get("bound_capabilities", []),
        extra={"compact_scope": scope},
    )
    # ...（其余 body 不变）...
```

- [ ] **Step 4: 运行确认通过**

Run: `cd ctx-weft && uv run pytest tests/unit/test_compact_cue_scope.py -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/ctx_weft/core/loop/steps/compact.py tests/unit/test_compact_cue_scope.py
git commit -m "feat(compact): summarize_for_compact 加 scope 参数选 task/agent cue"
```

---

### Task 3: `collapse_task_layer` 坍缩函数（含再坍缩分隔）

**Files:**
- Modify: `src/ctx_weft/core/loop/steps/compact.py`（新增函数 + 常量 + `content_to_text` 导入）
- Test: `tests/unit/test_task_collapse.py`（新）

**Interfaces:**
- Produces:
  - `COLLAPSE_DELIM: str` — 两节分隔标记。
  - `collapse_task_layer(state, ctx, keep_last: int, summary_text: str) -> int` — supersede 当前 task 层超过 `keep_last` 的早期回合（含原始 USER_PROMPT），ingest 一条新 `USER_PROMPT`（content = 原始消息 + `COLLAPSE_DELIM` + summary，role=user，metadata `{"collapsed": True}`），保留最近 `keep_last` 条。返回 supersede 条数（≤keep_last 时返回 0、不折）。
- Consumes: `ctx.memory.recall_recent / supersede / ingest`（现有协议）。

- [ ] **Step 1: 写失败测试**

新建 `tests/unit/test_task_collapse.py`：

```python
from datetime import datetime, timedelta, UTC
from types import SimpleNamespace

import pytest

from ctx_weft.core.loop.steps.compact import COLLAPSE_DELIM, collapse_task_layer
from ctx_weft.providers.memory_blackboard.in_memory import InMemoryBlackboard
from ctx_weft.protocols import (
    MemoryEvent, MemoryEventType as T, MemoryScope, ProviderContext,
)

pytestmark = pytest.mark.asyncio

_BASE = datetime(2026, 7, 1, tzinfo=UTC)


def _ctx():
    return ProviderContext(tenant_id="tn")


def _state(mem, scope):
    return SimpleNamespace(
        scope=scope, task=SimpleNamespace(id="t1"),
        agent=SimpleNamespace(), session=SimpleNamespace())


async def _ingest(mem, scope, typ, content, i, role="user"):
    await mem.ingest(MemoryEvent(type=typ, scope=scope, content=content,
                                 timestamp=_BASE + timedelta(seconds=i), role=role,
                                 metadata={"task_id": scope.task_id}), _ctx())


async def test_collapse_folds_early_keeps_recent():
    mem = InMemoryBlackboard()
    scope = MemoryScope(session_id="s", task_id="t1", agent_id="a")
    await _ingest(mem, scope, T.USER_PROMPT, "原始请求：做 X", 0)
    await _ingest(mem, scope, T.LLM_RESPONSE, "step1", 1, role="assistant")
    await _ingest(mem, scope, T.TOOL_RESULT, "r1", 2, role="tool")
    await _ingest(mem, scope, T.LLM_RESPONSE, "step2", 3, role="assistant")
    await _ingest(mem, scope, T.TOOL_RESULT, "r2", 4, role="tool")

    ctx = SimpleNamespace(memory=mem, provider_ctx=_ctx())
    n = await collapse_task_layer(_state(mem, scope), ctx, keep_last=2, summary_text="做了 step1/step2")

    assert n == 3  # 前 3 条被折
    recs = await mem.recall_recent(scope, [T.USER_PROMPT, T.LLM_RESPONSE, T.TOOL_RESULT], 100, _ctx())
    # newest-first；最老一条应是新坍缩 USER_PROMPT
    by_type = [(r.type, r.content) for r in reversed(recs)]
    assert by_type[0][0] == T.USER_PROMPT
    assert "原始请求：做 X" in by_type[0][1]       # 原始消息节
    assert COLLAPSE_DELIM in by_type[0][1]
    assert "做了 step1/step2" in by_type[0][1]     # 执行摘要节
    # 保留最近 2 条 raw
    assert by_type[1] == (T.LLM_RESPONSE, "step2")
    assert by_type[2] == (T.TOOL_RESULT, "r2")


async def test_collapse_noop_when_within_keep_last():
    mem = InMemoryBlackboard()
    scope = MemoryScope(session_id="s", task_id="t1", agent_id="a")
    await _ingest(mem, scope, T.USER_PROMPT, "orig", 0)
    await _ingest(mem, scope, T.LLM_RESPONSE, "step1", 1, role="assistant")
    ctx = SimpleNamespace(memory=mem, provider_ctx=_ctx())
    n = await collapse_task_layer(_state(mem, scope), ctx, keep_last=5, summary_text="x")
    assert n == 0


async def test_recollapse_keeps_original_bounded():
    mem = InMemoryBlackboard()
    scope = MemoryScope(session_id="s", task_id="t1", agent_id="a")
    # 已坍缩过一次的 USER_PROMPT
    await _ingest(mem, scope, T.USER_PROMPT, f"原始请求：做 X{COLLAPSE_DELIM}旧摘要", 0)
    await _ingest(mem, scope, T.LLM_RESPONSE, "step3", 1, role="assistant")
    await _ingest(mem, scope, T.TOOL_RESULT, "r3", 2, role="tool")
    ctx = SimpleNamespace(memory=mem, provider_ctx=_ctx())
    await collapse_task_layer(_state(mem, scope), ctx, keep_last=1, summary_text="新摘要含 step3")

    recs = await mem.recall_recent(scope, [T.USER_PROMPT], 100, _ctx())
    newest = recs[0].content
    assert newest.count("原始请求：做 X") == 1     # 原始节没有嵌套膨胀
    assert "旧摘要" not in newest                   # 旧摘要节被新摘要替掉
    assert "新摘要含 step3" in newest
```

- [ ] **Step 2: 运行确认失败**

Run: `cd ctx-weft && uv run pytest tests/unit/test_task_collapse.py -q`
Expected: FAIL（`collapse_task_layer` / `COLLAPSE_DELIM` 未定义 → ImportError）

- [ ] **Step 3: 实现 collapse_task_layer**

在 `src/ctx_weft/core/loop/steps/compact.py` 顶部导入补 `content_to_text`：

```python
from ctx_weft.core.utils import content_to_text, now_utc
```

（若已 `from ctx_weft.core.utils import now_utc`，改成上面这行合并导入。）

新增常量与函数（放在 `summarize_for_compact` 之后）：

```python
# 坍缩 USER_PROMPT 的两节分隔标记；再坍缩时据此切出「原始消息」节，保持有界。
COLLAPSE_DELIM = "\n\n---\n## 执行摘要（先前对话已压缩）\n"

# task 层可折类型（当前 task 私有执行对话；TOOL_INVOCATION 仅审计，但一并 supersede）。
_TASK_LAYER_TYPES = [
    MemoryEventType.USER_PROMPT,
    MemoryEventType.LLM_RESPONSE,
    MemoryEventType.TOOL_INVOCATION,
    MemoryEventType.TOOL_RESULT,
    MemoryEventType.TASK_COMPACT_SUMMARY,
]


def _original_section(content: str) -> str:
    """取（可能已坍缩过的）USER_PROMPT 的「原始消息」节：有分隔标记取其前段，否则整体即原文。"""
    idx = content.find(COLLAPSE_DELIM)
    return content[:idx] if idx != -1 else content


async def collapse_task_layer(
    state: LoopState, ctx: LoopContext, keep_last: int, summary_text: str
) -> int:
    """task compact（二级压缩）：把当前 task 层超过 keep_last 的早期回合（含原始 USER_PROMPT
    与 observer 的 `## Progress So Far`）整体坍缩成一条新 USER_PROMPT，content = 原始消息 +
    COLLAPSE_DELIM + 执行摘要；保留最近 keep_last 条 raw。返回 supersede 条数（≤keep_last → 0）。

    坍缩物是 USER_PROMPT 而非 assistant 摘要：composer 据 mtype=="user_prompt"+task_id 定位当前
    task 贴 `## Current Task/## Current Message` 框，故当前运行 task 坍缩后框架不丢；已结束胶囊被
    跨 task 召回时它就是一条背景 message。
    """
    memory = ctx.memory
    recs = await memory.recall_recent(state.scope, _TASK_LAYER_TYPES, 2000, ctx.provider_ctx)
    recs = list(reversed(recs))  # newest-first → chronological
    if len(recs) <= keep_last:
        return 0

    fold = recs if keep_last <= 0 else recs[:-keep_last]
    kept = [] if keep_last <= 0 else recs[-keep_last:]

    # 「原始消息」节 = 折区最早一条 USER_PROMPT 的原文（已坍缩过则取其原始节，保持有界）
    original = ""
    for r in fold:
        if r.type == MemoryEventType.USER_PROMPT:
            text = r.content if isinstance(r.content, str) else content_to_text(r.content)
            original = _original_section(text)
            break

    # 锚：新 USER_PROMPT 须排在所有保留回合之前
    anchor_src = kept[0] if kept else fold[0]
    anchor_ts = anchor_src.timestamp - timedelta(microseconds=1)

    ids = [r.id for r in fold]
    await memory.supersede(ids, ctx.provider_ctx)
    await memory.ingest(
        MemoryEvent(
            type=MemoryEventType.USER_PROMPT,
            scope=state.scope,
            content=f"{original}{COLLAPSE_DELIM}{summary_text or '[Context compacted]'}",
            timestamp=anchor_ts,
            role="user",
            metadata={"task_id": state.scope.task_id, "collapsed": True,
                      "keep_last": keep_last, "folded_count": len(fold)},
        ),
        ctx.provider_ctx,
    )
    return len(ids)
```

- [ ] **Step 4: 运行确认通过**

Run: `cd ctx-weft && uv run pytest tests/unit/test_task_collapse.py -q`
Expected: PASS（3 测试）

- [ ] **Step 5: Commit**

```bash
git add src/ctx_weft/core/loop/steps/compact.py tests/unit/test_task_collapse.py
git commit -m "feat(compact): collapse_task_layer 把 task 层早期回合坍缩成两节 USER_PROMPT"
```

---

### Task 4: `_compact_scope` 拆两份摘要 + 独立 `collapse_keep_last` 阈值

**Files:**
- Modify: `src/ctx_weft/protocols/template.py`（`LoopConfig`，约 78-80）
- Modify: `src/ipmastercowork/providers/templates/loader.py`（`_parse_loop_config`，约 181-192）
- Modify: `src/ctx_weft/core/loop/steps/compact.py`（`_compact_scope`，约 263-313）
- Test: `tests/unit/test_task_collapse.py`（追加）

**Interfaces:**
- Produces: `LoopConfig.collapse_keep_last: int = 3`（task 坍缩保留的回合数）。
- Consumes: `summarize_for_compact(..., scope=...)`（Task 2）、`collapse_task_layer`（Task 3）、`fold_root_experience`（现有）。
- Produces: `_compact_scope` 行为——`fold_task = task_n > collapse_keep_last` → task 摘要(scope="task") + `collapse_task_layer(..., collapse_keep_last, ...)`；`fold_root = residues > compact_keep_last` → agent 摘要(scope="agent") + `fold_root_experience(..., compact_keep_last, ...)`。事件 payload：task 分支 `layer="task", source="collapse"`；agent 分支 `layer="agent", source="root_experience"`。

- [ ] **Step 1: 写失败测试（追加到 test_task_collapse.py）**

```python
async def test_compact_scope_task_uses_collapse_keep_last(monkeypatch):
    from ctx_weft.core.loop.steps import compact as cm

    calls = []
    kept_arg = {}

    async def _fake_summ(state, ctx, *, scope="task"):
        calls.append(scope)
        return f"summary-{scope}"

    async def _fake_collapse(state, ctx, keep_last, summary_text):
        kept_arg["keep_last"] = keep_last
        return 3

    monkeypatch.setattr(cm, "summarize_for_compact", _fake_summ)
    monkeypatch.setattr(cm, "collapse_task_layer", _fake_collapse)

    mem = InMemoryBlackboard()
    scope = MemoryScope(session_id="s", task_id="t1", agent_id="a")
    for i in range(6):  # 6 条 TASK_COMPACT_TYPES > collapse_keep_last(2)
        await _ingest(mem, scope, T.LLM_RESPONSE, f"turn{i}", i, role="assistant")

    agent = SimpleNamespace(
        loop_config=SimpleNamespace(compact_keep_last=6, collapse_keep_last=2),
        id="a", loop_guard=SimpleNamespace())
    state = SimpleNamespace(scope=scope, task=SimpleNamespace(id="t1"), agent=agent,
                            session=SimpleNamespace(), extra={})
    ctx = SimpleNamespace(memory=mem, provider_ctx=_ctx(),
                          task_manager=None, event_bus=None)

    events = await cm._compact_scope(state, ctx, trigger="compact")

    # 只有 task 层可折（无派发对）→ 只产 task 摘要、坍缩用 collapse_keep_last(2)
    assert calls == ["task"]
    assert kept_arg["keep_last"] == 2
    assert any(e.payload.get("source") == "collapse" for e in events)
```

- [ ] **Step 2: 运行确认失败**

Run: `cd ctx-weft && uv run pytest tests/unit/test_task_collapse.py::test_compact_scope_task_uses_collapse_keep_last -q`
Expected: FAIL（`collapse_keep_last` 属性不存在 / `_compact_scope` 仍调 `apply_compact`）

- [ ] **Step 3: LoopConfig 加 `collapse_keep_last` + loader**

`src/ctx_weft/protocols/template.py`，在 `compact_keep_last` 行之后加：

```python
    compact_keep_last: int = 6           # agent 折叠保留的胶囊数（结束顶层单元）；更老的折成摘要
    collapse_keep_last: int = 3          # task 坍缩保留的最近回合数（task 层，与胶囊数分离）
```

`src/ipmastercowork/providers/templates/loader.py` 的 `_parse_loop_config`，在 `compact_keep_last=...` 行后加：

```python
        compact_keep_last=int(raw.get("compact_keep_last", 6)),
        collapse_keep_last=int(raw.get("collapse_keep_last", 3)),
```

- [ ] **Step 4: 改 `_compact_scope`（双阈值 + 两份摘要）**

改 `_compact_scope` 顶部阈值与触发（约 276-284）：

```python
    agent = state.agent
    keep_last = agent.loop_config.compact_keep_last                               # agent 折叠：胶囊数
    collapse_keep = getattr(agent.loop_config, "collapse_keep_last", keep_last)   # task 坍缩：回合数

    events: list[Any] = []

    # (a) 活跃 task 长对话（按 collapse 阈值）；(c) 已结束 root 残留（按胶囊阈值）
    task_n = await ctx.memory.count_recent(
        scope=state.scope, types=TASK_COMPACT_TYPES, ctx=ctx.provider_ctx)
    fold_task = task_n > collapse_keep
    fold_root = await _count_root_residues(state, ctx) > keep_last
    if not fold_task and not fold_root:
        return events
```

`MEMORY_COMPACT_STARTED` 事件（288-291）保留，并把 `keep_last` 换成两值上报：

```python
    events.append(make_event(state, EventType.MEMORY_COMPACT_STARTED, payload={
        "task_id": state.task.id, "agent_id": agent.id,
        "collapse_keep_last": collapse_keep, "compact_keep_last": keep_last,
        "fold_task": fold_task, "fold_root": fold_root, "trigger": trigger,
    }))
```

删除原 292 行单次 `summary_text = await summarize_for_compact(state, ctx)`，替换 task/agent 两段（约 294-309）为：

```python
    # 按需产两份摘要：task 坍缩用 collapse_keep、agent 折叠用 keep_last；各用各 cue、各只在本层折时调 LLM。
    if fold_task:
        summary_task = await summarize_for_compact(state, ctx, scope="task")
        n_task = await collapse_task_layer(state, ctx, collapse_keep, summary_task)
        if n_task:
            events.append(make_event(state, EventType.MEMORY_COMPACTED, payload={
                "superseded_count": n_task, "layer": "task", "source": "collapse",
                "trigger": trigger, "used_llm": bool(summary_task)}))

    if fold_root:
        summary_agent = await summarize_for_compact(state, ctx, scope="agent")
        n = await fold_root_experience(state, ctx, keep_last, summary_agent)
        if n:
            events.append(make_event(state, EventType.MEMORY_COMPACTED, payload={
                "superseded_count": n, "layer": "agent", "source": "root_experience",
                "trigger": trigger, "used_llm": bool(summary_agent)}))
```

更新 `logger.info`（去掉已删的 `summary_text` 引用，报双阈值）：

```python
    logger.info("compact[%s]: agent=%s task=%s fold_task=%s(keep=%d) fold_root=%s(keep=%d)",
                trigger, agent.id, state.task.id, fold_task, collapse_keep, fold_root, keep_last)
```

- [ ] **Step 5: 运行 + 回归**

Run: `cd ctx-weft && uv run pytest tests/unit/test_task_collapse.py tests/unit/test_compaction.py tests/unit/test_predispatch_compact.py tests/unit/test_dispatch_fold_golden.py -q`
Expected: PASS（若 `test_compaction.py` 断言旧 `apply_compact`/`TASK_COMPACT_SUMMARY` 形态，见 Task 6 统一处理；旧用例若只设 `compact_keep_last`，`getattr` 回退保证不炸，但 fold_task 阈值语义已变，按 Task 6 对齐）

- [ ] **Step 6: Commit**

```bash
git add src/ctx_weft/protocols/template.py src/ipmastercowork/providers/templates/loader.py src/ctx_weft/core/loop/steps/compact.py tests/unit/test_task_collapse.py
git commit -m "feat(compact): _compact_scope 双阈值——task 坍缩 collapse_keep_last / agent 折叠 compact_keep_last"
```

---

### Task 5: 坍缩 USER_PROMPT 的当前任务框架回归

**Files:**
- Test: `tests/unit/test_task_collapse.py`（追加）
- Modify（仅当测试暴露需要）: `src/ctx_weft/core/assembler/composer.py`

**Interfaces:**
- Consumes: `DefaultComposer._build_actor_messages`（现有）；坍缩产物为 `USER_PROMPT`（mtype=="user_prompt"）+ metadata task_id。
- Produces: 确认 act 装配把坍缩 USER_PROMPT 识别为当前 task 的 user 回合，贴 `## Current Task` 框、两节内容都在。

- [ ] **Step 1: 写测试**

```python
async def test_collapsed_user_prompt_gets_current_task_frame():
    from ctx_weft.core.assembler.assembler import ContextBlock
    from ctx_weft.core.assembler.composer import DefaultComposer
    from ctx_weft.core.utils import content_to_text

    collapsed = f"原始请求：做 X{COLLAPSE_DELIM}已完成 step1/step2"
    blk = ContextBlock(id="u", source="agent_recall", kind="history", target="messages",
                       content=collapsed, priority=3, token_estimate=1,
                       metadata={"role": "user", "type": "user_prompt", "timestamp": "1",
                                 "task_id": "t1"})
    task = SimpleNamespace(id="t1", title="任务标题", description="", user_prompt="做 X",
                           user_prompt_in_memory=True, process_report=None,
                           process_report_at=None, outputs=None)
    req = SimpleNamespace(task=task, purpose="act")

    msgs = DefaultComposer()._build_actor_messages([blk], req)
    framed = content_to_text(msgs[-1].content) if msgs else ""
    joined = "\n".join(content_to_text(m.content) for m in msgs)
    assert "## Current Task" in joined and "任务标题" in joined
    assert "原始请求：做 X" in joined          # 原始节
    assert "已完成 step1/step2" in joined      # 摘要节
```

- [ ] **Step 2: 运行**

Run: `cd ctx-weft && uv run pytest tests/unit/test_task_collapse.py::test_collapsed_user_prompt_gets_current_task_frame -q`
Expected: PASS（`_current_task_user_index` 已按 `mtype=="user_prompt"`+task_id 匹配；若 FAIL 则在 composer 修 framing 使其识别坍缩 USER_PROMPT，然后重跑至 PASS）

- [ ] **Step 3: Commit**

```bash
git add tests/unit/test_task_collapse.py src/ctx_weft/core/assembler/composer.py
git commit -m "test(compact): 坍缩 USER_PROMPT 保留当前任务框架回归"
```

---

### Task 6: 全量回归 + 收尾旧断言

**Files:**
- Modify（按失败情况）: `tests/unit/test_compaction.py` 等断言旧 `_compact_scope`（`apply_compact(TASK)` → assistant `## Progress So Far`）形态的用例。
- Test: 全 unit 套件。

**Interfaces:** 无新增；对齐既有测试到新行为。

- [ ] **Step 1: 跑受影响子集，收集失败**

Run: `cd ctx-weft && uv run pytest tests/unit -q -k "compact or collapse or observe or capsule or prepare or predispatch or prompt"`
Expected: 记录 FAIL 列表。可忽略 `test_background_observe.py` 的 asyncio 事件循环 flake（单测通过、模块并跑报错，与本改动无关）。

- [ ] **Step 2: 逐个对齐断言**

对每个因「`_compact_scope` 的 task 分支不再写 assistant `## Progress So Far`、改写 `collapsed` USER_PROMPT」而失败的用例：
- 若用例验证的是 observe/background 路径 → 不应受影响，核对是否误改，保持原样；
- 若用例验证 `_compact_scope` 的 task 层结果 → 改断言为「task 层出现一条 `metadata["collapsed"]==True` 的 USER_PROMPT，内容含 `COLLAPSE_DELIM`」，不再期望 `TASK_COMPACT_SUMMARY(assistant)`。

（逐用例真实修改，不留占位；每改一个跑一次对应文件确认绿。）

- [ ] **Step 3: 全量 unit（排除已知 flake 模块）**

Run: `cd ctx-weft && uv run pytest tests/unit -q --ignore=tests/unit/test_skill_exec_encoding.py --ignore=tests/unit/test_skill_exec_liveness.py --ignore=tests/unit/test_skill_exec_python.py --ignore=tests/unit/test_skill_local_config.py --ignore=tests/unit/test_background_observe.py`
Expected: PASS（全绿）

- [ ] **Step 4: host 侧模板/回归（确认 COMPACT.md 中性化不破坏加载）**

Run: `cd .. && uv run pytest tests/ -q -k "template or compact"`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add -A
git commit -m "test(compact): 对齐既有用例到 task 坍缩 / 两 cue 新行为"
```

---

## Self-Review

**1. Spec coverage：**
- 「observer 唯一负责执行复述、形成胶囊」→ D2 保 observe/background 不动（未列任务=有意不改）。
- 「task compact 改成坍缩胶囊、之前消息用一条 user_prompt 替代、两节（原始+摘要）」→ Task 3（`collapse_task_layer`，两节 content）+ Task 5（框架）。
- 「不保留原 user_prompt」→ Task 3 folds 含 USER_PROMPT，不设 protect。
- 「范围=当前 task 超 keep_last 的早期回合」→ Task 3 用 `state.scope`(当前 task) + `recs[:-keep_last]`。
- 「task/agent 共用人格、格式相似、cue 不同指明范围」→ Task 1（中性 COMPACT.md + 两 cue）+ Task 2（scope 参数）+ Task 4（分层调用）。
- 「task 坍缩与 agent 折叠用不同阈值」→ Task 4（`collapse_keep_last` vs `compact_keep_last`，D8）。

**2. Placeholder scan：** 各 code step 均给出完整代码；Task 6 Step 2 是「按实际失败逐个改」而非占位——因失败集合依赖运行时，明确了判定规则与改法，执行者据此真实修改。

**3. Type consistency：** `collapse_task_layer(state, ctx, keep_last, summary_text) -> int`（Task 3 定义，Task 4 以 `collapse_keep` 实参调用——形参名 `keep_last` 是 task-turn 语义，与 config 字段 `collapse_keep_last` 对应）、`summarize_for_compact(..., *, scope="task")`、`COLLAPSE_DELIM`、`compact_scope` extra 键、事件 `source="collapse"` 在 Task 3/4 定义与使用一致。`LoopConfig.collapse_keep_last`（Task 4 template.py 定义、loader 解析、`_compact_scope` 使用，默认 3 一致）。cue 常量名 `_COMPACTION_INSTRUCTION` / `_AGENT_COMPACTION_INSTRUCTION` 在 Task 1 定义与使用一致。
