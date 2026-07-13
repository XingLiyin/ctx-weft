# Root 自经验承载 compaction summary + 渲染期框架不落库 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 修复 root task close 后总结性 message 消失（被打断语取代）的 bug，并把所有「呈现态」从 memory 存储移到渲染期。

**Architecture:** 五个独立可测任务：① 重写 `_synthesize_dispatch_pair`（自经验胶囊换源 + 承载 compaction summary）；② `fold_root_experience` 连带折叠 `AGENT_CONVERSATION_TURN`；③ gateway `ensure_leading_user` 兜底；④ compaction summary 渲染期包装；⑤ current-message 框架渲染期不落库。

**Tech Stack:** Python 3.11、pytest（`pytest.mark.asyncio`）、`InMemoryMemoryProvider`。

## Global Constraints

- 测试运行器：在 `ctx-weft/` 目录下 `uv run pytest`（`pyproject.toml` 已配 `pythonpath=["."]`、`testpaths=["tests"]`）。
- 所有改动在 `src/ctx_weft/` 下；测试在 `tests/unit/`。
- 时间戳一律 `from ctx_weft.core.utils import now_utc`（禁用裸 `datetime.now()`）。
- 消息格式硬规则（Anthropic）：首条必须 `role="user"`；连续同 role 由 gateway 合并，**不要**在装配层强制以 user 收尾。
- compaction summary 的包装/`## Current Message` 框架/Reply 提示等**呈现态一律渲染期生成、不落库**。
- `MemoryRecord.metadata` 含 `seq_no`（provider 注入）；composer 按 `(timestamp, seq_no)` 排序。
- `recall_recent(scope, types, limit, ctx)` 返回 **newest-first**；`supersede(ids, ctx)`；`ingest(event, ctx)`。

---

### Task 1: `_synthesize_dispatch_pair` 换源 + 承载 compaction summary（§2.1，修根因 A+B）

**Files:**
- Modify: `src/ctx_weft/core/loop/steps/finalize.py:225-287`（`_synthesize_dispatch_pair`）
- Test: `tests/unit/test_root_self_experience.py`（新建）

**Interfaces:**
- Consumes: `memory.recall_recent`、`memory.ingest`、`MemoryEvent`、`MemoryEventType`（`AGENT_CONVERSATION_TURN`/`TASK_DISPATCH`/`TASK_DISPATCH_RESULT`/`TASK_COMPACT_SUMMARY`）、`now_utc`、`generate_id`、`content_to_text`、`qualify`（均已在 finalize.py import）。
- Produces: `_synthesize_dispatch_pair(memory, scope, task, mem_content, outcome, provider_ctx) -> None`（签名不变）。胶囊四件套：`AGENT_CONVERSATION_TURN(role=user, content=task.user_prompt, metadata.origin_task_id)` → `AGENT_CONVERSATION_TURN(role=assistant, content=task_compact_summary, metadata.origin_task_id)`（仅当存在 summary）→ `TASK_DISPATCH(role=assistant)` → `TASK_DISPATCH_RESULT(role=tool)`；四件套共享 `now_utc()`、ingest 顺序即 seq_no 顺序。

- [ ] **Step 1: 写失败测试**

新建 `tests/unit/test_root_self_experience.py`：

