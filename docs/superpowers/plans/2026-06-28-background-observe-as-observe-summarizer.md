# background observe 升级为 observe 风格段总结器 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让 background observe 走 observe persona（ROLE.md）+ 精简零状态工具产 process_report；按段边界分流（打断写 TASK_COMPACT_SUMMARY / close 进 finish 对 Process Report 不写 memory）；close 路径乐观快速返回 + 异步替换；无 ROLE 模板回退 default observe facet。

**Architecture:** 新增零状态写工具 `collect_process_report` + 新 purpose `background_observe`（复用 ROLE.md facet + 专属 cue）。background observe 跑 observe 风格 ReAct（复用 observe 抽出的公共骨架）取 process_report，按 `boundary` 分流写 memory 段摘要或落结果槽。finalize close 机会性用结果槽产出、否则薄占位 + background 完成回调异步替换 finish 对 Process Report。

**Tech Stack:** Python 3.11、pytest（`uv run pytest`，pyproject 配 `pythonpath=["."]`）、asyncio fire-and-forget。

## Global Constraints

- 测试一律 `uv run pytest`；core 测试在 `ctx-weft/`（`cd ctx-weft && uv run pytest`），host 测试在仓根 `tests/`（`uv run pytest`）。`uv` 会打印一行 `VIRTUAL_ENV ... does not match` 警告，是已知无害噪音。
- **不变量 1 · 零状态写**：`collect_process_report` 绝不写 `task.status / observer_outcome / actor_done / process_report / error`；只返回内容。
- **不变量 2 · persona 共用**：observe 与 background observe 共用 ROLE.md（整体不拆）；差异仅在 cue + 绑定工具。ROLE.md / COMPACT.md / METADATA.md 文本不动。
- **不变量 3 · 边界分流**：`interrupt`/`plain_text` → 写 `TASK_COMPACT_SUMMARY`（role=assistant）；`finish`/`normal` → 进 finish 对 Process Report，**不写 memory 段摘要**。
- **不变量 5 · 机械退出不变**：`max_turns`（`_maybe_compact_task`）/ `context_limit`（prepare compact）仍走同步 COMPACT.md 压缩，**不** fire background observe。
- **不变量 6 · 非 root 不变**：子任务仍走完整 LLM observe（`report_task_outcome`）。
- 段摘要 `TASK_COMPACT_SUMMARY` 写入 role=assistant（沿用前一 spec，`apply_compact(layer=TASK)` 已存 assistant）。
- Spec：`docs/superpowers/specs/2026-06-28-background-observe-as-observe-summarizer-design.md`。

---

### Task 1: 精简零状态工具 `collect_process_report`

**Files:**
- Modify: `src/ctx_weft/core/orchestrator/control_capability.py`（在 `report_task_outcome` 定义之后，约 line 450 后追加）
- Test: `tests/unit/test_collect_process_report.py`（新建）

**Interfaces:**
- Produces: `collect_process_report(task_process_report: str, *, ctx) -> ControlResult`，`@control_tool(purposes=["background_observe"])`，名 `BACKGROUND_PROCESS_REPORT_NAME = qualify("control:collect_process_report")`。返回 `ControlResult(content=task_process_report)`，零状态写。

- [ ] **Step 1: 写失败测试**（`test_collect_process_report.py`）

```python
"""collect_process_report：零状态写——只回传内容，不碰 task 任何状态字段。"""
from __future__ import annotations

import pytest

from ctx_weft.core.orchestrator.control_capability import (
    collect_process_report, BACKGROUND_PROCESS_REPORT_NAME,
)
from ctx_weft.core.state.models import Task, NormalTaskSettings


def _task() -> Task:
    return Task(id="t1", session_id="s1", status="RUNNING", assigned_agent_id="a1",
                creator_agent_id="a1", title="T", settings=NormalTaskSettings())


class _Ctx:
    def __init__(self, task): self.task = task


def test_collect_process_report_returns_content_unchanged():
    res = collect_process_report("段①：读了 auth，改了 3 处。", ctx=_Ctx(_task()))
    assert res.content == "段①：读了 auth，改了 3 处。"


def test_collect_process_report_zero_state_write():
    t = _task()
    before = (t.status, t.observer_outcome, t.actor_done, t.process_report, t.error)
    collect_process_report("任意报告", ctx=_Ctx(t))
    after = (t.status, t.observer_outcome, t.actor_done, t.process_report, t.error)
    assert before == after, "collect_process_report 不得写 task 任何状态字段"


def test_name_qualified():
    assert BACKGROUND_PROCESS_REPORT_NAME.endswith("collect_process_report")
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd ctx-weft && uv run pytest tests/unit/test_collect_process_report.py -v`
Expected: FAIL（`ImportError: cannot import name 'collect_process_report'`）。

- [ ] **Step 3: 实现**（control_capability.py，在 `report_task_outcome` 函数体结束后追加；并在文件顶部常量区 `REPORT_TASK_OUTCOME_NAME` 旁加名常量）

