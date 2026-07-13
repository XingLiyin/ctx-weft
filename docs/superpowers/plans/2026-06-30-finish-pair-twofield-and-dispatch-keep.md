# finish 对两段化 + 同 agent 派发对保留 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把 finish 对改成「assistant 诚实复述上一段 act（act_recap）+ tool 承载整段综合总结（task_summary）」两段，并让同 agent 派发对保留 delegate + 写时间戳锚到派发时刻的静态结果，根治派发结果被子 body 劈开的 LLM 400。

**Architecture:** 两个 observe 工具（`report_task_outcome` 内联 / `collect_process_report` 后台）产 `act_recap` + `task_summary` 两字段；`Verdict` 携带二者；`finalize._synthesize_dispatch_pair` 写两段 finish 对；`finalize._close_one` 的 same_agent 分支不再 supersede、改写 back-date 静态结果，cross_agent 结果改用 `task_summary`；`background_observe` 的 close 路径与 A1 替换机制携两字段。全在 `ctx-weft/` core，无持久 enum/schema 变更。

**Tech Stack:** Python 3.11，`uv run pytest`，dataclass，asyncio。

## Global Constraints

- 所有改动落 `src/ctx_weft/`（core）；**不**改 memory 写入的 role/layer，host postgres provider 不涉及。
- 测试一律 `uv run pytest`（`pyproject` 已配 `pythonpath=["."]`）。provider 测试缺 `psutil`/`pyyaml` 时加 `--with psutil --with pyyaml`；本计划涉及的 observe/finalize/composer/background 测试**不**依赖它们。
- **无新增/删除 `MemoryEventType` enum，无新增持久 schema**——`Verdict` 是瞬态内存对象、`Task.task_summary` 是可选默认 None 字段，前向兼容（存量数据免迁移、读路径不变）。
- `act_recap` / `task_summary` 字段名全仓一致（不得在某处写成 `summary` / `act_summary`）。
- TDD：每个 Task 先写失败测试 → 跑红 → 最小实现 → 跑绿 → 提交。

---

### Task 1: `Verdict` 改名 `act_recap` + 加 `task_summary`；`Task.task_summary` 字段

**Files:**
- Modify: `src/ctx_weft/core/state/models.py`（`Task` 加字段）
- Modify: `src/ctx_weft/core/loop/steps/observe.py`（`Verdict` 定义 + 全部 `verdict.summary` 引用）
- Modify: `src/ctx_weft/core/loop/steps/finalize.py:254`（`verdict.summary` → `verdict.act_recap`）
- Test: `tests/unit/test_observe_outcomes.py`

**Interfaces:**
- Produces: `Verdict(task_outcome: str, act_recap: str, task_summary: str = "", reported: bool = False)`；`Task.task_summary: str | None = None`。

- [ ] **Step 1: Write the failing test**

在 `tests/unit/test_observe_outcomes.py` 末尾追加：

```python
def test_verdict_has_act_recap_and_task_summary_fields():
    from ctx_weft.core.loop.steps.observe import Verdict
    v = Verdict(task_outcome="success", act_recap="did X", task_summary="whole journey")
    assert v.act_recap == "did X"
    assert v.task_summary == "whole journey"
    assert v.reported is False
    # task_summary 默认空
    assert Verdict(task_outcome="retry", act_recap="r").task_summary == ""


def test_task_model_has_task_summary_field():
    from ctx_weft.core.state.models import Task
    t = Task(id="t1", session_id="s1")
    assert t.task_summary is None
    t.task_summary = "comprehensive"
    assert t.task_summary == "comprehensive"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/unit/test_observe_outcomes.py::test_verdict_has_act_recap_and_task_summary_fields tests/unit/test_observe_outcomes.py::test_task_model_has_task_summary_field -v`
Expected: FAIL（`Verdict.__init__` 不识别 `act_recap`；`Task` 无 `task_summary`）

- [ ] **Step 3: Implement**

`src/ctx_weft/core/state/models.py`，在 `Task` 的 `observer_outcome: str | None = None`（约 line 209）之后加：

```python
    # observer 产出的整段综合总结（执行历程+结果）→ finish 对 tool 槽（spec 2026-06-30）。
    task_summary: str | None = None
```

`src/ctx_weft/core/loop/steps/observe.py`，`Verdict`（约 line 209-214）改为：

```python
@dataclass
class Verdict:
    """Observer 输出（三态）。"""
    task_outcome: str   # "retry" | "success" | "fail"
    act_recap: str      # 诚实复述上一轮 act 做了什么 → finish 对 assistant；retry 作 Current Progress
    task_summary: str = ""  # 整段综合总结（执行历程+结果）→ finish 对 tool 槽（仅终态有意义）
    reported: bool = False  # 本轮是否真的走成 report_task_outcome；压缩摘要据此取信
```

同文件改全部 `verdict.summary` / `summary=` 引用：
- line 258：`"summary_length": len(verdict.summary),` → `"summary_length": len(verdict.act_recap),`
- line 316-320 的 `Verdict(... summary=state.task.process_report or last_text[:500], reported=True)` →
  ```python
            return Verdict(
                task_outcome=state.task.observer_outcome or "success",
                act_recap=state.task.process_report or last_text[:500],
                task_summary=state.task.task_summary or "",
                reported=True,
            )
  ```
- line 336：`Verdict(task_outcome="fail", summary="[No actor execution recorded]")` → `... act_recap="[No actor execution recorded]")`
- line 357：`Verdict(task_outcome=outcome, summary=" ".join(lines))` → `... act_recap=" ".join(lines))`
- line 419-420：
  ```python
        if verdict.reported and verdict.act_recap:
            summary = verdict.act_recap
  ```

`src/ctx_weft/core/loop/steps/finalize.py:254`：`summary = verdict.summary if verdict else ""` → `summary = verdict.act_recap if verdict else ""`

- [ ] **Step 4: Run tests to verify they pass + suite green**

Run: `uv run pytest tests/unit/test_observe_outcomes.py tests/unit/test_finalize.py -v`
Expected: PASS（含新两条）。若有别处测试直接构造 `Verdict(summary=...)` 报红，一并改名为 `act_recap=`。

- [ ] **Step 5: Commit**

```bash
git add src/ctx_weft/core/state/models.py src/ctx_weft/core/loop/steps/observe.py src/ctx_weft/core/loop/steps/finalize.py tests/unit/test_observe_outcomes.py
git commit -m "refactor(observe): Verdict.summary→act_recap + 加 task_summary;Task 加 task_summary 字段"
```

---

### Task 2: 内联 observe 产两字段（`report_task_outcome` + `run_observe_react` 返回 ControlResult + verdict 装配）

