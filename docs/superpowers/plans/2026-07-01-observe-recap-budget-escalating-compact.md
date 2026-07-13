# observe 收敛 retry 段折叠 + 预算驱动三级升级 compact 实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让 retry 三来源在 observe 前台同步折成胶囊段摘要（删本轮 raw），把所有预算压缩收敛成 prepare/pre_dispatch 共用的一套「L1 agent 折 → L2 rich→lean 降级 → L3 坍当前 task」升级式 compact。

**Architecture:** observe 不再做预算 compact，只在 retry 时前台 `apply_compact(keep_last=0)` 折本轮 attempt；compact 逻辑单一实现 `escalating_compact`，由预算触发、双比率滞后、级间用「活跃记忆 token」代理增量粗估、进 act 前完整重装配一次校正。

**Tech Stack:** Python 3 (async)、pytest（`uv run pytest`）、`InMemoryMemoryProvider` 做 memory fixture、`SimpleNamespace` 做 state/ctx stub。

## Global Constraints

- 测试运行器：`cd ctx-weft && uv run pytest`（pyproject 已配 `pythonpath=["."]`）。
- 段摘要恒为 `MemoryEventType.TASK_COMPACT_SUMMARY`、`role="assistant"`、`protect_types=(USER_PROMPT,)`。
- 动 core 落 `src/ctx_weft/`；配置字段落 `ctx_weft/protocols/template.py` 的 `LoopConfig`。
- 触发一律看预算（token 比率）；`compact_keep_last`/`collapse_keep_last` 只作保留底线，不作触发门。
- 频繁提交：每个 Task 末尾一次 commit。
- Spec：`docs/superpowers/specs/2026-07-01-observe-recap-budget-escalating-compact-design.md`。

---

## 文件结构

| 文件 | 职责 | 改动 |
|---|---|---|
| `ctx_weft/protocols/template.py` | `LoopConfig` 字段 | 加 `compact_target_ratio`；`compact_message_delta` 标弃用；keep_last 注释改「保留底线」 |
| `ctx_weft/core/loop/steps/observe.py` | retry 前台段折 | `_maybe_compact_task` → `_fold_retry_segment`（无条件 keep_last=0）；`_should_use_llm` 扩 context_limit |
| `ctx_weft/core/loop/steps/finalize.py` | retry 收尾 | retry 分支不再写 `process_report`/`process_report_at` |
| `ctx_weft/core/assembler/composer.py` | 装配渲染 | 删 `_progress_already_in_compact` + `process_report` 的 Progress-So-Far 渲染分支 |
| `ctx_weft/core/loop/steps/compact.py` | 单一升级 compact | 加 `_active_memory_tokens`、`demote_kept_capsules`（L2）、`escalating_compact`（替 `_compact_scope`） |
| `ctx_weft/core/loop/steps/prepare.py` | 预算触发 | `_should_compact` 纯预算；调 `escalating_compact` + 末次重装配 |
| `ctx_weft/core/loop/steps/act.py` | 派发前压缩 | `_maybe_predispatch_compact` 走 `escalating_compact` |

依赖顺序：Task 1（配置）→ 2（observe）→ 3（finalize+composer）→ 4（token 代理）→ 5（L2 降级）→ 6（升级编排）→ 7（prepare）→ 8（pre_dispatch）。

---

### Task 1: LoopConfig 加 `compact_target_ratio`、弃用条数门控

**Files:**
- Modify: `src/ctx_weft/protocols/template.py:78-81`
- Test: `tests/unit/test_loop_config_compact_ratio.py`

**Interfaces:**
- Produces: `LoopConfig.compact_target_ratio: float = 0.0`（0 = 无滞后，回退等于 `compact_token_ratio`）。`compact_message_delta` 保留字段但标弃用（不再被读）。

- [ ] **Step 1: 写失败测试**

```python
# tests/unit/test_loop_config_compact_ratio.py
from ctx_weft.protocols.template import LoopConfig


def test_compact_target_ratio_defaults_to_zero():
    assert LoopConfig().compact_target_ratio == 0.0


def test_compact_target_ratio_below_trigger():
    lc = LoopConfig(compact_token_ratio=0.8, compact_target_ratio=0.6)
    assert lc.compact_target_ratio < lc.compact_token_ratio
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd ctx-weft && uv run pytest tests/unit/test_loop_config_compact_ratio.py -q`
Expected: FAIL（`LoopConfig` 无 `compact_target_ratio` 属性 → AttributeError/TypeError）

- [ ] **Step 3: 加字段**

`template.py`，在 `compact_token_ratio` 之后（约第 78 行区域）改成：

```python
    compact_token_ratio: float = 0.8
    # 压缩「压到」目标比率（滞后区下沿）：触发后一路升级直到 token 估算 < 此比率 * context_limit。
    # 0 = 无滞后，回退等于 compact_token_ratio（压到刚低于触发比率即停）。应设得比 compact_token_ratio 低。
    compact_target_ratio: float = 0.0
    compact_message_delta: int = 20      # DEPRECATED（2026-07-01）：compact 改纯预算驱动，本字段不再被读
    compact_keep_last: int = 6           # 保留底线（非触发门）：agent 层折叠保留的胶囊数；更老的折成摘要
    collapse_keep_last: int = 3          # 保留底线（非触发门）：task 坍缩保留的最近段摘要条数
```

- [ ] **Step 4: 跑测试确认通过**

Run: `cd ctx-weft && uv run pytest tests/unit/test_loop_config_compact_ratio.py -q`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add src/ctx_weft/protocols/template.py tests/unit/test_loop_config_compact_ratio.py
git commit -m "feat(compact): LoopConfig 加 compact_target_ratio、弃用 compact_message_delta 门控"
```

---

### Task 2: observe retry 前台同步段折 + context_limit 强制 LLM

把 `_maybe_compact_task`（仅 max_turns、看 keep_last、复用 verdict.act_recap）重塑成 `_fold_retry_segment`：**凡 verdict.task_outcome=="retry" 就无条件折**（`keep_last=0` 折掉本轮全部 raw，复用 act_recap 作段摘要）。`_should_use_llm` 扩到 context_limit，保证 root 机械退出也有质量 recap。

**Files:**
- Modify: `src/ctx_weft/core/loop/steps/observe.py`（`execute` 241 行、`_should_use_llm` 399 行、`_maybe_compact_task` 407-461 行）
- Test: `tests/unit/test_observe_retry_fold.py`

**Interfaces:**
- Consumes: `ctx.memory.apply_compact(scope, summary, keep_last, ctx, layer=MemoryLayer.TASK, protect_types=(USER_PROMPT,))`；`Verdict(task_outcome, act_recap, ...)`。
- Produces: `ObserveStep._fold_retry_segment(state, ctx, verdict, events) -> None`（写一条 `TASK_COMPACT_SUMMARY` + supersede 本轮 raw；仅当 `verdict.task_outcome=="retry"`）。

- [ ] **Step 1: 写失败测试**

```python
# tests/unit/test_observe_retry_fold.py
from datetime import datetime, timedelta, UTC
from types import SimpleNamespace

import pytest

from ctx_weft.core.loop.steps.observe import ObserveStep
from ctx_weft.providers.memory_blackboard.in_memory import InMemoryMemoryProvider
from ctx_weft.protocols import MemoryEvent, MemoryEventType as T, MemoryScope, ProviderContext

pytestmark = pytest.mark.asyncio

_BASE = datetime(2026, 7, 1, tzinfo=UTC)