文件顶部常量区（约 line 55 `REPORT_TASK_OUTCOME_NAME` 之后）加：
```python
BACKGROUND_PROCESS_REPORT_NAME = qualify(f"{PROVIDER_NAME}:collect_process_report")
```
追加工具定义（复用 `report_task_outcome` 的 `task_process_report` 描述原文）：
```python
@control_tool(purposes=["background_observe"])
def collect_process_report(
    task_process_report: Annotated[
        str,
        "A thorough execution record: describe what was accomplished, what was modified or "
        "produced, which tools were called and whether any failed, and — if incomplete — what remains and why. "
        "Written to memory and read by the next actor turn, so be specific and evidence-based.",
    ],
    *,
    ctx: ControlContext = None,
) -> ControlResult:
    """Summarize the current segment's progress as a process report. Zero state write:
    this tool never touches task.status / observer_outcome / actor_done / process_report / error."""
    return ControlResult(content=task_process_report)
```

- [ ] **Step 4: 跑测试确认通过**

Run: `cd ctx-weft && uv run pytest tests/unit/test_collect_process_report.py -v`
Expected: PASS（3 passed）。

- [ ] **Step 5: 提交**

```bash
git add src/ctx_weft/core/orchestrator/control_capability.py tests/unit/test_collect_process_report.py
git commit -m "feat(control): collect_process_report 零状态写工具（background observe 专用）"
```

---

### Task 2: facet 回退链 `background_observe → observe → act`

**Files:**
- Modify: `src/ctx_weft/core/assembler/sources/identity.py:32`
- Test: `tests/unit/test_identity_facet_fallback.py`（新建）

**Interfaces:**
- Consumes: `IdentitySource.fetch`（已存在）。
- Produces: 装配 `purpose="background_observe"` 时，facet 取 `template.identity["background_observe"]`，缺失回退 `["observe"]`，再缺回退 `["act"]`。

- [ ] **Step 1: 写失败测试**（`test_identity_facet_fallback.py`）

```python
"""IdentitySource：background_observe facet 缺失 → 回退 observe（ROLE.md）→ 再回退 act。"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from ctx_weft.core.assembler.sources.identity import IdentitySource


def _facet(text): return SimpleNamespace(text=text, style="")


def _template(identity: dict):
    return SimpleNamespace(id="tpl", version="1", identity=identity)


def _req(purpose, template):
    return SimpleNamespace(purpose=purpose, template=template, extra={})


async def _facets(req):
    return [b async for b in IdentitySource().fetch(req, SimpleNamespace())]


@pytest.mark.asyncio
async def test_background_observe_falls_back_to_observe():
    tpl = _template({"act": _facet("SOUL"), "observe": _facet("ROLE-OBSERVE")})
    blocks = await _facets(_req("background_observe", tpl))
    assert blocks[0].content == "ROLE-OBSERVE"


@pytest.mark.asyncio
async def test_background_observe_falls_back_to_act_when_no_observe():
    tpl = _template({"act": _facet("SOUL")})
    blocks = await _facets(_req("background_observe", tpl))
    assert blocks[0].content == "SOUL"


@pytest.mark.asyncio
async def test_background_observe_prefers_own_facet():
    tpl = _template({"act": _facet("SOUL"), "observe": _facet("ROLE"),
                     "background_observe": _facet("BG")})
    blocks = await _facets(_req("background_observe", tpl))
    assert blocks[0].content == "BG"
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd ctx-weft && uv run pytest tests/unit/test_identity_facet_fallback.py -v`
Expected: FAIL（`test_background_observe_falls_back_to_observe` 得到 "SOUL" 而非 "ROLE-OBSERVE"——现状只回退 act）。

- [ ] **Step 3: 实现**（identity.py:32）

把：
```python
        facet = template.identity.get(request.purpose) or template.identity.get("act")
```
改为：
```python
        # background_observe 复用 observe 的 ROLE facet；缺 observe 再回退 act。
        facet = (
            template.identity.get(request.purpose)
            or (template.identity.get("observe") if request.purpose == "background_observe" else None)
            or template.identity.get("act")
        )
```

- [ ] **Step 4: 跑测试确认通过**

Run: `cd ctx-weft && uv run pytest tests/unit/test_identity_facet_fallback.py -v`
Expected: PASS（3 passed）。

- [ ] **Step 5: 提交**

```bash
git add src/ctx_weft/core/assembler/sources/identity.py tests/unit/test_identity_facet_fallback.py
git commit -m "feat(assembler): background_observe facet 回退链 → observe → act"
```

---

### Task 3: background cue + boundary 描述 + purpose 装配分支

**Files:**
- Modify: `src/ctx_weft/core/assembler/composer.py`（常量区约 line 84 后加 cue + boundary 映射；`assemble` purpose 分支约 line 183；新增 `_build_background_observe_messages`）
- Test: `tests/unit/test_background_observe_prompt.py`（新建）

**Interfaces:**
- Consumes: `_build_facet_trailing_messages`（已存在，接 `cue` + `facet_fallback`）；`request.extra["observe_boundary"]`（boundary 字符串）。
- Produces: `assemble(ContextRequest(purpose="background_observe", extra={"observe_boundary": <b>}))` → system + trailing user message 含 ROLE facet + background cue（按 boundary 注入状态描述 + 「只给 process_report、不判裁决」）。常量 `_BACKGROUND_BOUNDARY_DESC: dict[str,str]`。