**Files:**
- Modify: `src/ctx_weft/core/orchestrator/control_capability.py`（`report_task_outcome`）
- Modify: `src/ctx_weft/core/loop/steps/observe.py`（`run_observe_react` 返回 ControlResult；`_llm_observe` 装配；`_apply_assessment` 写 task_summary）
- Test: `tests/unit/test_observe_outcomes.py`、`tests/unit/test_observe_react_helper.py`

**Interfaces:**
- Consumes: `Verdict(..., act_recap, task_summary)`、`Task.task_summary`（Task 1）。
- Produces: `report_task_outcome(task_status, act_recap, task_summary="", task_failure_reason="", task_reviews=None, next_step_hint="")`；`run_observe_react(...) -> tuple[ControlResult | None, str]`（terminal 工具的完整 ControlResult，未调用时 None）。

- [ ] **Step 1: Write the failing test**

`tests/unit/test_observe_outcomes.py` 追加（按现有该文件构造 `ControlContext` 的方式取 fixture；下例假设有 `make_ctx(task)` helper，无则参照文件内既有 report_task_outcome 测试的构造）：

```python
def test_report_task_outcome_writes_act_recap_and_task_summary(make_ctx):
    from ctx_weft.core.orchestrator.control_capability import report_task_outcome
    task = make_ctx.task  # 视文件内既有 helper 调整
    report_task_outcome(
        task_status="success",
        act_recap="本轮我创建了 skill 文件并验证",
        task_summary="整段：看模板→写 SKILL.md→写脚本→验证，已就绪",
        ctx=make_ctx,
    )
    assert task.process_report == "本轮我创建了 skill 文件并验证"
    assert task.task_summary == "整段：看模板→写 SKILL.md→写脚本→验证，已就绪"
    assert task.observer_outcome == "success"
```

`tests/unit/test_observe_react_helper.py` 追加（参照该文件既有的 run_observe_react 调用方式构造 fake gateway/LLM）：

```python
async def test_run_observe_react_returns_terminal_controlresult(...):
    # 让 fake LLM 调用 terminal 工具，工具返回 ControlResult(content="recap", metadata={"task_summary": "sum"})
    result, last_text = await run_observe_react(..., terminal_tool_name=TERMINAL)
    assert result is not None
    assert result.content == "recap"
    assert result.metadata.get("task_summary") == "sum"
```

- [ ] **Step 2: Run to verify fail**

Run: `uv run pytest tests/unit/test_observe_outcomes.py -k act_recap tests/unit/test_observe_react_helper.py -k controlresult -v`
Expected: FAIL（`report_task_outcome` 无 `act_recap` 参数；`run_observe_react` 返回 str 而非 ControlResult）

- [ ] **Step 3: Implement**

`control_capability.py` `report_task_outcome`（line 360-452）：
- 参数 `task_process_report` 改名 `act_recap`（Annotated 文案改为「诚实复述上一轮 act 做了什么：改了/产出了什么、调了哪些工具、是否失败。第一人称、忠于实际执行，只管最后这一段。Written to memory，retry 时作下一轮 Current Progress。」）。
- 在 `task_failure_reason` 之前新增参数：
  ```python
      task_summary: Annotated[
          str,
          "Required when task_status is 'success' or 'fail': a CONCISE process report of the WHOLE task — "
          "the important steps taken and lessons/experience, incorporating any sub-task results. "
          "Keep it high-signal, NOT a verbose blow-by-blow. This is NOT the final output: the final "
          "deliverable shown to the user goes in finish_task's `result`, not here. Leave empty for 'retry'.",
      ] = "",
  ```
- 函数体内把 `task_process_report` 全部改名 `act_recap`（含 `next_step_hint` 拼接、success 护栏 `_hint` 拼接、`task.process_report = act_recap`、最后 `content=f"Assessment recorded: ... {act_recap}{review_msg}"`）。
- 在 `task.process_report = act_recap` 之后加一行：`task.task_summary = task_summary`。

`observe.py` `run_observe_react`（line 167-194）：
```python
        terminal_result = None
        for tc in tool_calls:
            if ctx.capability_gateway is not None:
                result = await ctx.capability_gateway.invoke(
                    tool_name=tc.name, arguments=tc.arguments,
                    state=state, ctx=ctx, tool_call_id=tc.id,
                )
                content = result.content
                if tc.name == terminal_tool_name:
                    terminal_result = result
            else:
                logger.warning("run_observe_react: no CapabilityGateway for tool '%s'", tc.name)
                content = f"[Error: CapabilityGateway not configured, tool '{tc.name}' skipped]"
            current_messages.append(LLMMessage(role="tool", content=content, tool_call_id=tc.id))

        if terminal_result is not None:
            return terminal_result, last_text

    return None, last_text
```
并更新 docstring（line 79-80）：「返回 (terminal_result, last_text)：terminal_result — terminal_tool 被调用时的完整 ControlResult（未调用则 None）。」

`observe.py` `_llm_observe`（line 304-323）：
```python
        terminal_result, last_text = await run_observe_react(
            state, ctx,
            system=prompt.system, messages=list(prompt.messages), tools=prompt.tools,
            request_id_prefix=f"obs_{agent.id}_{state.sequence_counter}",
            max_rounds=max_rounds, terminal_tool_name=REPORT_TASK_OUTCOME_NAME,
        )
        if terminal_result is not None:
            # report_task_outcome 已写 task.observer_outcome / task.process_report / task.task_summary
            return Verdict(
                task_outcome=state.task.observer_outcome or "success",
                act_recap=state.task.process_report or last_text[:500],
                task_summary=state.task.task_summary or "",
                reported=True,
            )
        logger.warning("ObserveStep: LLM did not call report_task_outcome in %d rounds, falling back to rules", max_rounds)
        return self._rule_observe(state)
```

`observe.py` `_apply_assessment`（line 362-372），在 `task.actor_done = True` 之前加：
```python
        task.task_summary = verdict.task_summary
```

- [ ] **Step 4: Run to verify pass**

Run: `uv run pytest tests/unit/test_observe_outcomes.py tests/unit/test_observe_react_helper.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/ctx_weft/core/orchestrator/control_capability.py src/ctx_weft/core/loop/steps/observe.py tests/unit/test_observe_outcomes.py tests/unit/test_observe_react_helper.py
git commit -m "feat(observe): report_task_outcome 产 act_recap+task_summary;run_observe_react 返回 ControlResult"
```

---

### Task 3: finish 对两段化 端到端（synthesize + 后台 A1 替换 + collect_process_report）

**Files:**
- Modify: `src/ctx_weft/core/loop/steps/finalize.py`（`finalize_task_memory`/`_close_one`/`_synthesize_dispatch_pair` 签名与两段写入 + `_finish_tool_text` helper + `FinalizeStep.execute` 透传）
- Modify: `src/ctx_weft/core/loop/steps/background_observe.py`（`_close_report` 二元组、`_replace_finish_report` 换两条、`_run_background_observe` 解析两字段）
- Modify: `src/ctx_weft/core/orchestrator/control_capability.py`（`collect_process_report` 两字段）
- Test: `tests/unit/test_finalize.py`、`tests/unit/test_close_process_report_a1.py`、`tests/unit/test_background_observe.py`