def _pctx():
    return ProviderContext(session_id="s", tenant_id="tn")


async def _ingest(mem, scope, typ, content, i, role="user"):
    await mem.ingest(MemoryEvent(type=typ, scope=scope, content=content,
                                 timestamp=_BASE + timedelta(seconds=i), role=role,
                                 metadata={"task_id": scope.task_id}), _pctx())


async def test_retry_folds_current_attempt_and_deletes_raw():
    mem = InMemoryMemoryProvider()
    scope = MemoryScope(session_id="s", task_id="t1", agent_id="a")
    await _ingest(mem, scope, T.USER_PROMPT, "原始请求", 0)
    await _ingest(mem, scope, T.LLM_RESPONSE, "本轮回复", 1, role="assistant")
    await _ingest(mem, scope, T.TOOL_RESULT, "本轮工具结果", 2, role="tool")

    state = SimpleNamespace(scope=scope, task=SimpleNamespace(id="t1"),
                            agent=SimpleNamespace(loop_config=SimpleNamespace(compact_keep_last=6)),
                            session=SimpleNamespace())
    ctx = SimpleNamespace(memory=mem, provider_ctx=_pctx())
    verdict = SimpleNamespace(task_outcome="retry", act_recap="本段摘要：调了工具X", reported=False)

    events = []
    await ObserveStep()._fold_retry_segment(state, ctx, verdict, events)

    recs = await mem.recall_recent(scope, [T.USER_PROMPT, T.LLM_RESPONSE,
                                           T.TOOL_RESULT, T.TASK_COMPACT_SUMMARY], 100, _pctx())
    types = {r.type for r in recs}
    # 本轮 raw 被折掉，USER_PROMPT 锚保留，新增一条段摘要
    assert T.LLM_RESPONSE not in types and T.TOOL_RESULT not in types
    assert T.USER_PROMPT in types
    summ = [r for r in recs if r.type == T.TASK_COMPACT_SUMMARY]
    assert len(summ) == 1 and summ[0].content == "本段摘要：调了工具X" and summ[0].role == "assistant"


async def test_non_retry_outcome_does_not_fold():
    mem = InMemoryMemoryProvider()
    scope = MemoryScope(session_id="s", task_id="t1", agent_id="a")
    await _ingest(mem, scope, T.LLM_RESPONSE, "回复", 1, role="assistant")
    state = SimpleNamespace(scope=scope, task=SimpleNamespace(id="t1"),
                            agent=SimpleNamespace(loop_config=SimpleNamespace(compact_keep_last=6)),
                            session=SimpleNamespace())
    ctx = SimpleNamespace(memory=mem, provider_ctx=_pctx())
    verdict = SimpleNamespace(task_outcome="success", act_recap="done", reported=True)
    events = []
    await ObserveStep()._fold_retry_segment(state, ctx, verdict, events)
    recs = await mem.recall_recent(scope, [T.LLM_RESPONSE, T.TASK_COMPACT_SUMMARY], 100, _pctx())
    assert any(r.type == T.LLM_RESPONSE for r in recs)  # 未折
    assert not any(r.type == T.TASK_COMPACT_SUMMARY for r in recs)


def test_should_use_llm_forces_on_context_limit():
    template = SimpleNamespace(identity={"observe": object()})
    state = SimpleNamespace(
        extra={"template": template},
        act_exit_reason="context_limit",
        task=SimpleNamespace(parent_task_id=None))  # root
    assert ObserveStep()._should_use_llm(state) is True
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd ctx-weft && uv run pytest tests/unit/test_observe_retry_fold.py -q`
Expected: FAIL（`_fold_retry_segment` 不存在；`_should_use_llm` context_limit 分支返回 False）

- [ ] **Step 3: 实现**

`observe.py`，`_should_use_llm` 把 399 行的 max_turns 判断扩成两者：

```python
        # 机械退出（max_turns/context_limit）：即使 root 也要 LLM observe，产有质量 act_recap 作段摘要
        if state.act_exit_reason in ("max_turns", "context_limit"):
            return True
```

`execute` 把原第 241 行 `await self._maybe_compact_task(...)` 替换为（放在第 236-238 行强制 retry 之后）：

```python
        # retry（三来源：max_turns/context_limit/observer-retry）→ 前台同步段折：
        # 本轮 attempt raw 折成一条 TASK_COMPACT_SUMMARY（复用 act_recap），删本轮 raw。
        # 「马上要重跑」故同步做好，下个 run 一进 prepare 即见折后段摘要。
        if verdict.task_outcome == "retry":
            await self._fold_retry_segment(state, ctx, verdict, events)
```

把 `_maybe_compact_task`（407-461 行）整个替换为：

```python
    async def _fold_retry_segment(
        self, state: LoopState, ctx: LoopContext, verdict: Verdict, events: list[Any]
    ) -> None:
        """retry 前台同步段折：本轮 attempt raw → 一条 TASK_COMPACT_SUMMARY（复用 act_recap），
        supersede 本轮全部 raw（keep_last=0），保 USER_PROMPT 锚。无条件、无预算门、无额外 LLM。

        act_recap 来源：本轮真走成 report_task_outcome（reported）用其可信 report，否则用 verdict.act_recap
        （root 机械退出经 _should_use_llm 强制 LLM 已产出）。空则占位。
        """
        summary = (verdict.act_recap if (verdict.act_recap and verdict.act_recap.strip())
                   else "[Context compacted]")
        result = await ctx.memory.apply_compact(
            scope=state.scope,
            summary=summary,
            keep_last=0,
            ctx=ctx.provider_ctx,
            layer=MemoryLayer.TASK,
            protect_types=(MemoryEventType.USER_PROMPT,),
        )
        events.append(make_event(state, EventType.MEMORY_COMPACTED, payload={
            "events_before": result.events_before,
            "events_after": result.events_after,
            "summary_event_id": result.summary_event_id,
            "summary_length": len(summary),
            "layer": "task",
            "trigger": "observe_retry",
        }))
```

同时把 background observe 触发条件（249 行）加防御 `verdict.task_outcome != "retry"`，避免 observer-retry 且 actor_done 时既折又 launch：

```python
        if (state.act_exit_reason in ("normal", "actor_done")
                and verdict.task_outcome != "retry" and _is_own_root(state.task)):
```

（`TASK_COMPACT_TYPES` / `summarize_for_compact` 若因删 `_maybe_compact_task` 变为未使用，删对应 import。）

- [ ] **Step 4: 跑测试确认通过**

Run: `cd ctx-weft && uv run pytest tests/unit/test_observe_retry_fold.py -q`
Expected: PASS

- [ ] **Step 5: 跑既有 observe 回归**

Run: `cd ctx-weft && uv run pytest tests/unit/test_observe_task_compaction.py -q`
Expected: 该文件断言旧 `_maybe_compact_task` 行为（max_turns-only、keep_last 门控）——**预期部分 FAIL**。按新契约更新它：改为断言「retry 无条件折 keep_last=0」，删掉「未上报走 summarize_for_compact」「keep_last 门控 no-op」等已不成立的用例（新逻辑复用 verdict.act_recap、不再调 summarize_for_compact）。更新后重跑至 PASS。

- [ ] **Step 6: 提交**

```bash
git add src/ctx_weft/core/loop/steps/observe.py tests/unit/test_observe_retry_fold.py tests/unit/test_observe_task_compaction.py
git commit -m "feat(observe): retry 三来源前台同步段折 + context_limit 强制 LLM"
```

---

### Task 3: finalize 停写 retry process_report + composer 去重复渲染

retry 反馈改由 Task 2 的 `TASK_COMPACT_SUMMARY` 承载（`_history.py` 已冠 `## Progress So Far` 标题）。故 finalize 的 retry 分支不再写 `process_report`/`process_report_at`，composer 删掉据 `process_report` 的独立渲染 + 去重。