```python
"""root 自经验胶囊：换源（task.user_prompt）+ 承载 compaction summary（§2.1）。"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from ctx_weft.core.loop.steps.finalize import _synthesize_dispatch_pair
from ctx_weft.core.state.models import NormalTaskSettings, Task
from ctx_weft.protocols import MemoryEvent, MemoryEventType, MemoryScope, ProviderContext
from ctx_weft.providers.memory_blackboard.in_memory import InMemoryMemoryProvider

pytestmark = pytest.mark.asyncio
T = MemoryEventType
_BASE = datetime(2026, 1, 1, tzinfo=UTC)


def _ctx() -> ProviderContext:
    return ProviderContext(session_id="s1", tenant_id="default")


def _sc(task_id="t1", agent="ag1") -> MemoryScope:
    return MemoryScope(session_id="s1", task_id=task_id, agent_id=agent)


def _ev(type_, scope, content, t, role=None, **meta) -> MemoryEvent:
    return MemoryEvent(type=type_, scope=scope, content=content,
                       timestamp=_BASE + timedelta(seconds=t), role=role, metadata=meta)


def _task(task_id="t1", agent="ag1", prompt="帮我把这个ppt写成pdf") -> Task:
    return Task(id=task_id, session_id="s1", status="FINISHED", tenant_id="default",
                assigned_agent_id=agent, creator_agent_id=agent, parent_task_id=None,
                title="PPTX转PDF", description="转换为 PDF", user_prompt=prompt,
                settings=NormalTaskSettings())


async def _agent_turns(mem, scope):
    """返回 agent scope 的 AGENT_CONVERSATION_TURN 记录（chronological）。"""
    recs = await mem.recall_recent(
        scope, [T.AGENT_CONVERSATION_TURN, T.TASK_DISPATCH, T.TASK_DISPATCH_RESULT], 2000, _ctx())
    return list(reversed(recs))


async def test_user_turn_uses_task_prompt_not_superseded_recall():
    """根因 A：原始 prompt 被 compaction superseded 后，仍从 task.user_prompt 取，而非打断语。"""
    mem = InMemoryMemoryProvider()
    scope = _sc()
    # 原始 prompt 已被 supersede；剩下未 superseded 的最旧 USER_PROMPT 是打断语
    orig = await mem.ingest(_ev(T.USER_PROMPT, scope, "帮我把这个ppt写成pdf", 0, role="user"), _ctx())
    await mem.supersede([orig], _ctx())
    await mem.ingest(_ev(T.USER_PROMPT, scope, "你为什么不使用技能呢", 5, role="user"), _ctx())
    task = _task(prompt="帮我把这个ppt写成pdf")

    await _synthesize_dispatch_pair(mem, scope, task, "## PDF 已完成", "success", _ctx())

    turns = await _agent_turns(mem, scope)
    users = [r for r in turns if r.type == T.AGENT_CONVERSATION_TURN and r.role == "user"]
    assert len(users) == 1
    assert users[0].content == "帮我把这个ppt写成pdf"          # 不是「你为什么不使用技能呢」
    assert users[0].metadata.get("origin_task_id") == "t1"


async def test_assistant_summary_turn_carries_compaction_summary():
    """根因 B：存活的 task_compact_summary 被镜像成 assistant 回合。"""
    mem = InMemoryMemoryProvider()
    scope = _sc()
    await mem.ingest(_ev(T.TASK_COMPACT_SUMMARY, scope, "### 会话目标\n转 PDF\n### 已完成工作\n- 试过 COM", 1, role="user"), _ctx())
    task = _task()

    await _synthesize_dispatch_pair(mem, scope, task, "## PDF 已完成", "success", _ctx())

    turns = await _agent_turns(mem, scope)
    summ = [r for r in turns if r.type == T.AGENT_CONVERSATION_TURN and r.role == "assistant"]
    assert len(summ) == 1
    assert "会话目标" in summ[0].content
    assert summ[0].metadata.get("origin_task_id") == "t1"


async def test_capsule_order_user_summary_dispatch_result():
    """胶囊渲染序：user → assistant(summary) → assistant(dispatch) → tool(result)，靠 seq_no。"""
    mem = InMemoryMemoryProvider()
    scope = _sc()
    await mem.ingest(_ev(T.TASK_COMPACT_SUMMARY, scope, "### 会话目标\n转 PDF", 1, role="user"), _ctx())
    task = _task()

    await _synthesize_dispatch_pair(mem, scope, task, "## PDF 已完成", "success", _ctx())

    turns = await _agent_turns(mem, scope)  # chronological（同 ts → seq_no 序）
    kinds = [(r.type, r.role) for r in turns]
    assert kinds == [
        (T.AGENT_CONVERSATION_TURN, "user"),
        (T.AGENT_CONVERSATION_TURN, "assistant"),
        (T.TASK_DISPATCH, "assistant"),
        (T.TASK_DISPATCH_RESULT, "tool"),
    ]
    # 同一时间戳
    assert len({r.timestamp for r in turns}) == 1


async def test_no_compaction_no_summary_turn():
    """没被 compact（无 task_compact_summary）→ 不写 assistant summary 回合。"""
    mem = InMemoryMemoryProvider()
    scope = _sc()
    task = _task()

    await _synthesize_dispatch_pair(mem, scope, task, "## PDF 已完成", "success", _ctx())

    turns = await _agent_turns(mem, scope)
    kinds = [(r.type, r.role) for r in turns]
    assert kinds == [
        (T.AGENT_CONVERSATION_TURN, "user"),
        (T.TASK_DISPATCH, "assistant"),
        (T.TASK_DISPATCH_RESULT, "tool"),
    ]


async def test_latest_summary_wins_when_multiple():
    """多条 task_compact_summary 时取最新一条。"""
    mem = InMemoryMemoryProvider()
    scope = _sc()
    await mem.ingest(_ev(T.TASK_COMPACT_SUMMARY, scope, "旧摘要", 1, role="user"), _ctx())
    await mem.ingest(_ev(T.TASK_COMPACT_SUMMARY, scope, "新摘要", 9, role="user"), _ctx())
    task = _task()

    await _synthesize_dispatch_pair(mem, scope, task, "out", "success", _ctx())

    turns = await _agent_turns(mem, scope)
    summ = [r for r in turns if r.type == T.AGENT_CONVERSATION_TURN and r.role == "assistant"]
    assert summ[0].content == "新摘要"


async def test_empty_user_prompt_skips_user_turn():
    """task.user_prompt 为空 → 跳过 user 回合（防御）。"""
    mem = InMemoryMemoryProvider()
    scope = _sc()
    task = _task(prompt="")

    await _synthesize_dispatch_pair(mem, scope, task, "out", "success", _ctx())

    turns = await _agent_turns(mem, scope)
    users = [r for r in turns if r.type == T.AGENT_CONVERSATION_TURN and r.role == "user"]
    assert users == []
```

- [ ] **Step 2: 运行测试确认失败**

Run: `cd ctx-weft && uv run pytest tests/unit/test_root_self_experience.py -v`
Expected: FAIL（当前实现取 `prompts[-1]`、无 assistant summary 回合、user 回合用 `original.timestamp`）。

- [ ] **Step 3: 实现**

把 `finalize.py` 的 `_synthesize_dispatch_pair`（225-287）整体替换为：

```python
async def _synthesize_dispatch_pair(memory, scope, task, mem_content, outcome, provider_ctx) -> None:
    """Write a synthesized root self-experience capsule when a long root task closes.

    胶囊四件套（共享 now_utc()，ingest 顺序即 seq_no 顺序，composer 按 (timestamp, seq_no) 渲染）：
      [user]      task.user_prompt           —— 稳定原始诉求（不读 memory，免受 compaction superseded 影响）
      [assistant] <task_compact_summary>      —— 仅当 task 被 compact 过；承载「会话目标/已完成工作」
      [assistant] delegate_task(tool_call)    —— 把整个 root task 表示成一次派发
      [tool]      mem_content                 —— outputs + process report
    """
    base = now_utc()
    # 1) 原始 user prompt → user 回合（来源 = task.user_prompt，稳定）
    user_text = (
        task.user_prompt if isinstance(task.user_prompt, str)
        else content_to_text(task.user_prompt or "")
    )
    if user_text:
        await memory.ingest(
            MemoryEvent(
                type=MemoryEventType.AGENT_CONVERSATION_TURN,
                scope=scope,
                content=user_text,
                timestamp=base,
                role="user",
                metadata={"origin_task_id": task.id},
            ),
            provider_ctx,
        )
    # 2) compaction summary（若有）→ assistant 回合（承载「会话目标/已完成工作」）
    summaries = await memory.recall_recent(
        scope, [MemoryEventType.TASK_COMPACT_SUMMARY], 2000, provider_ctx,
    )
    if summaries:
        latest = summaries[0]  # recall 是 newest-first
        await memory.ingest(
            MemoryEvent(
                type=MemoryEventType.AGENT_CONVERSATION_TURN,
                scope=scope,
                content=latest.content,
                timestamp=base,
                role="assistant",
                metadata={"origin_task_id": task.id},
            ),
            provider_ctx,
        )
    # 3) synthesized delegate_task ↔ result
    tool_call_id = generate_id("tcall")
    await memory.ingest(
        MemoryEvent(
            type=MemoryEventType.TASK_DISPATCH,
            scope=scope,
            content="",
            timestamp=base,
            role="assistant",
            metadata={
                "tool_call_id": tool_call_id,
                "tool_name": qualify("control:delegate_task"),
                "arguments": {
                    "title": task.title,
                    "task_prompt": task.user_prompt,
                    "description": task.description,
                },
            },
        ),
        provider_ctx,
    )
    await memory.ingest(
        MemoryEvent(
            type=MemoryEventType.TASK_DISPATCH_RESULT,
            scope=scope,
            content=mem_content,
            timestamp=base,
            role="tool",
            metadata={
                "tool_call_id": tool_call_id,
                "child_task_id": task.id,
                "title": task.title,
                "outcome": outcome,
                "parent_task_id": None,
            },
        ),
        provider_ctx,
    )
```