**Interfaces:**
- Consumes: `Verdict.act_recap/task_summary`、`Task.task_summary`、`run_observe_react -> ControlResult`（Task 2）。
- Produces:
  - `_synthesize_dispatch_pair(memory, scope, task, act_recap: str, task_summary: str, outcome: str, provider_ctx)`
  - `_finish_tool_text(task_summary: str, act_recap: str, outputs_text: str, outcome: str) -> str`
  - `finalize_task_memory(memory, state, task, mem_content, outcome, ctx, *, act_recap: str, task_summary: str)`
  - `_close_one(..., *, short, act_recap: str, task_summary: str)`
  - `pop_close_report(task_id) -> tuple[str, str] | None`（(act_recap, task_summary)）
  - `_replace_finish_report(memory, provider_ctx, scope, task_id, tool_call_id, act_recap: str, task_summary: str, outcome)`
  - `collect_process_report(act_recap, task_summary="") -> ControlResult(content=act_recap, metadata={"task_summary": ...})`

- [ ] **Step 1: Write the failing test**

`tests/unit/test_finalize.py` 追加（参照该文件既有的 in-memory provider + Task + state 构造）：

```python
async def test_synthesize_dispatch_pair_two_segments(mem, scope, task, provider_ctx):
    from ctx_weft.core.loop.steps.finalize import _synthesize_dispatch_pair
    from ctx_weft.protocols import MemoryEventType
    task.outputs = "最终产出文本"
    await _synthesize_dispatch_pair(mem, scope, task, "本段我做了 A、B", "整段：A→B→验证，已就绪", "success", provider_ctx)
    turns = await mem.recall_recent(scope, [MemoryEventType.AGENT_CONVERSATION_TURN], 50, provider_ctx)
    asst = [r for r in turns if r.role == "assistant"]
    tool = [r for r in turns if r.role == "tool"]
    assert asst and asst[0].content == "本段我做了 A、B"
    call = asst[0].metadata["tool_calls"][0]
    assert call["name"].endswith("finish_task")
    # finish_task 的 input.result = task.outputs（给 user 看的最终输出，与 task_summary 分置）
    assert call["input"]["result"] == "最终产出文本"
    assert tool and tool[0].content == "整段：A→B→验证，已就绪"  # tool 槽 = task_summary（process report）
    # 同 tool_call_id、同 timestamp（相邻）
    tcid = asst[0].metadata["tool_calls"][0]["id"]
    assert tool[0].metadata["tool_call_id"] == tcid
    assert asst[0].timestamp == tool[0].timestamp


def test_finish_tool_text_falls_back():
    from ctx_weft.core.loop.steps.finalize import _finish_tool_text
    # task_summary（process report）优先；空则退 act_recap；都空给占位（不掺 outputs——outputs 在 call 入参）
    assert _finish_tool_text("综合 process report", "recap", "success") == "综合 process report"
    assert _finish_tool_text("", "recap", "success") == "recap"
    assert _finish_tool_text("", "", "success") == "(本段无更多总结)"
    assert _finish_tool_text("", "", "fail") == "(无最终产出)"
```

- [ ] **Step 2: Run to verify fail**

Run: `uv run pytest tests/unit/test_finalize.py -k "two_segments or finish_tool_text" -v`
Expected: FAIL（`_synthesize_dispatch_pair` 旧签名取 `mem_content`；无 `_finish_tool_text`）

- [ ] **Step 3: Implement**

`finalize.py` 顶部常量区后加 helper：
```python
def _finish_tool_text(task_summary: str, act_recap: str, outcome: str) -> str:
    """finish 对 tool 槽内容 = task_summary（process report）。R2 兜底：空则退 act_recap，
    再空给占位。**不掺 outputs**——最终输出在 finish_task 的 result 入参，tool 槽不重复它。
    绝不返回空串（避免空 tool 回合 / 400）。"""
    for cand in (task_summary, act_recap):
        if cand and cand.strip():
            return cand
    return "(无最终产出)" if outcome == "fail" else "(本段无更多总结)"
```

`finalize.py` `_synthesize_dispatch_pair`（替换 line 179-244 整体）：
```python
async def _synthesize_dispatch_pair(memory, scope, task, act_recap: str, task_summary: str,
                                    outcome: str, provider_ctx) -> None:
    """close 合成 agent 层 finish 对（spec 2026-06-30 两段化）：
    assistant{content=act_recap + finish_task 调用} / tool{content=task_summary 综合总结}。
    own-root：占位先写，bg close observe 产新两段后经 _replace_finish_report 替换（A1）。"""
    from ctx_weft.core.loop.steps.background_observe import (
        pop_close_report, register_close_synth, _replace_finish_report,
    )
    base = now_utc()
    tool_call_id = generate_id("tcall")
    outputs_text = _output_text(task.outputs)   # 进 finish_task 的 input.result（给 user 看）
    report_prefix = "[outcome=fail] " if outcome == "fail" else ""
    summary_text = _finish_tool_text(task_summary, act_recap, outcome)   # tool 槽 = process report

    await memory.ingest(
        MemoryEvent(
            type=MemoryEventType.AGENT_CONVERSATION_TURN, scope=scope,
            content=act_recap, timestamp=base, role="assistant",
            metadata={"origin_task_id": task.id, "parent_task_id": task.parent_task_id,
                      "tool_calls": [{"id": tool_call_id,
                                      "name": qualify("control:finish_task"),
                                      "input": {"result": outputs_text}}]},
        ),
        provider_ctx,
    )
    await memory.ingest(
        MemoryEvent(
            type=MemoryEventType.AGENT_CONVERSATION_TURN, scope=scope,
            content=f"{report_prefix}{summary_text}", timestamp=base, role="tool",
            metadata={"origin_task_id": task.id, "parent_task_id": task.parent_task_id,
                      "tool_call_id": tool_call_id},
        ),
        provider_ctx,
    )

    bg = pop_close_report(task.id)
    if bg is not None:
        bg_recap, bg_summary = bg
        await _replace_finish_report(memory, provider_ctx, scope, task.id, tool_call_id,
                                     bg_recap, bg_summary, outcome)
    else:
        register_close_synth(task.id, tool_call_id, scope, outcome)
```

`finalize.py` `finalize_task_memory`（line 70-82）签名加两 kwarg 并透传：
```python
async def finalize_task_memory(memory, state, task, mem_content: str, outcome: str, ctx,
                               *, act_recap: str, task_summary: str) -> list:
    descendants = _descendant_task_ids(task.id, ctx.task_manager)
    short = await _is_short_leaf(
        memory, state.scope, task, state.agent.loop_config, ctx, bool(descendants),
    )
    return await _close_one(
        memory, state, task, mem_content, outcome, ctx,
        short=short, act_recap=act_recap, task_summary=task_summary,
    )
```