**Files:**
- Modify: `src/ctx_weft/core/loop/steps/finalize.py:326-337`
- Modify: `src/ctx_weft/core/assembler/composer.py`（317-322、342-343、630-645 行）
- Test: `tests/unit/test_finalize_retry_no_process_report.py`

**Interfaces:**
- Consumes: `FinalizeStep.execute`；`Verdict`。
- Produces: retry 后 `task.process_report` 保持 None（未被 finalize 设置）；composer 不再有 `_progress_already_in_compact` / `_progress_history_block` 的 process_report 渲染路径。

- [ ] **Step 1: 写失败测试**

```python
# tests/unit/test_finalize_retry_no_process_report.py
from types import SimpleNamespace
import pytest

from ctx_weft.core.assembler.composer import DefaultComposer
from ctx_weft.core.assembler.assembler import ContextBlock
from ctx_weft.core.utils import content_to_text

pytestmark = pytest.mark.asyncio


def test_composer_renders_no_progress_from_process_report():
    # 即使 task.process_report 有值，也不再单独渲染 ## Progress So Far（改由段摘要承载）
    task = SimpleNamespace(id="t1", title="标题", description="", user_prompt="做 X",
                           user_prompt_in_memory=False, process_report="陈旧进度",
                           process_report_at=None, outputs=None)
    req = SimpleNamespace(task=task, purpose="act")
    msgs = DefaultComposer()._build_actor_messages([], req)
    joined = "\n".join(content_to_text(m.content) for m in msgs)
    assert "陈旧进度" not in joined
    assert "Progress So Far" not in joined
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd ctx-weft && uv run pytest tests/unit/test_finalize_retry_no_process_report.py -q`
Expected: FAIL（composer 仍据 `process_report` 渲染 `## Progress So Far` → "陈旧进度" 出现）

- [ ] **Step 3: 改 composer**

`composer.py` 把 317-322 行（`progress_as_history` 计算与追加）整段删除，直接用原始 `history_blocks`：

```python
        # Progress So Far 改由 task 层 TASK_COMPACT_SUMMARY 段摘要承载（_history 冠标题渲染），
        # 不再据 task.process_report 单独渲染 → 无重复、来源单一（spec 2026-07-01 §3.7）。
        history_pairs = self._history_to_messages_with_sources(history_blocks)
```

删除 342-343 行 `if getattr(task, "process_report", None): parts.append(...)` 两行（daemon 分支里的 process_report 渲染）。

删除 `_progress_already_in_compact`（630-645 行）与 `_progress_history_block`（647-664 行）两个方法（已无调用者）。

- [ ] **Step 4: 改 finalize**

`finalize.py` retry 分支（326-337 行）删掉 process_report 两行赋值：

```python
        elif outcome == "retry":
            # retry 反馈由 observe 前台段折写的 TASK_COMPACT_SUMMARY 承载（spec 2026-07-01 §3.1）；
            # 不再写 process_report/process_report_at（旧 Progress So Far 字段路径已废）。
            task.outputs = None
            task.retry_count += 1
            events.append(make_event(
                state, EventType.TASK_REQUEUED,
                payload={"outcome": "retry", "summary": summary, "retry_count": task.retry_count},
            ))
```

- [ ] **Step 5: 跑测试确认通过**

Run: `cd ctx-weft && uv run pytest tests/unit/test_finalize_retry_no_process_report.py -q`
Expected: PASS

- [ ] **Step 6: 回归 + 校验 task_manager 消费**

检查 `task_manager.py:391`（`prev_output = _outputs_to_text(task.outputs) or (task.process_report or "")`）：retry 后 `outputs=None` 且 `process_report=None` → `prev_output=""`。这是 dispatch inherit 的兜底，段摘要已在 task 层记忆里可召回，空串可接受。无需改动，仅确认。

Run: `cd ctx-weft && uv run pytest tests/unit/test_compaction.py tests/unit/test_compact_protects_user.py -q`
Expected: 若有断言旧 process_report/Progress 渲染的用例 → 更新为段摘要断言后 PASS。

- [ ] **Step 7: 提交**

```bash
git add src/ctx_weft/core/loop/steps/finalize.py src/ctx_weft/core/assembler/composer.py tests/unit/test_finalize_retry_no_process_report.py
git commit -m "feat(compact): retry 不再写 process_report，composer 去重复 Progress 渲染"
```

---

### Task 4: compact 活跃记忆 token 代理（分析式粗估基础）

升级式 compact 级间不完整重装配，用「活跃记忆 token」代理的 before/after 增量估算被折省下的 token。本 Task 建这个纯查询工具。

**Files:**
- Modify: `src/ctx_weft/core/loop/steps/compact.py`（加函数 + import `estimate_tokens`）
- Test: `tests/unit/test_active_memory_tokens.py`

**Interfaces:**
- Produces: `async def _active_memory_tokens(state, ctx) -> int`——召回当前 scope 活跃的 task 层 body（`_TASK_BODY_TYPES`）+ agent 层对话/摘要（`AGENT_CONVERSATION_TURN`/`AGENT_COMPACT_SUMMARY`），对每条 content 求 `estimate_tokens` 之和。

- [ ] **Step 1: 写失败测试**

```python
# tests/unit/test_active_memory_tokens.py
from datetime import datetime, timedelta, UTC
from types import SimpleNamespace
import pytest

from ctx_weft.core.loop.steps.compact import _active_memory_tokens
from ctx_weft.providers.memory_blackboard.in_memory import InMemoryMemoryProvider
from ctx_weft.protocols import MemoryEvent, MemoryEventType as T, MemoryScope, ProviderContext

pytestmark = pytest.mark.asyncio
_BASE = datetime(2026, 7, 1, tzinfo=UTC)


def _pctx():
    return ProviderContext(session_id="s", tenant_id="tn")


async def test_active_tokens_sums_and_drops_after_supersede():
    mem = InMemoryMemoryProvider()
    scope = MemoryScope(session_id="s", task_id="t1", agent_id="a")
    ids = []
    for i in range(3):
        await mem.ingest(MemoryEvent(type=T.LLM_RESPONSE, scope=scope, content="x" * 400,
                                     timestamp=_BASE + timedelta(seconds=i), role="assistant",
                                     metadata={"task_id": "t1"}), _pctx())
    state = SimpleNamespace(scope=scope, agent=SimpleNamespace())
    ctx = SimpleNamespace(memory=mem, provider_ctx=_pctx())
    before = await _active_memory_tokens(state, ctx)
    assert before > 0
    # supersede 掉最老一条后总量下降
    recs = await mem.recall_recent(scope, [T.LLM_RESPONSE], 100, _pctx())
    await mem.supersede([recs[-1].id], _pctx())
    after = await _active_memory_tokens(state, ctx)
    assert after < before
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd ctx-weft && uv run pytest tests/unit/test_active_memory_tokens.py -q`
Expected: FAIL（`_active_memory_tokens` 不存在）

- [ ] **Step 3: 实现**

`compact.py` 顶部 import 补 `estimate_tokens`：

```python
from ctx_weft.core.utils import content_to_text, now_utc, estimate_tokens
```