同时删除该函数顶部不再需要的 `from datetime import timedelta` 之类（本函数不引入 timedelta；若文件其他处用到则保留）。

- [ ] **Step 4: 运行测试确认通过**

Run: `cd ctx-weft && uv run pytest tests/unit/test_root_self_experience.py -v`
Expected: PASS（6 个用例全绿）。

- [ ] **Step 5: 回归现有 close 测试**

Run: `cd ctx-weft && uv run pytest tests/unit/test_close_task.py tests/unit/test_finalize.py -v`
Expected: PASS（若有断言旧 `prompts[-1]`/`original.timestamp` 行为的用例需同步更新）。

- [ ] **Step 6: Commit**

```bash
git add src/ctx_weft/core/loop/steps/finalize.py tests/unit/test_root_self_experience.py
git commit -m "fix(memory): root 自经验换源 task.user_prompt + 承载 compaction summary

修根因 A（synthesize_dispatch_pair 用 prompts[-1] 取错原始消息）与根因 B
（compaction summary 只在 task 层、close 时丢弃）。胶囊四件套共享 now_utc()
靠 seq_no 定序。

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 2: `fold_root_experience` 连带折叠 `AGENT_CONVERSATION_TURN`（§2.2，修折叠落单）

**Files:**
- Modify: `src/ctx_weft/core/loop/steps/compact.py:25-29`（删 `_AGENT_COMPACT_TYPES`）、`:101-150`（`fold_root_experience`）
- Test: `tests/unit/test_root_subtree_fold.py`（新建）

**Interfaces:**
- Consumes: `memory.recall_recent`、`memory.supersede`、`memory.ingest`、`now_utc`、`timedelta`、`MemoryEvent`/`MemoryEventType`（compact.py 已 import）。
- Produces: `fold_root_experience(state, ctx, keep_last, summary_text) -> int`（签名不变）；除原有 supersede 外，额外 supersede `origin_task_id ∈ 被折胶囊 task id` 的 `AGENT_CONVERSATION_TURN`。

- [ ] **Step 1: 写失败测试**

新建 `tests/unit/test_root_subtree_fold.py`：

```python
"""fold_root_experience：连带折叠被折胶囊的 AGENT_CONVERSATION_TURN（§2.2）。"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from ctx_weft.core.loop.steps.compact import fold_root_experience
from ctx_weft.protocols import MemoryEvent, MemoryEventType, MemoryScope, ProviderContext
from ctx_weft.protocols.template import LoopConfig
from ctx_weft.providers.memory_blackboard.in_memory import InMemoryMemoryProvider

pytestmark = pytest.mark.asyncio
T = MemoryEventType
_BASE = datetime(2026, 1, 1, tzinfo=UTC)


def _pctx() -> ProviderContext:
    return ProviderContext(session_id="s1", tenant_id="default")


def _sc(agent="ag1") -> MemoryScope:
    return MemoryScope(session_id="s1", task_id=None, agent_id=agent)


def _ev(type_, scope, content, t, role=None, **meta) -> MemoryEvent:
    return MemoryEvent(type=type_, scope=scope, content=content,
                       timestamp=_BASE + timedelta(seconds=t), role=role, metadata=meta)


def _state(scope):
    agent = SimpleNamespace(id=scope.agent_id, loop_config=LoopConfig())
    session = SimpleNamespace(id="s1", tenant_id="default")
    return SimpleNamespace(run_id="run1", sequence_counter=0, session=session,
                           scope=scope, task=SimpleNamespace(id="cur"), agent=agent)


def _ctx(mem):
    return SimpleNamespace(memory=mem, provider_ctx=_pctx())


async def _seed_capsule(mem, scope, task_id: str, t0: int):
    """一份 root 胶囊：user + assistant(summary) + dispatch + result，同 task_id。"""
    tcid = f"tc_{task_id}"
    await mem.ingest(_ev(T.AGENT_CONVERSATION_TURN, scope, f"prompt {task_id}", t0, role="user", origin_task_id=task_id), _pctx())
    await mem.ingest(_ev(T.AGENT_CONVERSATION_TURN, scope, f"summary {task_id}", t0, role="assistant", origin_task_id=task_id), _pctx())
    await mem.ingest(_ev(T.TASK_DISPATCH, scope, "", t0, role="assistant", tool_call_id=tcid), _pctx())
    await mem.ingest(_ev(T.TASK_DISPATCH_RESULT, scope, f"result {task_id}", t0, role="tool",
                         tool_call_id=tcid, child_task_id=task_id, parent_task_id=None), _pctx())


async def _alive(mem, scope, type_, role=None):
    recs = await mem.recall_recent(scope, [type_], 2000, _pctx())
    return [r for r in recs if role is None or r.role == role]


async def test_folded_capsule_conversation_turns_superseded():
    """被折胶囊（最旧）的 user+assistant conversation turn 不落单。"""
    mem = InMemoryMemoryProvider()
    scope = _sc()
    # keep_last=1 → 3 个胶囊中折掉最旧 2 个
    for i, t in enumerate([0, 10, 20]):
        await _seed_capsule(mem, scope, f"task{i}", t)
    n = await fold_root_experience(_state(scope), _ctx(mem), keep_last=1, summary_text="folded")
    assert n > 0
    turns = await _alive(mem, scope, T.AGENT_CONVERSATION_TURN)
    alive_tasks = {r.metadata.get("origin_task_id") for r in turns}
    assert alive_tasks == {"task2"}            # 只剩最新胶囊的 turns，旧的不落单


async def test_kept_capsule_intact():
    """保留胶囊（最新 keep_last 个）的四件套完整存活。"""
    mem = InMemoryMemoryProvider()
    scope = _sc()
    for i, t in enumerate([0, 10, 20]):
        await _seed_capsule(mem, scope, f"task{i}", t)
    await fold_root_experience(_state(scope), _ctx(mem), keep_last=2, summary_text="folded")
    turns = await _alive(mem, scope, T.AGENT_CONVERSATION_TURN)
    alive_tasks = {r.metadata.get("origin_task_id") for r in turns}
    assert alive_tasks == {"task1", "task2"}
    results = await _alive(mem, scope, T.TASK_DISPATCH_RESULT)
    assert {r.metadata.get("child_task_id") for r in results} == {"task1", "task2"}


async def test_compact_summary_sorts_before_kept_capsule():
    """新 AGENT_COMPACT_SUMMARY 的 timestamp 早于最旧保留胶囊（anchor − 1µs）。"""
    mem = InMemoryMemoryProvider()
    scope = _sc()
    for i, t in enumerate([0, 10, 20]):
        await _seed_capsule(mem, scope, f"task{i}", t)
    await fold_root_experience(_state(scope), _ctx(mem), keep_last=1, summary_text="folded")
    summ = await _alive(mem, scope, T.AGENT_COMPACT_SUMMARY)
    assert len(summ) == 1
    kept_results = await _alive(mem, scope, T.TASK_DISPATCH_RESULT)
    assert summ[0].timestamp < min(r.timestamp for r in kept_results)


async def test_nothing_to_fold_returns_zero():
    """root 残留 ≤ keep_last → 不折，conversation turns 全留。"""
    mem = InMemoryMemoryProvider()
    scope = _sc()
    await _seed_capsule(mem, scope, "task0", 0)
    n = await fold_root_experience(_state(scope), _ctx(mem), keep_last=6, summary_text="x")
    assert n == 0
    turns = await _alive(mem, scope, T.AGENT_CONVERSATION_TURN)
    assert {r.metadata.get("origin_task_id") for r in turns} == {"task0"}
```

- [ ] **Step 2: 运行测试确认失败**

Run: `cd ctx-weft && uv run pytest tests/unit/test_root_subtree_fold.py -v`
Expected: FAIL（`test_folded_capsule_conversation_turns_superseded`：旧 turns 未被 supersede，`alive_tasks` 含 task0/task1）。

- [ ] **Step 3: 实现**

(a) 删除 `compact.py:25-29` 的悬空常量：

```python
# 删除整段：
# _AGENT_COMPACT_TYPES = [
#     MemoryEventType.TASK_DISPATCH,
#     MemoryEventType.TASK_DISPATCH_RESULT,
#     MemoryEventType.AGENT_CONVERSATION_TURN,  # root self-experience records are agent-layer foldable content
# ]
```

(b) 改 `fold_root_experience`（约 109-133 行）。把 recall 列表加上 `AGENT_CONVERSATION_TURN`，并在 supersede 循环里按 `origin_task_id` 折叠：

```python
    recs = await memory.recall_recent(
        state.scope,
        [MemoryEventType.TASK_DISPATCH, MemoryEventType.TASK_DISPATCH_RESULT,
         MemoryEventType.AGENT_COMPACT_SUMMARY, MemoryEventType.AGENT_CONVERSATION_TURN],
        2000, ctx.provider_ctx,
    )
    recs = list(reversed(recs))  # recall newest-first → chronological
    root_results = [
        r for r in recs
        if r.type == MemoryEventType.TASK_DISPATCH_RESULT
        and r.metadata.get("parent_task_id") is None
    ]
    if len(root_results) <= keep_last:
        return 0
    fold = root_results if keep_last <= 0 else root_results[:-keep_last]
    kept = [] if keep_last <= 0 else root_results[-keep_last:]
    fold_tcids = {r.metadata.get("tool_call_id") for r in fold}
    fold_task_ids = {r.metadata.get("child_task_id") for r in fold}
    ids = [r.id for r in fold]
    for r in recs:
        if (r.type == MemoryEventType.TASK_DISPATCH
                and r.metadata.get("tool_call_id") in fold_tcids):
            ids.append(r.id)
        elif r.type == MemoryEventType.AGENT_COMPACT_SUMMARY:
            ids.append(r.id)  # 旧摘要并入新摘要
        elif (r.type == MemoryEventType.AGENT_CONVERSATION_TURN
                and r.metadata.get("origin_task_id") in fold_task_ids):
            ids.append(r.id)  # 被折胶囊的 user / assistant-summary 回合一并折叠，避免落单
    await memory.supersede(ids, ctx.provider_ctx)
```

（`anchor_ts = min(... for r in kept ...) - timedelta(microseconds=1)` 及之后的 ingest 段保持不变。）

- [ ] **Step 4: 运行测试确认通过**

Run: `cd ctx-weft && uv run pytest tests/unit/test_root_subtree_fold.py -v`
Expected: PASS（4 个用例全绿）。

- [ ] **Step 5: 回归 compact 测试**

Run: `cd ctx-weft && uv run pytest tests/unit/test_compact_layers.py tests/unit/test_compact_trigger.py tests/unit/test_compact_step_inline.py -v`
Expected: PASS。

- [ ] **Step 6: Commit**

```bash
git add src/ctx_weft/core/loop/steps/compact.py tests/unit/test_root_subtree_fold.py
git commit -m "fix(memory): fold_root_experience 连带折叠胶囊 conversation turn

按 origin_task_id 折叠被折 root 胶囊的 AGENT_CONVERSATION_TURN，避免 user/
assistant-summary 回合落单飘在 compact summary 上方；删除悬空常量
_AGENT_COMPACT_TYPES。

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 3: gateway `ensure_leading_user`（§2.5，防御纵深）

**Files:**
- Modify: `src/ctx_weft/core/loop/llm_gateway.py:42-96`（新增函数 + 改 `stream_llm` + 更 docstring）
- Test: `tests/unit/test_gateway_leading_user.py`（新建）

**Interfaces:**
- Consumes: `LLMMessage`（已 import）。
- Produces: `ensure_leading_user(messages: list[LLMMessage]) -> list[LLMMessage]`；`stream_llm` 内归一化顺序变为 `merge_consecutive_messages(drop_orphan_tool_results(ensure_leading_user(messages)))`。

- [ ] **Step 1: 写失败测试**

新建 `tests/unit/test_gateway_leading_user.py`：

```python
"""gateway ensure_leading_user：首条必须 user（§2.5）。"""
from __future__ import annotations

from ctx_weft.core.loop.llm_gateway import ensure_leading_user
from ctx_weft.protocols import LLMMessage


def _u(c): return LLMMessage(role="user", content=c)
def _a(c, tcs=None): return LLMMessage(role="assistant", content=c, tool_calls=tcs or [])
def _t(c, tcid): return LLMMessage(role="tool", content=c, tool_call_id=tcid)


def test_drops_leading_assistant():
    out = ensure_leading_user([_a("hi"), _u("q"), _a("a")])
    assert [m.role for m in out] == ["user", "assistant"]
    assert out[0].content == "q"


def test_drops_leading_tool_and_assistant_run():
    out = ensure_leading_user([_a("x", [{"id": "1", "name": "f", "input": {}}]), _t("r", "1"), _u("q")])
    assert [m.role for m in out] == ["user"]


def test_already_leading_user_unchanged():
    msgs = [_u("q"), _a("a"), _t("r", "1")]
    assert ensure_leading_user(msgs) == msgs


def test_empty_unchanged():
    assert ensure_leading_user([]) == []


def test_no_user_at_all_returns_empty():
    out = ensure_leading_user([_a("x"), _t("r", "1")])
    assert out == []


def test_mid_conversation_tool_result_not_touched():
    """正常工具循环：user → assistant(tool_call) → tool。首条已是 user，整体不动。"""
    msgs = [_u("q"), _a("", [{"id": "1", "name": "f", "input": {}}]), _t("r", "1")]
    assert ensure_leading_user(msgs) == msgs
```

并在同文件加一个集成断言（验证 `stream_llm` 的归一化串联顺序——前导 assistant + 其孤儿 tool 都被清理）：

```python
import pytest
from ctx_weft.core.loop.llm_gateway import drop_orphan_tool_results, merge_consecutive_messages

def test_pipeline_leading_assistant_then_orphan_cleaned():
    # ensure_leading_user 丢前导 assistant → 暴露的 tool 成孤儿 → drop_orphan 清掉
    msgs = [_a("x", [{"id": "1", "name": "f", "input": {}}]), _t("r", "1"), _u("q")]
    out = merge_consecutive_messages(drop_orphan_tool_results(ensure_leading_user(msgs)))
    assert [m.role for m in out] == ["user"]
    assert out[0].content == "q"
```

- [ ] **Step 2: 运行测试确认失败**

Run: `cd ctx-weft && uv run pytest tests/unit/test_gateway_leading_user.py -v`
Expected: FAIL with "cannot import name 'ensure_leading_user'"。

- [ ] **Step 3: 实现**

在 `llm_gateway.py` 的 `merge_consecutive_messages` 之后、`stream_llm` 之前新增：

```python
def ensure_leading_user(messages: list[LLMMessage]) -> list[LLMMessage]:
    """丢弃开头 role != "user" 的消息直到首条为 user（Anthropic 首条必须 user，否则 400）。

    只动头部、不注入文案——与「以 user 收尾」语义兜底（保留在 composer）不同，对正常工具
    循环（中段 tool result 之后无 user）无影响。前导 assistant 被丢后其配对 tool 会成孤儿，
    由随后的 drop_orphan_tool_results 清理（见 stream_llm 串联顺序）。
    """
    i = 0
    while i < len(messages) and messages[i].role != "user":
        i += 1
    return messages[i:] if i else messages
```

改 `stream_llm`（第 94 行）：

```python
    request.messages = merge_consecutive_messages(
        drop_orphan_tool_results(ensure_leading_user(request.messages))
    )
```

并在模块 docstring 的「合法化两条不变式」处补成三条（新增 `ensure_leading_user`：先丢前导非 user，再丢孤儿，最后合并）。

- [ ] **Step 4: 运行测试确认通过**

Run: `cd ctx-weft && uv run pytest tests/unit/test_gateway_leading_user.py -v`
Expected: PASS（7 个用例全绿）。

- [ ] **Step 5: 回归 gateway 测试**

Run: `cd ctx-weft && uv run pytest tests/unit/ -k gateway -v`
Expected: PASS。

- [ ] **Step 6: Commit**

```bash
git add src/ctx_weft/core/loop/llm_gateway.py tests/unit/test_gateway_leading_user.py
git commit -m "feat(gateway): ensure_leading_user 发送前归一化兜底

新增第三条合法化：丢前导非 user 消息保证首条为 user（Anthropic 硬规则），
顺序 merge(drop_orphan(ensure_leading_user(msgs)))。纯 400 防御，对正常工具
循环无影响。

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 4: compaction summary 渲染期包装（§2.4，消歧义）

**Files:**
- Modify: `src/ctx_weft/core/assembler/sources/_history.py`（加常量 + helper + `record_to_history_block` 内对 `TASK_COMPACT_SUMMARY` 包装）
- Modify: `src/ctx_weft/core/assembler/sources/agent_experience.py:78-90`（`AGENT_COMPACT_SUMMARY` 块包装）
- Test: `tests/unit/test_compact_summary_wrap.py`（新建）

**Interfaces:**
- Produces: `_history.py` 导出 `COMPACT_SUMMARY_WRAPPER_PREFIX: str` 与 `wrap_compact_summary(text: str) -> str`。两处 summary 渲染（task 层 via `record_to_history_block`、agent 层 via `agent_experience`）均调用它；存储内容不变。

- [ ] **Step 1: 写失败测试**

新建 `tests/unit/test_compact_summary_wrap.py`：

```python
"""compaction summary 渲染期包装（§2.4）：渲染带前缀，存储不含。"""
from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from ctx_weft.core.assembler.sources._history import (
    COMPACT_SUMMARY_WRAPPER_PREFIX, record_to_history_block, wrap_compact_summary,
)
from ctx_weft.protocols import MemoryEventType, MemoryRecord

T = MemoryEventType


def _rec(type_, content, role="user"):
    return MemoryRecord(id="m1", type=type_, content=content,
                        timestamp=datetime(2026, 1, 1, tzinfo=UTC), role=role,
                        topic=None, metadata={"seq_no": 1})


def test_wrap_helper_prefixes():
    out = wrap_compact_summary("### 会话目标\nX")
    assert out.startswith(COMPACT_SUMMARY_WRAPPER_PREFIX)
    assert "### 会话目标" in out


def test_task_compact_summary_block_wrapped():
    blk = record_to_history_block(_rec(T.TASK_COMPACT_SUMMARY, "### 会话目标\nX"), "task_conversation", 0)
    assert blk.content.startswith(COMPACT_SUMMARY_WRAPPER_PREFIX)


def test_plain_user_prompt_not_wrapped():
    blk = record_to_history_block(_rec(T.USER_PROMPT, "你好"), "task_conversation", 0)
    assert blk.content == "你好"


def test_agent_conversation_turn_not_wrapped():
    """胶囊里的 assistant summary 是 AGENT_CONVERSATION_TURN，不应被包装。"""
    blk = record_to_history_block(_rec(T.AGENT_CONVERSATION_TURN, "### 会话目标\nX", role="assistant"),
                                  "agent_experience", 0)
    assert blk.content == "### 会话目标\nX"
```

并加 agent_experience 的渲染断言：

```python
from ctx_weft.core.assembler.sources.agent_experience import AgentExperienceSource
from ctx_weft.protocols import ProviderContext

pytestmark = pytest.mark.asyncio


class _Mem:
    def __init__(self, recs): self._recs = recs
    async def recall_recent(self, scope, types, limit, ctx): return self._recs


async def test_agent_compact_summary_rendered_wrapped():
    rec = _rec(T.AGENT_COMPACT_SUMMARY, "### 既往派发摘要\nY")
    deps = SimpleNamespace(memory=_Mem([rec]), provider_ctx=ProviderContext(session_id="s1", tenant_id="default"))
    req = SimpleNamespace(scope=SimpleNamespace())
    blocks = [b async for b in AgentExperienceSource().fetch(req, deps)]
    summ = [b for b in blocks if b.metadata.get("type") == T.AGENT_COMPACT_SUMMARY]
    assert summ and summ[0].content.startswith(COMPACT_SUMMARY_WRAPPER_PREFIX)
```

- [ ] **Step 2: 运行测试确认失败**

Run: `cd ctx-weft && uv run pytest tests/unit/test_compact_summary_wrap.py -v`
Expected: FAIL with "cannot import name 'COMPACT_SUMMARY_WRAPPER_PREFIX'"。

- [ ] **Step 3: 实现**

(a) `_history.py` 顶部（import 之后）加常量与 helper，并在 `record_to_history_block` 内对 `TASK_COMPACT_SUMMARY` 包装：

```python
from ctx_weft.protocols import MemoryEventType

COMPACT_SUMMARY_WRAPPER_PREFIX = (
    "［以下是先前对话/经验的压缩摘要，供你延续工作参考；并非用户的新指令］\n"
)


def wrap_compact_summary(text: str) -> str:
    """给 compaction summary 文本套显式包装前缀（渲染期，不落库）。"""
    return f"{COMPACT_SUMMARY_WRAPPER_PREFIX}{text}"
```

在 `record_to_history_block` 内，`text = ...` 之后插入：

```python
    if record.type == MemoryEventType.TASK_COMPACT_SUMMARY:
        text = wrap_compact_summary(text)
```

(b) `agent_experience.py` 的 `AGENT_COMPACT_SUMMARY` 循环（78-90），把 `text` 包装：

```python
        # AGENT_COMPACT_SUMMARY → user 摘要回合（渲染期套包装前缀，消歧义）
        from ctx_weft.core.assembler.sources._history import wrap_compact_summary
        for s in summaries:
            text = content_to_text(s.content) if not isinstance(s.content, str) else s.content
            text = wrap_compact_summary(text)
            yield ContextBlock(
                ...  # 其余字段不变
```

- [ ] **Step 4: 运行测试确认通过**

Run: `cd ctx-weft && uv run pytest tests/unit/test_compact_summary_wrap.py -v`
Expected: PASS（5 个用例全绿）。

- [ ] **Step 5: 回归装配测试**

Run: `cd ctx-weft && uv run pytest tests/unit/test_assembler_reconstruction.py tests/unit/test_agent_recall_source.py tests/unit/test_golden_conformance.py -v`
Expected: PASS（如有 golden 断言旧无包装内容，按新前缀更新）。

- [ ] **Step 6: Commit**

```bash
git add src/ctx_weft/core/assembler/sources/_history.py src/ctx_weft/core/assembler/sources/agent_experience.py tests/unit/test_compact_summary_wrap.py
git commit -m "feat(assembler): compaction summary 渲染期套包装前缀

TASK/AGENT_COMPACT_SUMMARY 以 role=user 呈现时套［…压缩摘要，并非用户新指令］
前缀，消歧义；只在渲染期、不落库。胶囊内 AGENT_CONVERSATION_TURN 不套。

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 5: current-message 框架渲染期不落库（§2.6）

**Files:**
- Modify: `src/ctx_weft/core/loop/driver.py:218-239`（存 raw）
- Modify: `src/ctx_weft/core/assembler/composer.py:272-289`（Path 2 wrap-in-place）
- Test: `tests/unit/test_current_message_framing.py`（新建）

**Interfaces:**
- Consumes: `task.user_prompt`、`task.user_prompt_in_memory`、`history_pairs`（`list[(LLMMessage, src)]`）。
- Produces: driver 持久化 USER_PROMPT 时 `content = task.user_prompt`（raw）；composer 新增私有方法 `_frame_current_message(self, messages, history_pairs, task) -> None`（in-place 改写最近一条 `task_conversation` user message），在 `task.user_prompt_in_memory` 为真时调用。

- [ ] **Step 1: 写失败测试**

新建 `tests/unit/test_current_message_framing.py`：

```python
"""current-message 框架：存 raw（driver）+ 渲染期只贴最近一条 user（composer）（§2.6）。"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from ctx_weft.protocols import LLMMessage
from ctx_weft.core.assembler.composer import DefaultComposer