`finalize.py` `_close_one`（line 99）签名加两 kwarg：
```python
async def _close_one(memory, state, task, mem_content: str, outcome: str, ctx,
                     *, short: bool, act_recap: str, task_summary: str) -> list:
```
并把其内两处 `_synthesize_dispatch_pair(memory, <scope>, task, mem_content, outcome, ctx.provider_ctx)`（same_agent 的 line 160-161、own-root 的 line 165）改为 `_synthesize_dispatch_pair(memory, <scope>, task, act_recap, task_summary, outcome, ctx.provider_ctx)`。
（same_agent 整支在 Task 4 重写；此处先把 own-root 那处 line 164-170 改成新签名即可，same_agent 那处保持能编译——Task 4 会替换。）

`finalize.py` `FinalizeStep.execute`（line 267-270）：
```python
        if terminal and mem_content:
            events.extend(await finalize_task_memory(
                ctx.memory, state, task, mem_content, outcome, ctx,
                act_recap=summary, task_summary=(verdict.task_summary if verdict else ""),
            ))
```
（`summary` 已是 `verdict.act_recap`，Task 1 改过。）

`background_observe.py`：
- line 29：`_close_report: dict[str, str] = {}` → `_close_report: dict[str, tuple[str, str]] = {}  # task_id → (act_recap, task_summary)`
- `pop_close_report`（38-40）返回类型注释改 `tuple[str, str] | None`（实现 `_close_report.pop(task_id, None)` 不变）。
- 顶部 import 加：`from ctx_weft.protocols.capability import qualify`、`from ctx_weft.core.utils import generate_id`（若未导入）。
- `_replace_finish_report`（替换 line 53-114 整体）：
```python
async def _replace_finish_report(memory, provider_ctx, scope, task_id: str,
                                 tool_call_id: str, act_recap: str, task_summary: str,
                                 outcome: str) -> None:
    """supersede finish 对的 assistant + tool 两条占位，按新 act_recap / task_summary 重写。
    按 (tool_call_id + origin_task_id) 定位，不再靠 'Process Report:' 文本（spec 2026-06-30 §2.4）。"""
    from ctx_weft.core.utils import now_utc  # noqa: F401  (timestamp 复用旧记录)
    from ctx_weft.protocols import MemoryEvent, MemoryEventType
    from ctx_weft.protocols.capability import qualify

    turns = await memory.recall_recent(scope, [MemoryEventType.AGENT_CONVERSATION_TURN], 500, provider_ctx)
    asst = [r for r in turns
            if r.role == "assistant" and r.metadata.get("origin_task_id") == task_id
            and any(tc.get("id") == tool_call_id for tc in (r.metadata.get("tool_calls") or []))]
    tool = [r for r in turns
            if r.role == "tool" and r.metadata.get("origin_task_id") == task_id
            and r.metadata.get("tool_call_id") == tool_call_id]
    if not asst and not tool:
        logger.warning("A1 _replace_finish_report: no finish 对 for task=%s tcid=%s; skip (best-effort)",
                       task_id, tool_call_id)
        return

    anchor = (asst or tool)[0]
    ts = anchor.timestamp
    parent_task_id = anchor.metadata.get("parent_task_id")
    tool_calls = (asst[0].metadata.get("tool_calls") if asst
                  else [{"id": tool_call_id, "name": qualify("control:finish_task"), "input": {}}])

    await memory.supersede([r.id for r in (*asst, *tool)], provider_ctx)

    report_prefix = "[outcome=fail] " if outcome == "fail" else ""
    summary_text = task_summary if (task_summary and task_summary.strip()) else act_recap
    await memory.ingest(MemoryEvent(
        type=MemoryEventType.AGENT_CONVERSATION_TURN, scope=scope,
        content=act_recap, timestamp=ts, role="assistant",
        metadata={"origin_task_id": task_id, "parent_task_id": parent_task_id, "tool_calls": tool_calls},
    ), provider_ctx)
    await memory.ingest(MemoryEvent(
        type=MemoryEventType.AGENT_CONVERSATION_TURN, scope=scope,
        content=f"{report_prefix}{summary_text}", timestamp=ts, role="tool",
        metadata={"origin_task_id": task_id, "parent_task_id": parent_task_id, "tool_call_id": tool_call_id},
    ), provider_ctx)
```
- `_run_background_observe`（line 158-180）：
```python
            result, _ = await run_observe_react(
                state, ctx,
                system=prompt.system, messages=list(prompt.messages), tools=prompt.tools,
                request_id_prefix=f"bgobs_{state.task.id}",
                max_rounds=agent.loop_config.max_turns_per_observe,
                terminal_tool_name=BACKGROUND_PROCESS_REPORT_NAME,
                event_types=BACKGROUND_OBSERVE_REACT_EVENTS,
            )
            act_recap = (result.content if result else None) or "[Context compacted]"
            task_summary = (result.metadata or {}).get("task_summary", "") if result else ""
            if boundary in _CLOSE_BOUNDARIES:
                synth = pop_close_synth(state.task.id)
                if synth is not None:
                    tool_call_id, scope, outcome = synth
                    await _replace_finish_report(
                        ctx.memory, ctx.provider_ctx, scope, state.task.id,
                        tool_call_id, act_recap, task_summary, outcome,
                    )
                else:
                    _close_report[state.task.id] = (act_recap, task_summary)
            else:
                await ctx.memory.apply_compact(
                    scope=state.scope, summary=act_recap, keep_last=0,
                    ctx=ctx.provider_ctx, layer=MemoryLayer.TASK,
                    protect_types=(MemoryEventType.USER_PROMPT,),
                )
```

`control_capability.py` `collect_process_report`（替换 line 455-468）：
```python
@control_tool(purposes=["background_observe"])
def collect_process_report(
    act_recap: Annotated[
        str,
        "Honest recap of what the LAST act phase actually did: what was changed/produced, which tools "
        "were called and whether any failed. First-person, faithful to the transcript, this segment only.",
    ],
    task_summary: Annotated[
        str,
        "For a close (finish/normal) segment: a CONCISE process report of the WHOLE task — important steps "
        "and lessons, incorporating any sub-task results. High-signal, not verbose. NOT the final output "
        "(that is the actor's finish_task result). Leave empty for non-close segments.",
    ] = "",
    *,
    ctx: ControlContext = None,
) -> ControlResult:
    """Summarize the current segment. Zero state write: never touches task.status / process_report / etc.
    Returns act_recap as content + task_summary in metadata for the close-out finish 对."""
    return ControlResult(content=act_recap, metadata={"task_summary": task_summary})
```