- [ ] **Step 1: 写失败测试**（`test_background_observe_prompt.py`）

```python
"""composer：purpose=background_observe 装配——ROLE facet + background cue（按 boundary）。"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from ctx_weft.core.assembler.composer import (
    DefaultComposer, _BACKGROUND_BOUNDARY_DESC,
)


def _blocks():
    # 一条 identity facet（模拟 ROLE）+ 一条 user 历史，保证 _build_actor_messages 以 user 收尾
    from ctx_weft.core.assembler.assembler import ContextBlock
    return [
        ContextBlock(id="b0", source="identity", kind="identity", target="system",
                     content="ROLE-OBSERVE-BODY", priority=0, token_estimate=1, metadata={}),
        ContextBlock(id="b1", source="task_conversation", kind="history", target="messages",
                     content="原始诉求", priority=3, token_estimate=1,
                     metadata={"role": "user", "timestamp": "2026-01-01T00:00:00+00:00"}),
    ]


def _req(boundary):
    return SimpleNamespace(purpose="background_observe", task=SimpleNamespace(
        id="t1", user_prompt_in_memory=True, title="", description="", user_prompt="x",
        outputs="", process_report="", process_report_at=None, tracking_task_ids=[],
        parent_task_id=None), session=SimpleNamespace(user_prompt="x"),
        template=None, bound_capabilities=[], actor_transcript=[],
        extra={"observe_boundary": boundary})


def test_background_cue_only_process_report_no_verdict():
    msgs = DefaultComposer()._build_background_observe_messages(_blocks(), _req("interrupt"))
    joined = "\n".join(m.content for m in msgs if isinstance(m.content, str))
    assert "collect_process_report" in joined
    assert "无需判断" in joined            # 抑制三态裁决
    assert _BACKGROUND_BOUNDARY_DESC["interrupt"] in joined


@pytest.mark.parametrize("boundary", ["interrupt", "plain_text", "finish", "normal"])
def test_background_cue_injects_each_boundary(boundary):
    msgs = DefaultComposer()._build_background_observe_messages(_blocks(), _req(boundary))
    joined = "\n".join(m.content for m in msgs if isinstance(m.content, str))
    assert _BACKGROUND_BOUNDARY_DESC[boundary] in joined
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd ctx-weft && uv run pytest tests/unit/test_background_observe_prompt.py -v`
Expected: FAIL（`ImportError: _BACKGROUND_BOUNDARY_DESC` / `_build_background_observe_messages` 不存在）。

- [ ] **Step 3: 实现**（composer.py）

常量区（在 `_RECOGNIZE_INTENT_INSTRUCTION` 之后，约 line 90）加：
```python
_BACKGROUND_BOUNDARY_DESC = {
    "interrupt": "本段被用户打断（中途打断）",
    "plain_text": "你以散文回复后让位用户、暂停等待用户输入",
    "finish": "任务已通过 finish_task 收尾",
    "normal": "任务以最终产出正常结束",
}


def _background_observe_cue(boundary: str) -> str:
    desc = _BACKGROUND_BOUNDARY_DESC.get(boundary, _BACKGROUND_BOUNDARY_DESC["normal"])
    return (
        f"当前 task 的状态：{desc}。请基于以上执行过程，总结这一段的处理进展，"
        "调用 `collect_process_report` 一次给出 `task_process_report`。"
        "只需总结进展、给出 process report，无需判断 success/retry/fail，不要调用其他工具。"
    )
```
`assemble` purpose 分支（约 line 183，`else:  # compact` 之前）加：
```python
        elif request.purpose == "background_observe":
            system = self._build_act_system(blocks, request)
            messages = self._build_background_observe_messages(blocks, request)
```
新增方法（紧邻 `_build_observer_messages` 之后）：
```python
    def _build_background_observe_messages(self, blocks, request):
        """act 风格会话 + 尾部 background-observe cue（ROLE facet + boundary 状态 + 只给 process_report）。"""
        boundary = (getattr(request, "extra", {}) or {}).get("observe_boundary", "normal")
        return self._build_facet_trailing_messages(
            blocks, request, _background_observe_cue(boundary),
            facet_fallback=_OBSERVER_ROLE_FALLBACK,
        )
```

- [ ] **Step 4: 跑测试确认通过**

Run: `cd ctx-weft && uv run pytest tests/unit/test_background_observe_prompt.py -v`
Expected: PASS（5 passed：1 + 4 参数化）。

- [ ] **Step 5: 提交**

```bash
git add src/ctx_weft/core/assembler/composer.py tests/unit/test_background_observe_prompt.py
git commit -m "feat(composer): background_observe 装配分支 + boundary-aware cue（抑制裁决）"
```

---

### Task 4: default ROLE 回退（无 ROLE 模板借 default observe facet）

**Files:**
- Modify: `src/ipmastercowork/providers/templates/resolver.py:30`
- Test: `tests/unit/test_template_default_observe_merge.py`（新建，host 仓根 tests）