def _comp() -> DefaultComposer:
    # _frame_current_message 只依赖入参、不依赖实例状态 → 绕过 __init__
    return DefaultComposer.__new__(DefaultComposer)


def _u(c): return LLMMessage(role="user", content=c)
def _a(c): return LLMMessage(role="assistant", content=c)


def _task(in_mem=True, title="PPTX转PDF", desc="转 PDF", prompt="把这个 ppt 转 pdf"):
    return SimpleNamespace(user_prompt_in_memory=in_mem, title=title, description=desc,
                           user_prompt=prompt, process_report=None, id="t1")


def test_frame_only_latest_user_turn():
    """多轮：只有最近一条 task_conversation user 被框，历史 user 裸。"""
    comp = _comp()
    history_pairs = [
        (_u("检查工作目录"), "task_conversation"),
        (_a("好的"), "task_conversation"),
        (_u("把这个 ppt 转 pdf"), "task_conversation"),
    ]
    messages = [m for m, _ in history_pairs]
    comp._frame_current_message(messages, history_pairs, _task())
    assert messages[0].content == "检查工作目录"                    # 历史裸
    assert "## Current Message" in messages[2].content            # 最近被框
    assert "## Current Task" in messages[2].content
    assert "Reply in the same language" in messages[2].content
    assert "把这个 ppt 转 pdf" in messages[2].content