- [ ] **Step 4: Run to verify pass + A1/background green**

Run: `uv run pytest tests/unit/test_finalize.py tests/unit/test_close_process_report_a1.py tests/unit/test_background_observe.py -v`
Expected: PASS。`test_close_process_report_a1.py` 里既有断言「tool content 含 Process Report」的需改成断言 `tool.content == task_summary`、并新增断言 assistant content == act_recap、两条都被替换。

- [ ] **Step 5: Commit**

```bash
git add src/ctx_weft/core/loop/steps/finalize.py src/ctx_weft/core/loop/steps/background_observe.py src/ctx_weft/core/orchestrator/control_capability.py tests/unit/test_finalize.py tests/unit/test_close_process_report_a1.py tests/unit/test_background_observe.py
git commit -m "feat(finalize): finish 对两段化(act_recap/task_summary) + A1 替换两条 + collect_process_report 两字段"
```

---

### Task 4: 同 agent 派发对保留 + back-date 静态结果（不再 supersede）

**Files:**
- Modify: `src/ctx_weft/core/loop/steps/finalize.py`（`_DISPATCH_ACK` 常量 + `_close_one` same_agent 分支）
- Test: `tests/unit/test_subtask_nesting.py`、`tests/unit/test_capsule_interleaved.py`

**Interfaces:**
- Consumes: `_synthesize_dispatch_pair(memory, scope, task, act_recap, task_summary, outcome, provider_ctx)`（Task 3）。
- Produces: 常量 `_DISPATCH_ACK = "任务派发成功，以下是执行记录："`。

- [ ] **Step 1: Write the failing test**

`tests/unit/test_subtask_nesting.py` 追加（参照该文件既有的 same_agent 子任务 close 构造）：

```python
async def test_same_agent_keeps_delegate_and_writes_backdated_ack(mem, ctx, parent_scope, child_task, delegate_ts):
    # 前置：parent_scope 已有 gateway 写的 delegate assistant 回合（tool_calls[id=child.origin_tool_call_id], timestamp=delegate_ts）
    from ctx_weft.core.loop.steps.finalize import _close_one, _DISPATCH_ACK
    from ctx_weft.protocols import MemoryEventType
    await _close_one(mem, state, child_task, "out\n\nProcess Report: r", "success", ctx,
                     short=True, act_recap="本段做了 X", task_summary="整段总结")
    turns = await mem.recall_recent(parent_scope, [MemoryEventType.AGENT_CONVERSATION_TURN], 100, ctx.provider_ctx)
    # delegate 回合仍在（未被 supersede）
    delegate = [r for r in turns if r.role == "assistant"
                and any(tc.get("id") == child_task.origin_tool_call_id for tc in (r.metadata.get("tool_calls") or []))]
    assert delegate, "delegate 回合不应被 supersede"
    # 配对静态 result：content=_DISPATCH_ACK、tool_call_id 配对、timestamp == delegate 时刻
    ack = [r for r in turns if r.role == "tool" and r.metadata.get("tool_call_id") == child_task.origin_tool_call_id]
    assert ack and ack[0].content == _DISPATCH_ACK
    assert ack[0].timestamp == delegate[0].timestamp
    assert _DISPATCH_ACK not in {r.content for r in turns if r.metadata.get("tool_call_id") != child_task.origin_tool_call_id}
```

- [ ] **Step 2: Run to verify fail**

Run: `uv run pytest tests/unit/test_subtask_nesting.py -k backdated_ack -v`
Expected: FAIL（当前 same_agent 仍 supersede delegate、不写 ack；无 `_DISPATCH_ACK`）

- [ ] **Step 3: Implement**

`finalize.py` 常量区（`_FINAL_RAW_TYPES` 之后）加：
```python
# 同 agent 派发：派发对 tool 结果的静态文案（不含任何子任务结果，永不回填，spec 2026-06-30 §2.5）。
_DISPATCH_ACK = "任务派发成功，以下是执行记录："
```

`finalize.py` `_close_one` 的 `elif same_agent:` 分支（line 145-161）整体替换为：
```python
        elif same_agent:
            # 同 agent（spec 2026-06-30 §2.5）：保留 delegate 回合（不再 supersede），改写一条配对的
            # 静态 tool result，timestamp back-date 到 delegate 回合时刻 → 与 delegate 严格相邻、排在
            # 子 body 之前。子任务真实产出由内联胶囊 body + 嵌套 finish 对承载。
            dispatches = await memory.recall_recent(
                parent_scope, [MemoryEventType.AGENT_CONVERSATION_TURN], 2000, ctx.provider_ctx)
            delegate_turn = next(
                (r for r in dispatches
                 if r.role == "assistant"
                 and any(tc.get("id") == task.origin_tool_call_id
                         for tc in (r.metadata.get("tool_calls") or []))),
                None,
            )
            delegate_ts = delegate_turn.timestamp if delegate_turn else now_utc()
            await memory.ingest(
                MemoryEvent(
                    type=MemoryEventType.AGENT_CONVERSATION_TURN, scope=parent_scope,
                    content=_DISPATCH_ACK, timestamp=delegate_ts, role="tool",
                    metadata={"origin_task_id": task.parent_task_id,
                              "tool_call_id": task.origin_tool_call_id},
                ),
                ctx.provider_ctx,
            )
            # 嵌套合成子自己的 finish 对（写进共享 agent scope，@close 时刻）
            await _synthesize_dispatch_pair(
                memory, parent_scope, task, act_recap, task_summary, outcome, ctx.provider_ctx)
```

- [ ] **Step 4: Run to verify pass + 相邻性回归**

Run: `uv run pytest tests/unit/test_subtask_nesting.py tests/unit/test_capsule_interleaved.py tests/unit/test_capsule_golden.py -v`
Expected: PASS。`test_capsule_golden.py` / `test_subtask_nesting.py` 中断言「same_agent delegate 被 supersede / 无 dispatch result」的旧用例须反转为「delegate 保留 + 配对静态 ack、时间戳锚 delegate」。

- [ ] **Step 5: Commit**

```bash
git add src/ctx_weft/core/loop/steps/finalize.py tests/unit/test_subtask_nesting.py tests/unit/test_capsule_interleaved.py tests/unit/test_capsule_golden.py
git commit -m "fix(finalize): 同 agent 派发对保留 delegate + back-date 静态 result(根治派发结果劈开 400)"
```

---

### Task 5: 汇报给 parent 用 `outputs` + `task_summary`（`mem_content` 的 report 部分改 `task_summary`）

父看不到子胶囊，cross_agent dispatch result（及 blackboard）是父唯一可见的子结果，须把**两部分拼在一起** = `outputs` + `task_summary`（task_summary 承载 process report 作用）。cross_agent 分支**写法不变**（仍写 `mem_content`），只把 `mem_content` 的 report 部分从 `act_recap` 改为 `task_summary`。