加函数（放在 `_count_root_residues` 附近）：

```python
_AGENT_LAYER_TYPES = [
    MemoryEventType.AGENT_CONVERSATION_TURN,
    MemoryEventType.AGENT_COMPACT_SUMMARY,
]


async def _active_memory_tokens(state: LoopState, ctx: LoopContext) -> int:
    """当前 scope 活跃记忆的 token 代理：task 层 body（跨 task 按 agent 召回）+ agent 层对话/摘要，
    逐条 content 求 estimate_tokens 之和。用于升级 compact 级间的 before/after 增量粗估（非精确装配）。"""
    total = 0
    body = await ctx.memory.recall_recent_by_agent(
        state.scope, _TASK_BODY_TYPES, 2000, ctx.provider_ctx)
    agent_recs = await ctx.memory.recall_recent(
        state.scope, _AGENT_LAYER_TYPES, 2000, ctx.provider_ctx)
    for r in [*body, *agent_recs]:
        text = r.content if isinstance(r.content, str) else content_to_text(r.content)
        total += estimate_tokens(text)
    return total
```

- [ ] **Step 4: 跑测试确认通过**

Run: `cd ctx-weft && uv run pytest tests/unit/test_active_memory_tokens.py -q`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add src/ctx_weft/core/loop/steps/compact.py tests/unit/test_active_memory_tokens.py
git commit -m "feat(compact): 加 _active_memory_tokens 活跃记忆 token 代理"
```

---

### Task 5: L2 —— 同 agent rich 胶囊降级成 sub-agent lean 表示

把 L1 保留的、本 agent 亲自做的 rich 胶囊（task 层 body + agent 层 finish 对）降级成 lean 表示：删该 task 的 task 层 body，agent 层 finish 对塌成一条 tool 回填（`origin_task_id` 保身份，content=`[outcome=fail]? ` + finish 对 tool 槽综合总结）。无 LLM（内容取自现有 finish 对）。

**Files:**
- Modify: `src/ctx_weft/core/loop/steps/compact.py`（加 `demote_kept_capsules`）
- Test: `tests/unit/test_demote_kept_capsules.py`

**Interfaces:**
- Consumes: `_dispatch_finish_sets`、`_TASK_BODY_TYPES`、`recall_recent_by_agent`、`recall_recent`、`memory.supersede`、`memory.ingest`。
- Produces: `async def demote_kept_capsules(state, ctx, origin_ids: set[str]) -> int`——对 `origin_ids` 里每个同 agent 顶层单元：supersede 其 task 层 body（`metadata["task_id"] in origin_ids`）+ agent 层 finish 对的 assistant 槽；把 tool 槽内容前缀化保留（若已 lean 则跳过）。返回 supersede 条数。仅处理**同 agent finish 对**（has_finish 命中、非 dispatch-only）。

- [ ] **Step 1: 写失败测试**

```python
# tests/unit/test_demote_kept_capsules.py
from datetime import datetime, timedelta, UTC
from types import SimpleNamespace
import pytest

from ctx_weft.core.loop.steps.compact import demote_kept_capsules
from ctx_weft.providers.memory_blackboard.in_memory import InMemoryMemoryProvider
from ctx_weft.protocols import MemoryEvent, MemoryEventType as T, MemoryScope, ProviderContext

pytestmark = pytest.mark.asyncio
_BASE = datetime(2026, 7, 1, tzinfo=UTC)


def _pctx():
    return ProviderContext(session_id="s", tenant_id="tn")


async def test_demote_drops_body_and_thins_finish_pair():
    mem = InMemoryMemoryProvider()
    scope = MemoryScope(session_id="s", task_id="root", agent_id="a")
    # 同 agent 子任务 c1 的 rich 胶囊：task 层 body（含 c1 的 USER_PROMPT + 段摘要）
    await mem.ingest(MemoryEvent(type=T.USER_PROMPT, scope=scope, content="c1 请求",
                                 timestamp=_BASE, role="user", metadata={"task_id": "c1"}), _pctx())
    await mem.ingest(MemoryEvent(type=T.TASK_COMPACT_SUMMARY, scope=scope, content="c1 段摘要",
                                 timestamp=_BASE + timedelta(seconds=1), role="assistant",
                                 metadata={"task_id": "c1"}), _pctx())
    # agent 层 finish 对：assistant{act_recap + finish 调用} / tool{task_summary}
    await mem.ingest(MemoryEvent(type=T.AGENT_CONVERSATION_TURN, scope=scope, content="c1 act_recap",
                                 timestamp=_BASE + timedelta(seconds=2), role="assistant",
                                 metadata={"origin_task_id": "c1", "parent_task_id": "root",
                                           "tool_calls": [{"id": "tc1", "name": "control:finish_task"}]}), _pctx())
    await mem.ingest(MemoryEvent(type=T.AGENT_CONVERSATION_TURN, scope=scope, content="c1 综合总结",
                                 timestamp=_BASE + timedelta(seconds=2), role="tool",
                                 metadata={"origin_task_id": "c1", "parent_task_id": "root",
                                           "tool_call_id": "tc1"}), _pctx())

    state = SimpleNamespace(scope=scope, agent=SimpleNamespace())
    ctx = SimpleNamespace(memory=mem, provider_ctx=_pctx())
    n = await demote_kept_capsules(state, ctx, {"c1"})
    assert n >= 2  # 至少折掉 task body 2 条 + assistant 槽

    body = await mem.recall_recent_by_agent(
        scope, [T.USER_PROMPT, T.TASK_COMPACT_SUMMARY], 100, _pctx())
    assert not any(r.metadata.get("task_id") == "c1" for r in body)  # task body 全删

    turns = await mem.recall_recent(scope, [T.AGENT_CONVERSATION_TURN], 100, _pctx())
    c1_turns = [r for r in turns if r.metadata.get("origin_task_id") == "c1"]
    # 只剩一条 tool 回填（lean），assistant 段被折
    assert len(c1_turns) == 1 and c1_turns[0].role == "tool"
    assert "c1 综合总结" in c1_turns[0].content
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd ctx-weft && uv run pytest tests/unit/test_demote_kept_capsules.py -q`
Expected: FAIL（`demote_kept_capsules` 不存在）

- [ ] **Step 3: 实现**

`compact.py` 加：

```python
async def demote_kept_capsules(state: LoopState, ctx: LoopContext, origin_ids: set) -> int:
    """L2：把 origin_ids 里本 agent 亲做的 rich 胶囊降级成 sub-agent lean 表示。
    - 删该 task 的 task 层 body（USER_PROMPT/段摘要/raw，metadata['task_id'] in origin_ids）。
    - agent 层 finish 对：supersede assistant 槽（act_recap + finish 调用），保留 tool 槽（综合总结回填）
      作 lean 表示。已无 assistant 槽（已 lean / 纯 dispatch 对）的单元跳过。
    无 LLM。返回 supersede 条数。"""
    memory = ctx.memory
    body = await memory.recall_recent_by_agent(state.scope, _TASK_BODY_TYPES, 2000, ctx.provider_ctx)
    turns = await memory.recall_recent(
        state.scope, [MemoryEventType.AGENT_CONVERSATION_TURN], 2000, ctx.provider_ctx)
    has_dispatch, has_finish = _dispatch_finish_sets(turns)

    ids: list = []
    for r in body:
        if r.metadata.get("task_id") in origin_ids:
            ids.append(r.id)   # 删 task 层 body（降级核心：丢交互细节）
    for r in turns:
        oid = r.metadata.get("origin_task_id")
        if oid not in origin_ids:
            continue
        # 仅降级「本 agent 亲做」单元（有 finish 回合）；纯 dispatch 对本就 lean，不动
        if oid in has_finish and r.role == "assistant":
            ids.append(r.id)   # 折 finish 对 assistant 槽，仅留 tool 槽回填
    if not ids:
        return 0
    await memory.supersede(ids, ctx.provider_ctx)
    return len(ids)