def test_frame_ignores_agent_experience_user():
    """agent_experience 来源的 user 回合不被当作当前消息。"""
    comp = _comp()
    history_pairs = [
        (_u("旧自经验"), "agent_experience"),
        (_u("当前消息"), "task_conversation"),
    ]
    messages = [m for m, _ in history_pairs]
    comp._frame_current_message(messages, history_pairs, _task())
    assert messages[0].content == "旧自经验"
    assert "## Current Message" in messages[1].content


def test_frame_noop_when_no_task_conversation_user():
    comp = _comp()
    history_pairs = [(_a("only assistant"), "task_conversation")]
    messages = [m for m, _ in history_pairs]
    comp._frame_current_message(messages, history_pairs, _task())
    assert messages[0].content == "only assistant"
```

> 注：composer 类是 `DefaultComposer`（`Composer` 是 Protocol）；`_build_actor_messages` / 新增的 `_frame_current_message` 都在 `DefaultComposer` 上。测试用 `DefaultComposer.__new__(DefaultComposer)` 绕过 `__init__`（方法只依赖入参）。

driver 的 raw 存储用一个轻量集成断言（沿用 test_close_task 的 harness 风格，断言 ingest 内容为 raw）：

```python
pytestmark = pytest.mark.asyncio

async def test_driver_persists_raw_user_prompt():
    from ctx_weft.providers.memory_blackboard.in_memory import InMemoryMemoryProvider
    from ctx_weft.protocols import MemoryEventType, MemoryScope, ProviderContext
    mem = InMemoryMemoryProvider()
    pctx = ProviderContext(session_id="s1", tenant_id="default")
    scope = MemoryScope(session_id="s1", task_id="t1", agent_id="ag1")
    task = _task()
    task.user_prompt_in_memory = False
    state = SimpleNamespace(task=task, scope=scope)
    ctx = SimpleNamespace(memory=mem, provider_ctx=pctx)
    # 调用 driver 内联的持久化逻辑（提取为可测函数，见实现 (a)）
    from ctx_weft.core.loop.driver import _persist_user_prompt
    await _persist_user_prompt(state, ctx)
    recs = await mem.recall_recent(scope, [MemoryEventType.USER_PROMPT], 10, pctx)
    assert recs[0].content == "把这个 ppt 转 pdf"                  # raw，无 ## Current
    assert "## Current" not in recs[0].content
    assert task.user_prompt_in_memory is True