**Files:**
- Modify: `src/ctx_weft/core/loop/steps/finalize.py`（`FinalizeStep.execute` 构建 `mem_content`）
- Test: `tests/unit/test_subtask_nesting.py`、`tests/unit/test_finalize.py`

**Interfaces:**
- Consumes: `Verdict.act_recap/task_summary`、`_build_memory_content`（既有）。

- [ ] **Step 1: Write the failing test**

`tests/unit/test_subtask_nesting.py` 追加：

```python
async def test_cross_agent_result_carries_outputs_and_task_summary(mem, ctx, parent_scope, cross_child_task, state):
    from ctx_weft.core.loop.steps.finalize import FinalizeStep
    from ctx_weft.protocols import MemoryEventType
    cross_child_task.outputs = "最终产出给 user"
    # verdict：act_recap=本段、task_summary=综合 process report
    state.verdict = _make_verdict(outcome="success", act_recap="本段", task_summary="综合 process report")
    state.task = cross_child_task
    await FinalizeStep().execute(state, ctx)
    turns = await mem.recall_recent(parent_scope, [MemoryEventType.AGENT_CONVERSATION_TURN], 100, ctx.provider_ctx)
    result = [r for r in turns if r.role == "tool"
              and r.metadata.get("tool_call_id") == cross_child_task.origin_tool_call_id]
    assert result
    body = result[0].content
    assert "最终产出给 user" in body          # 最终输出
    assert "综合 process report" in body       # task_summary 承载 process report
    assert "本段" not in body                   # 不掺 act_recap
```
（`_make_verdict` / `state` / `cross_child_task` 取该文件既有 helper；无则参照既有 cross_agent close 测试构造。）

- [ ] **Step 2: Run to verify fail**

Run: `uv run pytest tests/unit/test_subtask_nesting.py -k carries_outputs_and_task_summary -v`
Expected: FAIL（当前 `mem_content` 的 report 部分是 `act_recap`，不含 task_summary）

- [ ] **Step 3: Implement**

`finalize.py` `FinalizeStep.execute`（Task 3 改过的 `mem_content` 那行，约 line 264）改为：
```python
        summary = verdict.act_recap if verdict else ""            # → task.process_report（retry Current Progress）
        task_summary = verdict.task_summary if verdict else ""    # → 汇报给 parent 的 process report
        ...
        # 汇报给 parent（blackboard + cross_agent bubble）= 最终输出 + task_summary（process report 作用）；
        # task_summary 空时回退 act_recap。
        mem_content = _build_memory_content(task.outputs, task_summary or summary)
```
cross_agent 分支与 `BLACKBOARD_PUBLISH` 代码**不动**——它们已用 `mem_content`，现自动携 outputs + task_summary。

- [ ] **Step 4: Run to verify pass**

Run: `uv run pytest tests/unit/test_subtask_nesting.py tests/unit/test_open_closed_recall.py tests/unit/test_dispatch_fold_golden.py tests/unit/test_finalize.py -v`
Expected: PASS。既有断言「cross result / blackboard 含旧 process_report 文本」改为含 task_summary。

- [ ] **Step 5: Commit**

```bash
git add src/ctx_weft/core/loop/steps/finalize.py tests/unit/test_subtask_nesting.py tests/unit/test_open_closed_recall.py tests/unit/test_dispatch_fold_golden.py tests/unit/test_finalize.py
git commit -m "feat(finalize): 汇报给 parent 用 outputs+task_summary(mem_content report 部分改 task_summary)"
```

---

### Task 6: observe / background_observe cue 要两字段（含综合子任务）

**Files:**
- Modify: `src/ctx_weft/core/assembler/composer.py`（`_OBSERVE_JUDGMENT_CUE`、`_background_observe_cue`）
- Test: `tests/unit/test_background_observe_prompt.py`

**Interfaces:**
- Consumes: 工具名 `report_task_outcome` / `collect_process_report` 的新字段语义（Task 2/3）。

- [ ] **Step 1: Write the failing test**

`tests/unit/test_background_observe_prompt.py` 追加（参照该文件既有断言 cue 文案的方式）：

```python
def test_observe_cue_mentions_both_fields():
    from ctx_weft.core.assembler.composer import _OBSERVE_JUDGMENT_CUE, _background_observe_cue
    assert "act_recap" in _OBSERVE_JUDGMENT_CUE
    assert "task_summary" in _OBSERVE_JUDGMENT_CUE
    close_cue = _background_observe_cue("finish")
    assert "act_recap" in close_cue and "task_summary" in close_cue
    # 综合子任务结果的引导
    assert "sub-task" in close_cue.lower() or "子任务" in close_cue
```

- [ ] **Step 2: Run to verify fail**

Run: `uv run pytest tests/unit/test_background_observe_prompt.py -k both_fields -v`
Expected: FAIL（现 cue 只提 `task_process_report`）

- [ ] **Step 3: Implement**

`composer.py` `_OBSERVE_JUDGMENT_CUE`（line 71-77）替换为：
```python
_OBSERVE_JUDGMENT_CUE = (
    "Now act as the observer for the current task. Based on the execution above, judge the "
    f"task's completion status and call `{REPORT_TASK_OUTCOME_NAME}` exactly once with: a `task_status` "
    "of `success` / `retry` / `fail`; an `act_recap` honestly recapping what the last act phase did; "
    "and — when status is success/fail — a concise `task_summary`: the important steps and lessons of the "
    "whole task (a process report, not verbose, and NOT the final output), incorporating the results of any "
    "sub-tasks you dispatched. Optionally review your own sub-tasks via `task_reviews`. Call no other tools."
)
```

`composer.py` `_background_observe_cue`（line 105-111）替换为：
```python
def _background_observe_cue(boundary: str) -> str:
    desc = _BACKGROUND_BOUNDARY_DESC.get(boundary, _BACKGROUND_BOUNDARY_DESC["normal"])
    is_close = boundary in _CLOSE_BOUNDARIES
    summary_ask = (
        " 并给出 `task_summary`：整个 task 执行历程的简洁 process report（点出重要步骤与经验，不琐碎；"
        "不是最终输出），须综合已完成子任务（sub-task）的结果。"
        if is_close else ""
    )
    return (
        f"当前 task 的状态：{desc}。请基于以上执行过程，调用 `collect_process_report` 一次："
        "给出 `act_recap`（诚实复述上一段 act 做了什么）" + summary_ask +
        " 只需总结，无需判断 success/retry/fail，不要调用其他工具。"
    )
```

- [ ] **Step 4: Run to verify pass**

Run: `uv run pytest tests/unit/test_background_observe_prompt.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/ctx_weft/core/assembler/composer.py tests/unit/test_background_observe_prompt.py
git commit -m "feat(composer): observe/background cue 要 act_recap+task_summary(含综合子任务结果)"
```

---