**Interfaces:**
- Consumes: `merge_default_facets(template, default, purposes)`（已存在）。
- Produces: `DEFAULT_MERGE_PURPOSES` 含 `"observe"` → 无 `observe` facet 的模板从 default 模板补入。

- [ ] **Step 1: 写失败测试**（`tests/unit/test_template_default_observe_merge.py`）

```python
"""resolver：无 observe facet 的模板经 merge_default_facets 借 default 的 observe（ROLE）。"""
from __future__ import annotations

from types import SimpleNamespace

from ipmastercowork.providers.templates.resolver import (
    merge_default_facets, DEFAULT_MERGE_PURPOSES,
)


def _facet(t): return SimpleNamespace(text=t, style="")


def test_observe_in_default_merge_purposes():
    assert "observe" in DEFAULT_MERGE_PURPOSES


def test_merge_fills_missing_observe_from_default():
    tpl = SimpleNamespace(id="planner", identity={"act": _facet("PLANNER-SOUL")})
    default = SimpleNamespace(id="default", identity={
        "act": _facet("D-SOUL"), "observe": _facet("D-ROLE"),
        "compact": _facet("D-COMPACT"), "recognize_intent": _facet("D-META")})
    merge_default_facets(tpl, default, DEFAULT_MERGE_PURPOSES)
    assert tpl.identity["observe"].text == "D-ROLE"


def test_merge_does_not_override_existing_observe():
    tpl = SimpleNamespace(id="x", identity={"act": _facet("S"), "observe": _facet("OWN-ROLE")})
    default = SimpleNamespace(id="default", identity={"observe": _facet("D-ROLE")})
    merge_default_facets(tpl, default, DEFAULT_MERGE_PURPOSES)
    assert tpl.identity["observe"].text == "OWN-ROLE"
```

- [ ] **Step 2: 跑测试确认失败**

Run: `uv run pytest tests/unit/test_template_default_observe_merge.py -v`
Expected: FAIL（`test_observe_in_default_merge_purposes` + `test_merge_fills_missing_observe_from_default`：现状 `DEFAULT_MERGE_PURPOSES` 无 observe）。

- [ ] **Step 3: 实现**（resolver.py:30）

把：
```python
DEFAULT_MERGE_PURPOSES = ("compact", "recognize_intent")
```
改为：
```python
DEFAULT_MERGE_PURPOSES = ("compact", "recognize_intent", "observe")
```

- [ ] **Step 4: 跑测试确认通过**

Run: `uv run pytest tests/unit/test_template_default_observe_merge.py -v`
Expected: PASS（3 passed）。

- [ ] **Step 5: 提交**

```bash
git add src/ipmastercowork/providers/templates/resolver.py tests/unit/test_template_default_observe_merge.py
git commit -m "feat(templates): default observe(ROLE) facet 合并——无 ROLE 模板走 LLM observe"
```

---

### Task 5: 抽公共 observe ReAct 骨架（observe 行为不变）

**Files:**
- Modify: `src/ctx_weft/core/loop/steps/observe.py`（`_llm_observe` 的 ReAct 循环抽成模块级 `run_observe_react`，`_llm_observe` 改调用它）
- Test: `tests/unit/test_observe_react_helper.py`（新建）+ 回归 `test_observe_task_compaction.py` 等

**Interfaces:**
- Produces: `async def run_observe_react(state, ctx, *, system: str, messages: list[LLMMessage], tools, request_id_prefix: str, max_rounds: int) -> tuple[str | None, str]`——跑 ReAct，返回 `(last_tool_content, last_text)`：`last_tool_content` 为最后一个被调用控制工具返回的 `ControlResult.content`（无工具调用则 None）；`last_text` 为最后一轮纯文本。**不解读 verdict、不写 task 状态**——状态写是工具副作用，由各调用方的工具决定（observe 绑 report_task_outcome 会写，background 绑 collect_process_report 不写）。
- Consumes（later）: Task 6 的 background observe 调用它。

- [ ] **Step 1: 写失败测试**（`test_observe_react_helper.py`，用 mock LLM stub 验证骨架取到工具 content + 终止）

```python
"""run_observe_react：跑 ReAct，返回最后一个控制工具的 content；不写 task 状态。"""
from __future__ import annotations

import pytest

from ctx_weft.core.loop.steps.observe import run_observe_react

pytestmark = pytest.mark.asyncio


async def test_helper_returns_tool_content(monkeypatch):
    # 见 test_observe_task_compaction.py 的既有 LoopState/LoopContext + mock LLM 装配方式，
    # 构造一个：第一轮 LLM 直接调用一个返回 ControlResult(content="REPORT") 的工具。
    from tests.unit._observe_fixtures import build_react_ctx  # 复用既有 fixture 工厂（若无则在本测试内联构造）
    state, ctx, prompt = await build_react_ctx(tool_content="REPORT")
    content, last_text = await run_observe_react(
        state, ctx, system=prompt.system, messages=list(prompt.messages),
        tools=prompt.tools, request_id_prefix="test", max_rounds=3)
    assert content == "REPORT"
```