```

- [ ] **Step 4: 跑测试确认通过**

Run: `cd ctx-weft && uv run pytest tests/unit/test_demote_kept_capsules.py -q`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add src/ctx_weft/core/loop/steps/compact.py tests/unit/test_demote_kept_capsules.py
git commit -m "feat(compact): L2 demote_kept_capsules 同 agent rich 胶囊降级 lean"
```

---

### Task 6: `escalating_compact` —— 预算驱动 L1→L2→L3 升级编排

替换 `_compact_scope`：按预算升级，级间用 `_active_memory_tokens` 的 before/after 增量从传入的 `token_estimate` 累减，降到目标比率以下即停。`CompactStep` 与 `maybe_compact_before_dispatch` 都改调它。

**Files:**
- Modify: `src/ctx_weft/core/loop/steps/compact.py`（新 `escalating_compact` 替 `_compact_scope`；`CompactStep.execute`、`maybe_compact_before_dispatch` 改调）
- Test: `tests/unit/test_escalating_compact.py`

**Interfaces:**
- Consumes: `fold_root_experience(state, ctx, keep_last, summary_text) -> int`、`demote_kept_capsules(state, ctx, origin_ids) -> int`、`collapse_task_layer(state, ctx, keep_last, summary_text) -> int`、`summarize_for_compact(state, ctx, *, scope)`、`_active_memory_tokens`、`_count_root_residues`。
- Produces: `async def escalating_compact(state, ctx, *, token_estimate: int, trigger: str = "compact") -> list[Any]`——按需 L1→L2→L3，返回事件列表；无 `context_limit`（≤0）或估算已达标则空跑返回 `[]`。

- [ ] **Step 1: 写失败测试**

```python
# tests/unit/test_escalating_compact.py
from types import SimpleNamespace
import pytest

from ctx_weft.core.loop.steps import compact as cm

pytestmark = pytest.mark.asyncio


def _state(ratio_trigger=0.8, ratio_target=0.6, limit=1000):
    agent = SimpleNamespace(
        id="a",
        loop_config=SimpleNamespace(
            compact_keep_last=6, collapse_keep_last=3,
            compact_token_ratio=ratio_trigger, compact_target_ratio=ratio_target),
        loop_guard=SimpleNamespace(context_limit=limit))
    return SimpleNamespace(scope=SimpleNamespace(), task=SimpleNamespace(id="t1"),
                           agent=agent, session=SimpleNamespace(), extra={})


async def test_stops_after_l1_when_under_target(monkeypatch):
    calls = []
    # L1 折后活跃 token 从 900 掉到 500（< target 600）→ 不进 L2/L3
    seq = iter([900, 500])
    monkeypatch.setattr(cm, "_active_memory_tokens", lambda s, c: _anext(seq))
    monkeypatch.setattr(cm, "_count_root_residues", lambda s, c: _const(10))
    monkeypatch.setattr(cm, "summarize_for_compact", lambda s, c, *, scope="task": _const(f"sum-{scope}"))
    monkeypatch.setattr(cm, "fold_root_experience", lambda s, c, k, t: (calls.append("L1") or 5))
    monkeypatch.setattr(cm, "demote_kept_capsules", lambda s, c, o: (calls.append("L2") or 0))
    monkeypatch.setattr(cm, "collapse_task_layer", lambda s, c, k, t: (calls.append("L3") or 0))

    events = await cm.escalating_compact(_state(), SimpleNamespace(memory=None, provider_ctx=None),
                                         token_estimate=900, trigger="compact")
    assert calls == ["L1"]
    assert any(e.payload.get("layer") == "agent" for e in events)


async def test_escalates_l1_l2_l3(monkeypatch):
    calls = []
    seq = iter([900, 850, 820, 500])  # L1 后仍高、L2 后仍高、L3 后达标
    monkeypatch.setattr(cm, "_active_memory_tokens", lambda s, c: _anext(seq))
    monkeypatch.setattr(cm, "_count_root_residues", lambda s, c: _const(10))
    monkeypatch.setattr(cm, "summarize_for_compact", lambda s, c, *, scope="task": _const(f"sum-{scope}"))
    monkeypatch.setattr(cm, "fold_root_experience", lambda s, c, k, t: (calls.append("L1") or 3))
    monkeypatch.setattr(cm, "demote_kept_capsules", lambda s, c, o: (calls.append("L2") or 2))
    monkeypatch.setattr(cm, "collapse_task_layer", lambda s, c, k, t: (calls.append("L3") or 4))
    monkeypatch.setattr(cm, "_kept_origin_ids", lambda s, c, keep: _const({"c1"}))

    await cm.escalating_compact(_state(), SimpleNamespace(memory=None, provider_ctx=None),
                                token_estimate=900, trigger="compact")
    assert calls == ["L1", "L2", "L3"]


async def test_noop_when_no_context_limit(monkeypatch):
    st = _state(limit=0)
    events = await cm.escalating_compact(st, SimpleNamespace(memory=None, provider_ctx=None),
                                         token_estimate=900, trigger="compact")
    assert events == []


# 测试辅助：把常量/序列包装成 awaitable
async def _const(v):
    return v


async def _anext(it):
    return next(it)
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd ctx-weft && uv run pytest tests/unit/test_escalating_compact.py -q`
Expected: FAIL（`escalating_compact`、`_kept_origin_ids` 不存在）

- [ ] **Step 3: 实现**

`compact.py` 加辅助 + 主函数，并删除旧 `_compact_scope`（337-393 行）：