### Task 7: 相关 prompt —— observer persona（ROLE.md）+ actor（SOUL.md）+ tracking 汇报

工具字段重设计后，告诉模型「往哪个槽写什么」的 persona prompt 必须同步改。两份 git-tracked 副本：dev `resources/`、打包 `packaging/default_data/`。ROLE.md 两份目前仅差一行（dev 多一句子任务综合），本任务统一重写为相同新内容；SOUL.md 两份 frontmatter 故意不同，只做**定向**一行编辑。

**Files:**
- Modify: `resources/agents/default/ROLE.md`、`packaging/default_data/agents/default/ROLE.md`（observer：两段 act_recap/task_summary，整文件改为相同新内容）
- Modify: `resources/agents/default/SOUL.md`、`packaging/default_data/agents/default/SOUL.md`（actor：finish_task result 澄清，定向一行）
- Modify: `src/ctx_weft/core/runtime.py`（tracking 汇报用 `task_summary`）
- Modify: `src/ctx_weft/core/orchestrator/control_capability.py:421`（success 护栏 hint 文案 process report→recap）
- Test: `tests/unit/test_runtime_tracking.py`（若无则就近既有 runtime/tracking 测试文件）；persona 文件加 grep 守护测试

**Interfaces:**
- Consumes: `Task.task_summary`（Task 1）、`report_task_outcome` 字段（Task 2）。

- [ ] **Step 1: Write the failing test**

`tests/unit/test_observe_outcomes.py` 追加 persona 守护 + tracking 测试（tracking 测试参照 runtime 既有 fixture，无则放 `test_finalize.py` 旁的 runtime 测试）：

```python
def test_default_role_prompt_uses_two_fields():
    import pathlib
    root = pathlib.Path(__file__).resolve().parents[3]  # 仓库根
    for rel in ["resources/agents/default/ROLE.md",
                "packaging/default_data/agents/default/ROLE.md"]:
        text = (root / rel).read_text(encoding="utf-8")
        assert "act_recap" in text and "task_summary" in text, rel
        assert "task_process_report" not in text, rel  # 旧字段名已清

async def test_tracking_report_uses_task_summary(mem, runtime_ctx, tracked_task, dependent_scope):
    # tracked_task.outputs="最终输出", tracked_task.task_summary="综合 report", process_report="本段 recap"
    await _fetch_tracking_results(...)  # 触发 runtime 的 tracking 注入（取该模块既有入口名）
    turns = await mem.recall_recent(dependent_scope, [MemoryEventType.OBSERVER_SUMMARY], 10, runtime_ctx.provider_ctx)
    body = turns[-1].content
    assert "综合 report" in body and "本段 recap" not in body
```

- [ ] **Step 2: Run to verify fail**

Run: `cd ctx-weft && uv run pytest tests/unit/test_observe_outcomes.py -k "role_prompt or tracking_report" -v`
Expected: FAIL（ROLE.md 仍含 `task_process_report`、无 act_recap/task_summary；tracking 注入用 process_report）

- [ ] **Step 3: Implement — 重写 ROLE.md（两份相同内容）**

把 `resources/agents/default/ROLE.md` 与 `packaging/default_data/agents/default/ROLE.md` **都**改为以下完整内容：

````markdown
---
tools:
  required:
    - report_task_outcome
    - replan
  forbidden: []
---

你是执行循环中的观察者，在 actor 完成一段执行后接管。职责是客观记录"发生了什么"，而不是重新执行或替 actor 做决策。末尾的提示会告诉你本次具体要做哪件事，两种情况：

- **仅生成执行总结**：基于上面的执行过程，调用 `collect_process_report`，给出 `act_recap`（诚实复述这一段 act 做了什么）；若是收尾段（任务已完成），再给出 `task_summary`（整段简洁 process report）。无需判断成败、不调用其他工具。
- **裁决任务结果**：在写执行复述之外，还要判断任务状态并调用 `control__report_task_outcome`（或在计划根本有误时调用 `control__replan`）。

**调用一次工具后循环即停止，不得再调用任何工具；每个工具只调用一次。不要过度思考，结论清楚就尽快调用。**

---

## act_recap：本段 act 执行复述（始终要写）

**先确定复述范围——「本段 act」**：从对话里**最后一个 `## Progress So Far`**（那是上一次观察留下的进度复述）之后、actor 新发生的执行算起，到当前为止。若对话里没有 `## Progress So Far`（本任务首次观察），则从 `## Current Task` / 用户最新消息之后算起。该起点之前的历史已被先前的观察覆盖过，**不要回头重述**。

在这个范围内，第一人称、基于证据、不臆测、不泛泛而谈：

- actor 做了什么、调用了哪些工具、各自得到什么结果（哪个工具失败、报了什么错也写上）；
- 产出或修改了什么（文件名、数据、关键结论等）；
- 若 actor 通过 `control__finish_task` 收尾，写明它收尾了；本轮没有最终输出就直说。

如实陈述，区分"实际完成"与"仅尝试"：这个范围里实际发生的工具调用、结果与产出都要交代清楚，不要为求简短而略去关键步骤；但只写与执行相关的事实，不堆砌无关篇幅。

---

## task_summary：整段简洁 process report（仅收尾段 / 裁决为 success|fail 时写）

**整个 task 执行历程的简洁总结**——点出其中的重要步骤与经验/教训，高信噪、不琐碎：

- 跨轮回顾这个 task 一路是怎么走完的、关键决策与产出、踩过的坑；
- 若本任务通过 `control__delegate_task` / `control__delegate_plan` 委派过子任务、且已拿到回填结果，**综合这些已完成子任务的关键结果**（成功产出、失败原因、关键数据）——子任务产出稍后会随对话压缩隐去，这里是它们最终留存的黑盒摘要，务必收进来，不要只写"已委派/已完成"。

**注意：`task_summary` 不是最终输出。** 给 user 看的最终成品是 actor 在 `control__finish_task` 的 `result` 里提交的；`task_summary` 只承载"执行历程与经验"的 process report 作用，不要把最终产物原样抄进来。

---

## 裁决（仅当末尾提示要求判断任务结果时）

### 路径 A：结论明确 → `control__report_task_outcome`

- `task_status`（三选一）：
  - `success`：任务目标已达成。
  - `retry`：本轮未达成、但值得再试一轮，用 `next_step_hint` 给出下一步提示。
  - `fail`：任务无法完成、且不应再重试，在 `task_failure_reason` 说明原因。
  - 注：若 actor 需要用户介入，那是 actor 在执行阶段用 `control__ask_user` 处理的，不归你裁决；你只需据现状判 `success` / `retry` / `fail`。