> 注：若仓库无 `tests/unit/_observe_fixtures.py`，在本测试文件内联一个最小 LoopState/LoopContext + mock `stream_llm_resilient`（参照 `test_observe_task_compaction.py` / `test_background_observe.py` 的现有构造）使第一轮产出一个 tool_call，gateway 返回 `ControlResult(content="REPORT")`。实现者按既有测试夹具风格补齐。

- [ ] **Step 2: 跑测试确认失败**

Run: `cd ctx-weft && uv run pytest tests/unit/test_observe_react_helper.py -v`
Expected: FAIL（`ImportError: run_observe_react`）。

- [ ] **Step 3: 实现**（observe.py）

把 `_llm_observe`（observe.py:104-247）中 `for round_num in range(max_rounds)` 的整段 ReAct 循环（line 139-244，含流式累积、tool_calls 收集、gateway.invoke、append tool message、terminate 检测）抽成模块级：
```python
async def run_observe_react(state, ctx, *, system, messages, tools, request_id_prefix, max_rounds):
    """共用 observe/background ReAct：跑多轮 LLM，任一轮调用控制工具即取其 ControlResult.content 终止。
    返回 (last_tool_content, last_text)。不解读 verdict、不写 task 状态（状态写由工具副作用决定）。"""
    current_messages = list(messages)
    last_text = ""
    for round_num in range(max_rounds):
        req_id = f"{request_id_prefix}_r{round_num}"
        # … 原 _llm_observe 循环体：emit LLM_REQUEST_STARTED / LLM_PROMPT_SENT，
        #    stream_llm_resilient 累积 accumulated_text/tool_calls/usage，
        #    loop_guard / session.token_used 记账，emit LLM_RESPONSE_FINISHED …
        if not tool_calls:
            break
        current_messages.append(LLMMessage(role="assistant", content=accumulated_text,
            tool_calls=[{"id": tc.id, "name": tc.name, "input": tc.arguments} for tc in tool_calls]))
        tool_content = None
        for tc in tool_calls:
            if ctx.capability_gateway is not None:
                result = await ctx.capability_gateway.invoke(
                    tool_name=tc.name, arguments=tc.arguments, state=state, ctx=ctx, tool_call_id=tc.id)
                content = result.content
                tool_content = content
            else:
                content = f"[Error: CapabilityGateway not configured, tool '{tc.name}' skipped]"
            current_messages.append(LLMMessage(role="tool", content=content, tool_call_id=tc.id))
        if tool_content is not None:
            return tool_content, last_text
    return None, last_text
```
`_llm_observe` 改为：装配 request（不变）→ 调 `run_observe_react(..., request_id_prefix=f"obs_{agent.id}_{state.sequence_counter}", max_rounds=max_rounds)` → 拿 `(tool_content, last_text)`；若 `tool_content is not None`（report_task_outcome 已写 `task.observer_outcome`/`task.process_report`）→ 返回 `Verdict(task_outcome=state.task.observer_outcome or "success", summary=state.task.process_report or last_text[:500], reported=True)`；否则 `return self._rule_observe(state)`。

> 关键：observe 的终止语义从「task.status 变化」改为「调用了控制工具」。report_task_outcome 必然在 observe 装配里被调用且改 status，二者等价——确认 `test_observe_*` 回归全绿即可。

- [ ] **Step 4: 跑测试 + observe 回归**

Run: `cd ctx-weft && uv run pytest tests/unit/test_observe_react_helper.py tests/unit/test_observe_task_compaction.py tests/unit/test_capsule_golden.py -v`
Expected: PASS（helper 测试 + observe/capsule 回归不破）。

- [ ] **Step 5: 提交**

```bash
git add src/ctx_weft/core/loop/steps/observe.py tests/unit/test_observe_react_helper.py
git commit -m "refactor(observe): 抽公共 run_observe_react ReAct 骨架（行为不变）"
```

---

### Task 6: background observe 走 observe ReAct + boundary 分流 + 结果槽

**Files:**
- Modify: `src/ctx_weft/core/loop/steps/background_observe.py`
- Test: `tests/unit/test_background_observe.py`（扩充）

**Interfaces:**
- Consumes: `run_observe_react`（Task 5）、`collect_process_report` / `BACKGROUND_PROCESS_REPORT_NAME`（Task 1）、`purpose="background_observe"` 装配（Task 3）。
- Produces:
  - `launch_background_observe(state, ctx, *, boundary: str)`（增 `boundary` 关键字参数）。
  - 模块级结果槽 `_close_report: dict[str, str]`（close 路径产出，task_id → process_report）+ 取用接口 `pop_close_report(task_id) -> str | None`。
  - close 路径**不**写 memory；打断/暂停路径写 `TASK_COMPACT_SUMMARY`。

- [ ] **Step 1: 写失败测试**（扩充 `test_background_observe.py`；mock LLM 让 background ReAct 调 `collect_process_report` 返回固定文本）