```python
async def _kept_origin_ids(state: LoopState, ctx: LoopContext, keep_last: int) -> set:
    """L1 折后仍保留的最近 keep_last 个顶层单元的 origin_task_id（L2 的降级对象）。"""
    recs = await ctx.memory.recall_recent(
        state.scope, [MemoryEventType.AGENT_CONVERSATION_TURN], 2000, ctx.provider_ctx)
    recs = normalize_legacy_dispatch(list(reversed(recs)))
    parent_of, first_ts = {}, {}
    has_dispatch, has_finish = _dispatch_finish_sets(recs)
    for r in recs:
        if r.type != MemoryEventType.AGENT_CONVERSATION_TURN:
            continue
        oid = r.metadata.get("origin_task_id")
        if oid is None:
            continue
        p = r.metadata.get("parent_task_id")
        if oid not in parent_of or (parent_of[oid] is None and p is not None):
            parent_of[oid] = p
        if oid not in first_ts or r.timestamp < first_ts[oid]:
            first_ts[oid] = r.timestamp
    origins = set(parent_of)
    active = {oid for oid in has_dispatch if oid not in has_finish}
    top = [oid for oid, pid in parent_of.items()
           if oid not in active and (pid is None or pid not in origins)]
    top.sort(key=lambda oid: first_ts[oid])
    return set(top[-keep_last:]) if keep_last > 0 else set()


async def escalating_compact(
    state: LoopState, ctx: LoopContext, *, token_estimate: int, trigger: str = "compact"
) -> list[Any]:
    """预算驱动升级式 compact（替 _compact_scope）：L1 agent 折 → L2 rich→lean → L3 坍当前 task，
    每级后用 _active_memory_tokens 的增量从 token_estimate 累减，降到 target 以下即停。
    级间不完整重装配（Q4=c，调用方进 act 前重装配一次校正）。无 context_limit 或已达标 → []。"""
    agent = state.agent
    lc = agent.loop_config
    context_limit = agent.loop_guard.context_limit
    if context_limit <= 0:
        return []
    target_ratio = lc.compact_target_ratio if getattr(lc, "compact_target_ratio", 0.0) > 0 \
        else lc.compact_token_ratio
    target_tokens = int(context_limit * target_ratio)
    keep_last = lc.compact_keep_last
    collapse_keep = getattr(lc, "collapse_keep_last", keep_last)

    est = token_estimate
    if est < target_tokens:
        return []
    events: list[Any] = [make_event(state, EventType.MEMORY_COMPACT_STARTED, payload={
        "task_id": state.task.id, "agent_id": agent.id, "trigger": trigger,
        "token_estimate": est, "target_tokens": target_tokens})]

    async def _apply(level_coro):
        """跑一级折叠，用活跃 token before/after 增量累减 est。返回 (superseded_count, freed)。"""
        nonlocal est
        before = await _active_memory_tokens(state, ctx)
        n = await level_coro
        after = await _active_memory_tokens(state, ctx)
        freed = max(0, before - after)
        est -= freed
        return n, freed

    # L1 · agent 折（仅当有可折顶层单元）
    if await _count_root_residues(state, ctx) > keep_last:
        summary_agent = await summarize_for_compact(state, ctx, scope="agent")
        n, freed = await _apply(fold_root_experience(state, ctx, keep_last, summary_agent))
        if n:
            events.append(make_event(state, EventType.MEMORY_COMPACTED, payload={
                "superseded_count": n, "layer": "agent", "source": "root_experience",
                "trigger": trigger, "freed_tokens": freed}))
    if est < target_tokens:
        return events

    # L2 · 保留的同 agent rich 胶囊降级 lean（无 LLM）
    kept = await _kept_origin_ids(state, ctx, keep_last)
    if kept:
        n, freed = await _apply(demote_kept_capsules(state, ctx, kept))
        if n:
            events.append(make_event(state, EventType.MEMORY_COMPACTED, payload={
                "superseded_count": n, "layer": "agent", "source": "demote_lean",
                "trigger": trigger, "freed_tokens": freed}))
    if est < target_tokens:
        return events

    # L3 · 坍缩当前 task（段摘要坍成更少，保 collapse_keep 条）
    summary_task = await summarize_for_compact(state, ctx, scope="task")
    n, freed = await _apply(collapse_task_layer(state, ctx, collapse_keep, summary_task))
    if n:
        events.append(make_event(state, EventType.MEMORY_COMPACTED, payload={
            "superseded_count": n, "layer": "task", "source": "collapse",
            "trigger": trigger, "freed_tokens": freed}))
    logger.info("escalating_compact[%s]: agent=%s task=%s est→%d target=%d",
                trigger, agent.id, state.task.id, est, target_tokens)
    return events
```

改 `CompactStep.execute`（396-405 行）与 `maybe_compact_before_dispatch`（末行 171）：

```python
# CompactStep.execute
        return StepOutcome(
            next_step=None,
            events=await escalating_compact(
                state, ctx, token_estimate=state.agent.loop_guard.context_tokens, trigger="compact"),
        )

# maybe_compact_before_dispatch 末行
    return await escalating_compact(state, ctx, token_estimate=tokens, trigger="pre_dispatch")
```

- [ ] **Step 4: 跑测试确认通过**

Run: `cd ctx-weft && uv run pytest tests/unit/test_escalating_compact.py -q`
Expected: PASS

- [ ] **Step 5: 迁移旧 `_compact_scope` 测试**

旧用例（`test_task_collapse.py::test_compact_scope_task_uses_collapse_keep_last`、`test_compaction.py`、`test_predispatch_compact.py`、`test_compact_layers.py`）断言旧双阈值并行折。改为断言 `escalating_compact` 的升级行为（用上面 monkeypatch 序列风格）。删不再成立的双阈值并行断言。逐个跑至 PASS。

Run: `cd ctx-weft && uv run pytest tests/unit/test_task_collapse.py tests/unit/test_compaction.py tests/unit/test_predispatch_compact.py tests/unit/test_compact_layers.py -q`
Expected: PASS

- [ ] **Step 6: 提交**

```bash
git add src/ctx_weft/core/loop/steps/compact.py tests/unit/
git commit -m "feat(compact): escalating_compact 预算驱动 L1→L2→L3 升级编排替 _compact_scope"
```

---

### Task 7: prepare 纯预算触发 + 升级 compact + 末次重装配

`_should_compact` 去掉消息条数分支，只留 token 比率。命中后调 `escalating_compact`（传本轮 token_estimate），再完整重装配一次。

**Files:**
- Modify: `src/ctx_weft/core/loop/steps/prepare.py`（103-109 行 compact 触发块；161-207 行 `_should_compact`）
- Test: `tests/unit/test_prepare_budget_compact.py`

**Interfaces:**
- Consumes: `escalating_compact(state, ctx, *, token_estimate, trigger)`。
- Produces: `PrepareStep._should_compact(state, ctx, token_estimate) -> bool`（纯预算）。

- [ ] **Step 1: 写失败测试**

```python
# tests/unit/test_prepare_budget_compact.py
from types import SimpleNamespace
import pytest

from ctx_weft.core.loop.steps.prepare import PrepareStep

pytestmark = pytest.mark.asyncio


async def test_should_compact_pure_budget_true():
    state = SimpleNamespace(agent=SimpleNamespace(
        loop_config=SimpleNamespace(compact_token_ratio=0.8),
        loop_guard=SimpleNamespace(context_limit=1000)))
    ctx = SimpleNamespace()
    assert await PrepareStep()._should_compact(state, ctx, token_estimate=850) is True


async def test_should_compact_below_ratio_false_even_with_many_messages():
    # 消息条数门控已废：token 低就不压，无论消息多少
    state = SimpleNamespace(agent=SimpleNamespace(
        loop_config=SimpleNamespace(compact_token_ratio=0.8),
        loop_guard=SimpleNamespace(context_limit=1000)))
    ctx = SimpleNamespace()
    assert await PrepareStep()._should_compact(state, ctx, token_estimate=200) is False
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd ctx-weft && uv run pytest tests/unit/test_prepare_budget_compact.py -q`
Expected: FAIL（现 `_should_compact` 还含 delta 分支/签名不符或仍返回 True）

- [ ] **Step 3: 实现**

`prepare.py` 把 `_should_compact`（161-207 行）整体替换为纯预算：

```python
    async def _should_compact(
        self, state: LoopState, ctx: LoopContext, token_estimate: int
    ) -> bool:
        """纯预算触发（spec 2026-07-01 §3.6）：token 估算 / context_limit ≥ compact_token_ratio。
        消息条数门控（compact_message_delta）已废。"""
        loop_config = state.agent.loop_config
        context_limit = state.agent.loop_guard.context_limit
        if context_limit > 0 and token_estimate > 0:
            return token_estimate / context_limit >= loop_config.compact_token_ratio
        return False
```

把触发块（103-109 行）改为调 `escalating_compact` 并重装配：