- `act_recap`：按上面"本段 act 执行复述"填写（始终）。
- `task_summary`：仅当 `task_status` 为 `success` / `fail` 时填，按上面"整段简洁 process report"填写；`retry` 留空。
- `task_failure_reason`：仅当 `task_status` 为 `fail` 时必填，说明失败发生在哪个环节、什么错误或与要求不符之处、根本原因。非 fail 留空。
- `next_step_hint`（可选）：若有明显风险、阻塞点或下一轮需特别注意的事项在此说明；无则留空。
- `task_reviews`：若用户消息中包含"Session task list"，在同一次调用中复核其中 FINISHED / PENDING 任务；否则留空列表。对每条填 `task_title`（须与原始标题完全一致）、`review_status`、`reasoning`（必填，一句话即可）：
  - `confirmed`：FINISHED 任务已达成，无需操作。
  - `reopen`：FINISHED 任务实际未达成，系统将重新入队执行。
  - `skip`：PENDING 任务已被间接满足，系统将直接标记完成。

  注意：当前正在裁决的任务不填入（已由 `task_status` 处理）；没把握的任务不填，避免误判；由当前任务通过 `control__delegate_task` 委派产生的 PENDING 子任务**不要** `skip`。

### 路径 B：计划根本有误 → `control__replan`

当执行结果揭示当前计划从根本上有误、继续按原计划推进已无意义时使用。所有待执行任务将被取消，系统将创建新的规划任务重新开始。

- `reason`：为何需要重新规划。
- `tasks`：替代原计划的新任务列表（有序）。
````

- [ ] **Step 4: Implement — SOUL.md 定向一行（两份）**

在 `resources/agents/default/SOUL.md` 与 `packaging/default_data/agents/default/SOUL.md` 中，把这一行：
```
- 完成当前任务时，调用 `control__finish_task(result=...)` 提交最终结果以结束任务；`result` 必须描述实际完成或产出的内容。
```
改为：
```
- 完成当前任务时，调用 `control__finish_task(result=...)` 提交最终结果以结束任务；`result` 必须描述实际完成或产出的内容——这是给用户看的最终成品，执行过程/历程总结由系统的观察者另行记录，你无需把过程塞进 `result`。
```

- [ ] **Step 5: Implement — runtime tracking + success hint**

`src/ctx_weft/core/runtime.py:158`：
```python
        report = tracked.task_summary or tracked.process_report or ""
```

`src/ctx_weft/core/orchestrator/control_capability.py:421`，把 `_hint` 里的 `"Review the process report above "` 改为 `"Review the act_recap above "`（字段已改名）。

- [ ] **Step 6: Run to verify pass**

Run: `cd ctx-weft && uv run pytest tests/unit/test_observe_outcomes.py -k "role_prompt or tracking_report" -v`
Expected: PASS

- [ ] **Step 7: Commit**

```bash
git add resources/agents/default/ROLE.md packaging/default_data/agents/default/ROLE.md resources/agents/default/SOUL.md packaging/default_data/agents/default/SOUL.md src/ctx_weft/core/runtime.py src/ctx_weft/core/orchestrator/control_capability.py tests/unit/test_observe_outcomes.py
git commit -m "feat(prompt): observer ROLE 两段(act_recap/task_summary) + SOUL finish_task 澄清 + tracking 汇报用 task_summary"
```

---

### Task 8: 全套回归 + 端到端相邻性

**Files:**
- Test only（修复任何因形态变更而过期的 golden / 断言）

- [ ] **Step 1: 跑全套 unit**

Run: `uv run pytest tests/unit -q --with psutil --with pyyaml`
Expected: 全绿。重点过期点：`test_capsule_golden.py`、`test_capsule_render.py`、`test_capsule_interleaved.py`、`test_subtask_nesting.py`、`test_close_task.py`、`test_open_closed_recall.py`、`test_dispatch_fold_golden.py`、`test_close_process_report_a1.py`、`test_observe_react_helper.py`——逐一按新形态（finish 对两段、same_agent 保留 delegate+ack、cross result=task_summary）更新断言。

- [ ] **Step 2: 端到端相邻性守护测试**

在 `tests/unit/test_capsule_interleaved.py` 加一条端到端：构造「parent delegate 同 agent 子任务 → 子任务跑 body（时间戳介于 delegate 与 close 之间）→ close」，经 `AgentRecallSource` + composer 归并出 messages，断言：
```python
def _assert_tool_follows_call(messages):
    for i, m in enumerate(messages):
        if m.role == "tool":
            prev = messages[i-1]
            assert prev.role == "assistant" and any(
                tc["id"] == m.tool_call_id for tc in (prev.tool_calls or [])
            ), f"tool@{i} 未紧跟其 assistant tool_call"
```
断言归并后 `delegate → _DISPATCH_ACK` 严格相邻、子 body 排在 ack 之后、finish 对相邻；`_assert_tool_follows_call(messages)` 通过（无 400 隐患）。

- [ ] **Step 3: Commit**

```bash
git add tests/
git commit -m "test: finish 对两段 + 同 agent 派发对保留 全套回归 + 端到端相邻性守护"
```

---

## Self-Review

**Spec coverage：**
- §2.1 工具字段（act_recap/task_summary、折 process_report）→ Task 2（report_task_outcome）+ Task 3（collect_process_report）。✓
- §2.2 Verdict + cue → Task 1（Verdict）+ Task 6（cue）。✓
- §2.3 finish 对两段 synthesize → Task 3。✓
- §2.4 _replace_finish_report 换两条 + A1 二元组 → Task 3。✓
- §2.5 同 agent 保留+back-date 静态 result → Task 4。✓
- §2.6 跨 agent result→task_summary → Task 5。✓
- R2 task_summary 兜底 → Task 3 `_finish_tool_text` + Task 5 cross。✓
- R3 delegate 回合缺失回退 now_utc → Task 4（`delegate_ts = ... if delegate_turn else now_utc()`）。✓
- §2.7 相关 prompt（ROLE.md 两段 / SOUL.md 澄清 / runtime tracking 用 task_summary） → Task 7。✓
- 验收「端到端无 400」→ Task 8 相邻性守护。✓

**Placeholder scan：** 无 TBD；每步含真实代码/命令/期望。测试 fixture 处注明「参照文件内既有构造」——因各 test 文件 fixture 命名不一，实现时取该文件既有 helper（非占位，是适配既有测试基建的指示）。

**Type consistency：** `_synthesize_dispatch_pair(... act_recap, task_summary, outcome, provider_ctx)`、`_finish_tool_text(task_summary, act_recap, outcome)`（3 参，不含 outputs——outputs 在 finish_task call 入参）、`pop_close_report -> tuple[str,str]|None`、`_replace_finish_report(... act_recap, task_summary, outcome)`、`_close_one(..., act_recap, task_summary)` 在 Task 3/4/5 间签名一致。`Verdict.act_recap/task_summary`、`Task.task_summary` 全程一致。mem_content 的 report 部分（Task 5）= `task_summary or act_recap`。✓