```python
@pytest.mark.asyncio
async def test_background_interrupt_writes_segment_summary(...):
    # boundary="interrupt"：run 后 task 层有一条 TASK_COMPACT_SUMMARY(role=assistant)，内容=工具产出
    ...
    t = launch_background_observe(state, ctx, boundary="interrupt")
    await t
    recs = await ctx.memory.recall_recent(state.scope, [MemoryEventType.TASK_COMPACT_SUMMARY], 100, ctx.provider_ctx)
    assert len(recs) == 1 and recs[0].role == "assistant"


@pytest.mark.asyncio
async def test_background_close_no_memory_writes_slot(...):
    # boundary="finish"：run 后无 TASK_COMPACT_SUMMARY；结果落 _close_report 槽
    from ctx_weft.core.loop.steps.background_observe import pop_close_report
    t = launch_background_observe(state, ctx, boundary="finish")
    await t
    recs = await ctx.memory.recall_recent(state.scope, [MemoryEventType.TASK_COMPACT_SUMMARY], 100, ctx.provider_ctx)
    assert recs == []
    assert pop_close_report(state.task.id) is not None


@pytest.mark.asyncio
async def test_background_zero_state_pollution(...):
    # 跑 background observe 前后，task.status/actor_done/observer_outcome 不变
    before = (state.task.status, state.task.actor_done, state.task.observer_outcome)
    await launch_background_observe(state, ctx, boundary="finish")
    assert (state.task.status, state.task.actor_done, state.task.observer_outcome) == before
```

> 用既有 `test_background_observe.py` 的夹具构造 state/ctx；mock `stream_llm_resilient` 使第一轮调 `collect_process_report(task_process_report="段总结X")`，gateway 走真实 control_capability 分发（或 mock 返回 `ControlResult(content="段总结X")`）。实现者按既有夹具补齐 mock。

- [ ] **Step 2: 跑测试确认失败**

Run: `cd ctx-weft && uv run pytest tests/unit/test_background_observe.py -v`
Expected: FAIL（`launch_background_observe` 无 `boundary` 参数 / `pop_close_report` 不存在 / close 仍写 summary）。

- [ ] **Step 3: 实现**（background_observe.py 重写 `_run_background_observe` + `launch_background_observe`）

```python
_close_report: dict[str, str] = {}

def pop_close_report(task_id: str) -> str | None:
    return _close_report.pop(task_id, None)

_CLOSE_BOUNDARIES = {"finish", "normal"}

async def _run_background_observe(state, ctx, boundary):
    from ctx_weft.core.assembler import ContextRequest
    from ctx_weft.core.loop.steps.observe import run_observe_react
    async with _lock_for(state.task.id):
        try:
            agent = state.agent
            bound_caps = (ctx.capability_cache.get(agent.id)
                          if ctx.capability_cache and ctx.capability_cache.has_agent(agent.id) else [])
            request = ContextRequest(
                purpose="background_observe", scope=state.scope, task=state.task, agent=agent,
                session=state.session, template=state.extra.get("template"),
                bound_capabilities=bound_caps, actor_transcript=state.transcript,
                extra={"observe_boundary": boundary})
            prompt = await ctx.assembler.assemble(request)
            content, _ = await run_observe_react(
                state, ctx, system=prompt.system, messages=list(prompt.messages),
                tools=prompt.tools, request_id_prefix=f"bgobs_{state.task.id}",
                max_rounds=agent.loop_config.max_turns_per_observe)
            report = content or "[Context compacted]"
            if boundary in _CLOSE_BOUNDARIES:
                _close_report[state.task.id] = report          # 不写 memory（不变量 3）
            else:
                await ctx.memory.apply_compact(
                    scope=state.scope, summary=report, keep_last=0, ctx=ctx.provider_ctx,
                    layer=MemoryLayer.TASK, protect_types=(MemoryEventType.USER_PROMPT,))
        except Exception:
            logger.exception("background observe failed (ignored); segment kept raw")

def launch_background_observe(state, ctx, *, boundary):
    snapshot = dataclasses.replace(state)
    task = asyncio.create_task(_run_background_observe(snapshot, ctx, boundary))
    _task_pending[state.task.id] = task
    tm = getattr(ctx, "task_manager", None)
    if tm is not None and hasattr(tm, "track_background"):
        tm.track_background(task)
    else:
        _orphan_tasks.add(task); task.add_done_callback(_orphan_tasks.discard)
    task.add_done_callback(lambda t, tid=state.task.id: _clear_pending(t, tid))
    return task
```

- [ ] **Step 4: 跑测试确认通过**

Run: `cd ctx-weft && uv run pytest tests/unit/test_background_observe.py -v`
Expected: PASS（含新增 3 条 + 既有触发/注册测试）。

- [ ] **Step 5: 提交**

```bash
git add src/ctx_weft/core/loop/steps/background_observe.py tests/unit/test_background_observe.py
git commit -m "feat(bg-observe): 走 observe ReAct + boundary 分流（打断写段摘要 / close 落结果槽）"
```

---

### Task 7: 6 个触发点传 boundary

**Files:**
- Modify: `src/ctx_weft/core/loop/steps/act.py`（5 处 `launch_background_observe`）、`src/ctx_weft/core/loop/steps/observe.py:83`（1 处）
- Test: `tests/unit/test_background_observe_boundary_wiring.py`（新建，monkeypatch 捕获 boundary）