```python
        # ── 5. compact 触发：命中则跑升级式 compact，再在压缩后 memory 上重装配一次（Q4=c 校正）──
        if await self._should_compact(state, ctx, token_estimate):
            from ctx_weft.core.loop.steps.compact import escalating_compact
            for ev in await escalating_compact(state, ctx, token_estimate=token_estimate, trigger="compact"):
                await ctx.event_bus.emit(ev)
            prompt = await _assemble()
```

（若 `CompactStep` 已无其他调用者，保留类不影响；`runtime.py` 的注册与直调仍用 `CompactStep.execute` → 已在 Task 6 改为调 `escalating_compact`，无需再动。）

- [ ] **Step 4: 跑测试确认通过**

Run: `cd ctx-weft && uv run pytest tests/unit/test_prepare_budget_compact.py -q`
Expected: PASS

- [ ] **Step 5: 回归**

Run: `cd ctx-weft && uv run pytest tests/unit/test_compact_step_inline.py -q`
Expected: 更新其中断言旧 `_should_compact` 消息条数行为的用例后 PASS。

- [ ] **Step 6: 提交**

```bash
git add src/ctx_weft/core/loop/steps/prepare.py tests/unit/test_prepare_budget_compact.py tests/unit/test_compact_step_inline.py
git commit -m "feat(compact): prepare 纯预算触发 + 调 escalating_compact + 末次重装配"
```

---

### Task 8: pre_dispatch 复用升级 compact（收尾 + 全量回归）

`maybe_compact_before_dispatch` 已在 Task 6 改调 `escalating_compact(trigger="pre_dispatch")`，本 Task 补端到端测试并跑全量回归。

**Files:**
- Test: `tests/unit/test_predispatch_compact.py`（补用例）
- Verify: 全 `tests/unit`

**Interfaces:**
- Consumes: `maybe_compact_before_dispatch(state, ctx, prompt_tokens) -> list[Any]`（内部走 `escalating_compact`）。

- [ ] **Step 1: 写失败/补充测试**

```python
# 追加到 tests/unit/test_predispatch_compact.py
async def test_predispatch_uses_escalating_compact(monkeypatch):
    from types import SimpleNamespace
    from ctx_weft.core.loop.steps import compact as cm

    seen = {}

    async def _fake_esc(state, ctx, *, token_estimate, trigger):
        seen["trigger"] = trigger
        seen["est"] = token_estimate
        return []

    monkeypatch.setattr(cm, "escalating_compact", _fake_esc)
    agent = SimpleNamespace(
        loop_config=SimpleNamespace(predispatch_compact_token_ratio=0.5),
        loop_guard=SimpleNamespace(context_limit=1000, context_tokens=0))
    state = SimpleNamespace(agent=agent, scope=SimpleNamespace(), task=SimpleNamespace(id="t1"))
    ctx = SimpleNamespace()
    await cm.maybe_compact_before_dispatch(state, ctx, prompt_tokens=800)
    assert seen["trigger"] == "pre_dispatch" and seen["est"] == 800
```

- [ ] **Step 2: 跑测试确认（先 fail 后 pass）**

Run: `cd ctx-weft && uv run pytest tests/unit/test_predispatch_compact.py::test_predispatch_uses_escalating_compact -q`
Expected: PASS（Task 6 已接线；若 fail 检查 `maybe_compact_before_dispatch` 是否漏改）

- [ ] **Step 3: 全量回归**

Run: `cd ctx-weft && uv run pytest tests/unit -q`
Expected: PASS（全绿）。若有零散 FAIL，均为断言旧 compact 行为的用例，按新契约更新。

- [ ] **Step 4: 提交**

```bash
git add tests/unit/test_predispatch_compact.py
git commit -m "test(compact): pre_dispatch 复用 escalating_compact 端到端 + 全量回归"
```

---

### Task 9: 全流程集成测试（各场景端到端）

用真实 runtime（`run_single_task`）与真实 memory 驱动整条 prepare→act→observe→finalize，覆盖：机械退出 retry 段折、正常收尾回归、预算升级 compact 对真实记忆。放 `tests/integration/`，复用 `test_minimal_loop` 的 `InMemoryTemplateResolver`/`make_echo_template`。

**Files:**
- Create: `tests/integration/test_compact_flow_e2e.py`

**Interfaces:**
- Consumes: `CtxWeftRuntime.run_single_task(template_id, user_prompt) -> (handle, state)`；`MockLLMAdapter`/`MockResponse`；`escalating_compact`；`InMemoryMemoryProvider`。

**可靠性设计（执行者须知）：**
- `context_limit` 由 `MockLLMAdapter(context_limit=...)` 注入；act turn1 的 `prompt_tokens = estimate_tokens(system+messages)`（mock.py:85-89），设 `context_limit=20` → `0.8*20=16` 必被真实 prompt 超过 → context_limit 命中。
- 用**无 `observe` facet** 的模板 → `_should_use_llm` 在「无 ROLE」分支即返回 False → 规则 observe，**无需脚本化 observe LLM**，仅需 1 条 act response。
- `capability_refs=[]` → 无 bound_capabilities → recognize_intent 跳过（prepare.py:112），不额外消耗 response。
- `run_single_task` 只跑一个 run（retry 后 task 停 PENDING，不自动重排）→ 直接断言 memory，避开多 attempt 脚本化。

- [ ] **Step 1: 写测试**