```

- [ ] **Step 2: 运行测试确认失败**

Run: `cd ctx-weft && uv run pytest tests/unit/test_current_message_framing.py -v`
Expected: FAIL（`_frame_current_message`/`_persist_user_prompt` 不存在；driver 仍存 baked 内容）。

- [ ] **Step 3a: 实现 driver 存 raw**

把 `driver.py:216-239` 的内联持久化抽成可测函数并存 raw。在 driver 模块加：

```python
async def _persist_user_prompt(state, ctx) -> None:
    """task 启动时持久化 raw user_prompt（呈现态框架由 composer 渲染期生成，不落库）。"""
    task = state.task
    if not task.user_prompt or task.user_prompt_in_memory:
        return
    from ctx_weft.core.utils import content_to_text, now_utc
    text = (task.user_prompt if isinstance(task.user_prompt, str)
            else content_to_text(task.user_prompt))
    await ctx.memory.ingest(
        MemoryEvent(
            type=MemoryEventType.USER_PROMPT,
            scope=state.scope,
            content=text,
            timestamp=now_utc(),
            role="user",
            metadata={"task_id": task.id},
        ),
        ctx.provider_ctx,
    )
    task.user_prompt_in_memory = True
```

把原 216-239 段替换为 `await _persist_user_prompt(state, ctx)`。

- [ ] **Step 3b: 实现 composer wrap-in-place**

在 `composer.py` 给 `_build_actor_messages` 所在类加私有方法：

```python
    def _frame_current_message(self, messages, history_pairs, task) -> None:
        """In-memory 路径：把最近一条 task_conversation user message 包成当前消息框架（不落库）。"""
        target = None
        for i, (m, src) in enumerate(history_pairs):
            if m.role == "user" and src == "task_conversation":
                target = i
        if target is None:
            return
        raw = content_to_text(messages[target].content)
        prefix = ""
        if task.title and task.description:
            prefix = f"## Current Task\n{task.title}\n{task.description}\n\n"
        elif task.title:
            prefix = f"## Current Task\n{task.title}\n\n"
        framed = (
            f"{prefix}## Current Message\n{raw}\n\n"
            "（Reply in the same language as the Current Message above.）"
        )
        messages[target] = LLMMessage(role="user", content=framed)