**Interfaces:**
- Consumes: `launch_background_observe(state, ctx, *, boundary)`（Task 6）。
- Produces: act 4 处 interrupt-park 传 `boundary="interrupt"`；act 纯文本暂停传 `boundary="plain_text"`；observe 传 `boundary="finish" if act_exit_reason=="actor_done" else "normal"`。

- [ ] **Step 1: 写失败测试**（monkeypatch `launch_background_observe` 捕获 boundary）

```python
"""触发点 boundary 接线：act interrupt/plain_text、observe finish/normal。"""
from __future__ import annotations
import pytest

# 用 test_background_observe.py / act/observe 既有夹具驱动一次对应路径，
# monkeypatch launch_background_observe 记录调用的 boundary kwarg，断言其值。

@pytest.mark.asyncio
async def test_observe_actor_done_boundary_is_finish(monkeypatch):
    captured = {}
    import ctx_weft.core.loop.steps.background_observe as bg
    monkeypatch.setattr(bg, "launch_background_observe",
                        lambda state, ctx, *, boundary: captured.setdefault("b", boundary))
    # 构造 root task + act_exit_reason="actor_done" → 跑 ObserveStep.execute
    ...
    assert captured["b"] == "finish"


@pytest.mark.asyncio
async def test_observe_normal_boundary_is_normal(monkeypatch):
    # act_exit_reason="normal" → boundary=="normal"
    ...
```

> 实现者用既有 observe/act 夹具构造对应 exit_reason / interrupt 路径；act 5 处可各驱动一次或抽样验证 interrupt/plain_text 两值。

- [ ] **Step 2: 跑测试确认失败**

Run: `cd ctx-weft && uv run pytest tests/unit/test_background_observe_boundary_wiring.py -v`
Expected: FAIL（现状 `launch_background_observe(state, ctx)` 无 boundary，调用签名报错或捕获缺失）。

- [ ] **Step 3: 实现**

`observe.py:83-84`：
```python
        if state.act_exit_reason in ("normal", "actor_done") and _is_own_root(state.task):
            from ctx_weft.core.loop.steps.background_observe import launch_background_observe
            launch_background_observe(
                state, ctx,
                boundary="finish" if state.act_exit_reason == "actor_done" else "normal")
```
`act.py` 的 4 处 interrupt-park（217-219 / 334-335 / 350-351 / 553-554）：每处 `launch_background_observe(state, ctx)` → `launch_background_observe(state, ctx, boundary="interrupt")`。
`act.py` 纯文本暂停（389-390）：→ `launch_background_observe(state, ctx, boundary="plain_text")`。

- [ ] **Step 4: 跑测试确认通过**

Run: `cd ctx-weft && uv run pytest tests/unit/test_background_observe_boundary_wiring.py -v`
Expected: PASS。

- [ ] **Step 5: 提交**

```bash
git add src/ctx_weft/core/loop/steps/act.py src/ctx_weft/core/loop/steps/observe.py tests/unit/test_background_observe_boundary_wiring.py
git commit -m "feat(loop): 6 触发点传 boundary（act interrupt/plain_text、observe finish/normal）"
```

---

### Task 8: finalize close 路径 A1（机会性用结果槽 + 异步替换）

**Files:**
- Modify: `src/ctx_weft/core/loop/steps/finalize.py`（`_synthesize_dispatch_pair` 取结果槽 / 占位 + 标记）、`src/ctx_weft/core/loop/steps/background_observe.py`（close 回调：finalize 已合成则替换 finish 对 tool 记录）
- Test: `tests/unit/test_close_process_report_a1.py`（新建）

**Interfaces:**
- Consumes: `pop_close_report(task_id)` / `_close_report`（Task 6）。
- Produces:
  - finalize `_synthesize_dispatch_pair`：先 `pop_close_report(task.id)`，命中→用作 Process Report；未命中→薄占位（`mem_content`）+ 在模块级 `_close_synth[task.id] = (tool_call_id, scope)` 登记「finish 对已合成、待替换」。
  - background close 回调（`_run_background_observe` 末尾 close 分支）：若 `_close_synth` 已登记（finalize 先到）→ `supersede` 旧 finish-对 tool 记录 + `ingest` 新（同 tool_call_id）；否则写 `_close_report`（finalize 后到自取）。

- [ ] **Step 1: 写失败测试**（`test_close_process_report_a1.py`）

```python
"""close 路径 A1：机会性用结果槽 / 占位+异步替换 finish 对 Process Report。"""
from __future__ import annotations
import pytest

pytestmark = pytest.mark.asyncio


async def test_a1_slot_hit_uses_background_report(...):
    # background 先完成、_close_report[task.id]="好报告" → _synthesize_dispatch_pair
    # 合成的 finish 对 tool 记录 content == "Process Report: 好报告"，且无后续替换
    ...

async def test_a1_placeholder_then_async_replace(...):
    # 结果槽空 → finish 对先用薄占位；随后 background close 回调替换：
    # 旧 tool 记录 superseded，新记录 content=="Process Report: 好报告"，tool_call_id 配对不悬挂
    ...
```