```python
# tests/integration/test_compact_flow_e2e.py
from types import SimpleNamespace
from datetime import datetime, timedelta, UTC
import pytest

from ctx_weft.core import CtxWeftRuntime
from ctx_weft.providers.llm.mock import MockLLMAdapter, MockResponse
from ctx_weft.providers.memory_blackboard import InMemoryMemoryProvider
from ctx_weft.protocols import (
    AgentTemplate, IdentityFacet, LoopConfig, MemoryConfig,
    MemoryEvent, MemoryEventType as T, MemoryScope, ProviderContext,
)
from ctx_weft.core.loop.steps import compact as cm
from tests.integration.test_minimal_loop import InMemoryTemplateResolver

pytestmark = pytest.mark.asyncio
_BASE = datetime(2026, 7, 1, tzinfo=UTC)


def _act_only_template() -> AgentTemplate:
    """act facet only（无 observe）→ 机械退出走规则 observe，不需脚本化 observe LLM。"""
    return AgentTemplate(
        id="tpl_actonly", name="actonly", version="0.1.0",
        identity={"act": IdentityFacet(text="You are a worker. Keep working on the task.")},
        description="e2e", capability_refs=[],
        memory_config=MemoryConfig(), loop_config=LoopConfig(),
    )


async def test_context_limit_retry_folds_segment_e2e():
    resolver = InMemoryTemplateResolver()
    resolver.register(_act_only_template())
    # context_limit=20 → 0.8*20=16 tokens 阈值，真实 prompt 必超 → act turn1 context_limit 命中
    llm = MockLLMAdapter(responses=[MockResponse(text="partial work, not done yet")],
                         context_limit=20)
    runtime = CtxWeftRuntime(llm=llm, template_resolver=resolver)
    runtime.providers.register_memory(InMemoryMemoryProvider())

    handle, state = await runtime.run_single_task(
        template_id="tpl_actonly", user_prompt="do a long task")

    # 机械退出 → retry（非终态），本轮 attempt 折成段摘要、raw 删除、USER_PROMPT 保留
    assert state.verdict is not None and state.verdict.task_outcome == "retry"
    assert state.task.status == "PENDING"
    assert not getattr(state.task, "process_report", None)  # retry 不写 process_report

    mem = runtime.providers.get_memory()
    pctx = ProviderContext(session_id=state.session.id, agent_id=state.agent.id)
    n_summary = await mem.count_recent(state.scope, [T.TASK_COMPACT_SUMMARY], pctx)
    n_user = await mem.count_recent(state.scope, [T.USER_PROMPT], pctx)
    n_raw = await mem.count_recent(state.scope, [T.LLM_RESPONSE], pctx)
    assert n_summary >= 1, "retry 应写至少一条 TASK_COMPACT_SUMMARY 段摘要"
    assert n_user >= 1, "USER_PROMPT 锚必须保留"
    assert n_raw == 0, "本轮 attempt 的 LLM_RESPONSE raw 应被折叠删除"


async def test_normal_finish_still_works_e2e():
    """正常收尾回归：整条 loop 仍跑通到 FINISHED（Task 3 去 process_report 渲染不破主流程）。"""
    resolver = InMemoryTemplateResolver()
    resolver.register(_act_only_template())
    llm = MockLLMAdapter(responses=[MockResponse(text="Here is the final answer.")])  # 正常 context_limit
    runtime = CtxWeftRuntime(llm=llm, template_resolver=resolver)
    runtime.providers.register_memory(InMemoryMemoryProvider())

    handle, state = await runtime.run_single_task(
        template_id="tpl_actonly", user_prompt="say hi")

    assert state.task.status == "FINISHED"
    assert state.verdict.task_outcome == "success"


async def test_escalating_compact_shrinks_real_memory(monkeypatch):
    """预算升级 compact 对真实 memory：seed 多个结束胶囊 → escalating_compact 至少折 agent 层，
    活跃记忆 token 下降；summarize_for_compact 打桩免真实 LLM。"""
    async def _fake_summ(state, ctx, *, scope="task"):
        return f"[summary-{scope}]"
    monkeypatch.setattr(cm, "summarize_for_compact", _fake_summ)

    mem = InMemoryMemoryProvider()
    scope = MemoryScope(session_id="s", task_id="root", agent_id="a")
    pctx = ProviderContext(session_id="s", tenant_id="tn")

    # seed 5 个结束顶层单元（finish 对：assistant + tool），parent=None → 顶层
    for i in range(5):
        oid = f"c{i}"
        await mem.ingest(MemoryEvent(type=T.AGENT_CONVERSATION_TURN, scope=scope,
            content="act_recap " + "x" * 200, timestamp=_BASE + timedelta(seconds=2 * i),
            role="assistant", metadata={"origin_task_id": oid, "parent_task_id": None,
            "tool_calls": [{"id": f"tc{i}", "name": "control:finish_task"}]}), pctx)
        await mem.ingest(MemoryEvent(type=T.AGENT_CONVERSATION_TURN, scope=scope,
            content="summary " + "y" * 200, timestamp=_BASE + timedelta(seconds=2 * i + 1),
            role="tool", metadata={"origin_task_id": oid, "parent_task_id": None,
            "tool_call_id": f"tc{i}"}), pctx)

    agent = SimpleNamespace(id="a", loop_config=SimpleNamespace(
        compact_keep_last=2, collapse_keep_last=3,
        compact_token_ratio=0.8, compact_target_ratio=0.01),  # target 极低 → 尽量升级
        loop_guard=SimpleNamespace(context_limit=100))
    state = SimpleNamespace(scope=scope, task=SimpleNamespace(id="root"), agent=agent,
                            session=SimpleNamespace(id="s"), extra={})
    ctx = SimpleNamespace(memory=mem, provider_ctx=pctx, event_bus=None, task_manager=None)

    before = await cm._active_memory_tokens(state, ctx)
    events = await cm.escalating_compact(state, ctx, token_estimate=before, trigger="compact")
    after = await cm._active_memory_tokens(state, ctx)

    # L1 agent 折触发（5 个顶层 > compact_keep_last=2）→ 活跃 token 下降
    assert after < before
    assert any(e.payload.get("layer") == "agent" and e.payload.get("source") == "root_experience"
               for e in events)
```

- [ ] **Step 2: 跑测试**

Run: `cd ctx-weft && uv run pytest tests/integration/test_compact_flow_e2e.py -q`
Expected: 三条 PASS。若 `test_context_limit_retry_folds_segment_e2e` 未命中 context_limit（prompt 太短），把 `context_limit` 再调低（如 10）；若 `run_single_task` 因 interactive 模式挂起，改用 `_act_only_template` 的 auto 语义确认（`run_single_task` 默认 auto，参见 test_minimal_loop）。

- [ ] **Step 3: 提交**

```bash
git add tests/integration/test_compact_flow_e2e.py
git commit -m "test(compact): 全流程集成测试——context_limit retry 段折 / 正常收尾 / 预算升级折真实记忆"
```

---

## Self-Review

**Spec coverage：**
- §3.1 retry 前台段折 → Task 2；不写 process_report → Task 3。✓
- §3.2 interactive background observe 不变 → 无改动（Task 2 仅加防御条件，回归覆盖）。✓
- §3.3 L1/L2/L3 → Task 5（L2）、Task 6（编排，复用既有 L1 `fold_root_experience` / L3 `collapse_task_layer`）。✓
- §3.4 双比率 → Task 1（配置）+ Task 6（target_tokens）。✓
- §3.5 分析式粗估 + 末次重装配 → Task 4（代理）+ Task 6（累减）+ Task 7（重装配）。✓
- §3.6 纯预算 + 废条数门控 + keep_last 保留底线 → Task 1 + Task 7。✓
- §3.6 pre_dispatch 复用 → Task 6 接线 + Task 8 验证。✓
- §3.7 composer 去重 → Task 3。✓
- §4 全流程各场景（retry 段折 / 正常收尾 / 预算升级折真实记忆）→ Task 9 集成测试。✓

**Placeholder scan：** 无 TBD/TODO；各 Step 均含真实代码/命令/预期。回归步骤（Task 2/3/6/7 的迁移旧测试）给了明确改法方向而非空泛「更新测试」——因旧用例数量多、逐条列出不经济，故指明契约变化点由执行者据此改；如需更细可在执行时按失败信息逐个处理。

**Type consistency：**
- `escalating_compact(state, ctx, *, token_estimate: int, trigger: str)` —— Task 6 定义，Task 7/8 一致引用。✓
- `_fold_retry_segment(state, ctx, verdict, events)` —— Task 2 定义与调用一致。✓
- `demote_kept_capsules(state, ctx, origin_ids: set)` —— Task 5 定义、Task 6 调用一致。✓
- `_active_memory_tokens(state, ctx)` / `_kept_origin_ids(state, ctx, keep_last)` —— Task 4/6 一致。✓
- `apply_compact(... keep_last, layer=MemoryLayer.TASK, protect_types=(USER_PROMPT,))` —— 与 `memory.py:278` 签名一致。✓

**已知执行注意点（非 placeholder，供执行者留意）：**
- Task 6 `_apply` 内嵌 helper 用了 `nonlocal est`；monkeypatch 测试用序列桩 `_active_memory_tokens`，实现里每级调用两次（before/after），测试序列长度须匹配实际调用次数——按实际级数供值。
- Task 5 的 lean 回填「前缀化」若需 `[outcome=fail]` 前缀，取自 finish 对 tool 槽既有内容即可，本计划保留 tool 槽原文（已含前缀）不重写，降低风险。