```

把 `if not task.user_prompt_in_memory:` 块（272-289）补上 `else` 分支：

```python
        if not task.user_prompt_in_memory:
            # daemon / 未持久化：实时构建完整用户消息（原逻辑不变）
            ...
        else:
            # in-memory：渲染期就地装饰最近一条 task_conversation user 回合
            self._frame_current_message(messages, history_pairs, task)
```

（`## Current Progress` 仍由 `_progress_history_block` 单独处理，不在本方法内。）

- [ ] **Step 4: 运行测试确认通过**

Run: `cd ctx-weft && uv run pytest tests/unit/test_current_message_framing.py -v`
Expected: PASS。

- [ ] **Step 5: 回归 composer / driver / 装配测试**

Run: `cd ctx-weft && uv run pytest tests/unit/ -k "composer or driver or assembler or resume or hitl" -v`
Expected: PASS（如有断言旧 baked `## Current Message` 内容的用例，按 raw 存储 + 渲染期框架更新）。

- [ ] **Step 6: Commit**

```bash
git add src/ctx_weft/core/loop/driver.py src/ctx_weft/core/assembler/composer.py tests/unit/test_current_message_framing.py
git commit -m "fix(memory): current-message 框架渲染期不落库

USER_PROMPT 改存 raw（driver）；## Current Task/Message + Reply 提示改为
composer 渲染期只贴最近一条 task_conversation user 回合。顺带修掉「只有首条
带框架」的现状不一致。

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 6: 全量回归 + 端到端排序 golden

**Files:**
- Test: `tests/unit/test_dispatch_fold_golden.py`（新建/恢复）

**Interfaces:**
- Consumes: Task 1–5 的产物。
- Produces: 一个端到端用例——「compact 过的 root close → 渲染序」golden。

- [ ] **Step 1: 写 golden 测试**

新建 `tests/unit/test_dispatch_fold_golden.py`：

```python
"""端到端：compact 过的 root task close 后，agent 层渲染序符合 §2.2。"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from ctx_weft.core.assembler.sources.agent_experience import AgentExperienceSource
from ctx_weft.core.loop.steps.finalize import _synthesize_dispatch_pair
from ctx_weft.protocols import (
    MemoryEvent, MemoryEventType, MemoryScope, ProviderContext,
)
from ctx_weft.core.state.models import NormalTaskSettings, Task
from ctx_weft.providers.memory_blackboard.in_memory import InMemoryMemoryProvider

pytestmark = pytest.mark.asyncio
T = MemoryEventType
_BASE = datetime(2026, 1, 1, tzinfo=UTC)


def _pctx(): return ProviderContext(session_id="s1", tenant_id="default")
def _sc(agent="ag1"): return MemoryScope(session_id="s1", task_id=None, agent_id=agent)


def _task():
    return Task(id="t1", session_id="s1", status="FINISHED", tenant_id="default",
                assigned_agent_id="ag1", creator_agent_id="ag1", parent_task_id=None,
                title="PPTX转PDF", description="转 PDF", user_prompt="把 ppt 转 pdf",
                settings=NormalTaskSettings())


async def test_capsule_renders_user_summary_dispatch_result_in_order():
    mem = InMemoryMemoryProvider()
    scope = _sc()
    # task scope 有一条存活 compaction summary
    tscope = MemoryScope(session_id="s1", task_id="t1", agent_id="ag1")
    await mem.ingest(MemoryEvent(type=T.TASK_COMPACT_SUMMARY, scope=tscope,
                                 content="### 会话目标\n转 PDF", timestamp=_BASE, role="user",
                                 metadata={}), _pctx())
    # 在 agent scope 合成胶囊
    await _synthesize_dispatch_pair(mem, scope, _task(), "## PDF 已完成", "success", _pctx())

    deps = SimpleNamespace(memory=mem, provider_ctx=_pctx())
    req = SimpleNamespace(scope=scope)
    blocks = [b async for b in AgentExperienceSource().fetch(req, deps)]
    blocks.sort(key=lambda b: (b.metadata.get("timestamp", ""), b.metadata.get("seq_no", 0)))
    roles = [b.metadata.get("role") for b in blocks]
    # user(原始诉求) → assistant(summary) → assistant(delegate tool_call) → tool(result)
    assert roles == ["user", "assistant", "assistant", "tool"]
    assert "把 ppt 转 pdf" in blocks[0].content
    assert "会话目标" in blocks[1].content
    assert blocks[3].content.startswith("## PDF 已完成")
```

- [ ] **Step 2: 运行 golden + 全量回归**

Run: `cd ctx-weft && uv run pytest tests/unit/test_dispatch_fold_golden.py -v && uv run pytest -q`
Expected: golden PASS；全量 `uv run pytest -q` 全绿（修掉任何被本计划行为变更波及的旧断言）。

- [ ] **Step 3: Commit**

```bash
git add tests/unit/test_dispatch_fold_golden.py
git commit -m "test(memory): 端到端胶囊渲染序 golden + 全量回归

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## 备注（执行者必读）

- 工作区 `git status` 里 `test_root_self_experience.py` / `test_root_subtree_fold.py` / `test_dispatch_fold_golden.py` 显示为 **deleted**（在途重写）。本计划「新建」它们即覆盖这些路径——执行前 `git status` 确认是删除态，按计划内容重写即可。
- 每个 Task 的 Step 5「回归」若撞到断言旧行为的既有用例，**更新断言以匹配新设计**（不要改回旧实现）；若不确定该用例意图，停下并向人确认。
- 全量 `uv run pytest -q` 必须全绿才算完成（§4 验收）。