> 用 `test_capsule_golden.py` 的胶囊夹具构造 root task close；分别预置/不预置 `_close_report` 槽，断言 finish 对 Process Report 来源与替换行为。

- [ ] **Step 2: 跑测试确认失败**

Run: `cd ctx-weft && uv run pytest tests/unit/test_close_process_report_a1.py -v`
Expected: FAIL（现状 `_synthesize_dispatch_pair` 只用 `mem_content`，无结果槽 / 替换逻辑）。

- [ ] **Step 3: 实现**

`finalize.py` `_synthesize_dispatch_pair`（finish 对合成处，约 line 270-311）：在算 `report_only` 前插入——
```python
    from ctx_weft.core.loop.steps.background_observe import pop_close_report, register_close_synth
    bg_report = pop_close_report(task.id)
    if bg_report is not None:
        report_only = bg_report                      # 机会性命中：直接用 background 产出
    else:
        # 现状逻辑：从 mem_content rsplit 取薄占位
        report_only = mem_content.rsplit(_SEP, 1)[-1] if _SEP in mem_content else mem_content
        register_close_synth(task.id, tool_call_id, scope)   # 登记待替换（finish 对 tool 记录刚写）
```
（`tool_call_id` 为合成 finish 对时生成的那个；`scope` 为 agent scope。`register_close_synth` 在 background_observe.py 定义，存 `_close_synth[task_id] = (tool_call_id, scope)`。）

`background_observe.py` close 分支（`_run_background_observe` 内 `boundary in _CLOSE_BOUNDARIES` 时）改为：
```python
            if boundary in _CLOSE_BOUNDARIES:
                synth = _close_synth.pop(state.task.id, None)
                if synth is not None:
                    tool_call_id, scope = synth
                    await _replace_finish_report(ctx, scope, state.task.id, tool_call_id, report)
                else:
                    _close_report[state.task.id] = report
```
新增 `_close_synth: dict[str, tuple[str, object]] = {}` + `register_close_synth(task_id, tool_call_id, scope)` + `_replace_finish_report`：在 agent scope 召回 `AGENT_CONVERSATION_TURN`（role=tool、`origin_task_id==task_id`、content 以 `Process Report:` 开头），`supersede` 旧记录，`ingest` 新记录（同 `tool_call_id`、content=`f"Process Report: {report}"`，保留既有 `[outcome=fail] ` 前缀逻辑）。

- [ ] **Step 4: 跑测试 + 胶囊回归**

Run: `cd ctx-weft && uv run pytest tests/unit/test_close_process_report_a1.py tests/unit/test_capsule_golden.py tests/unit/test_capsule_interleaved.py -v`
Expected: PASS（A1 两路径 + 胶囊 golden 不破）。

- [ ] **Step 5: 全量回归 + 提交**

Run: `cd ctx-weft && uv run pytest -q` 然后仓根 `uv run pytest -q`
Expected: core 全绿；host 仅既有无关失败（如 `test_skills_pull_store.py`，与本改动无关）。
```bash
git add src/ctx_weft/core/loop/steps/finalize.py src/ctx_weft/core/loop/steps/background_observe.py tests/unit/test_close_process_report_a1.py
git commit -m "feat(finalize): close 路径 A1——机会性用 bg 产出 / 占位+异步替换 Process Report"
```

---

## 自检（writing-plans self-review）

- **Spec coverage**：§3.1 facet 回退→T2、cue→T3；§3.2 精简工具→T1；§3.3 boundary 标签→T3(描述)+T7(接线)；§3.4 bg observe 改造→T6（复用 T5 骨架）；§3.5 _rule_observe 占位→保留（T5 `_llm_observe` 兜底仍调 `_rule_observe`，未删）+ T7（fire boundary）；§3.6 finalize A1→T8；§3.7 失败降级→T6（except 吞）+T8（槽空用占位）；§3.8 default ROLE→T4；§3.9 不动→各 task 不碰 COMPACT/`_maybe_compact_task`/非 root。
- **不变量守护**：不变量1（零状态写）→ T1 + T6 `test_background_zero_state_pollution`；不变量3（分流）→ T6 两测；不变量5/6（机械退出/非 root 不变）→ T5 observe 回归 + 不碰相关路径。
- **Placeholder scan**：测试夹具处标注「实现者按既有夹具补齐 mock」——非占位逻辑，是复用既有测试基建的明确指引（T5/T6/T7/T8 的 LLM mock 依赖 `test_observe_task_compaction.py`/`test_background_observe.py` 现成夹具）。所有生产代码块完整。
- **Type consistency**：`launch_background_observe(state, ctx, *, boundary)`、`pop_close_report(task_id)`、`register_close_synth(task_id, tool_call_id, scope)`、`run_observe_react(...) -> (content, last_text)`、`BACKGROUND_PROCESS_REPORT_NAME`、`_BACKGROUND_BOUNDARY_DESC` 全程一致。
- **依赖序**：T1/T2/T3/T4 独立 → T5（骨架）→ T6（用 T1/T3/T5）→ T7（用 T6 签名）→ T8（用 T6 槽）。
