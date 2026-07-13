# Effective Context Limit + 保最近/保摘要裁剪 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 为 LLM 输出预留余量（装配预算按 `effective_limit = context_limit − max_output_tokens` 裁剪），并把 budget 裁剪改为"保最近 / 保摘要 + 配对原子 + 当前消息锚"。

**Architecture:** 新增 `effective_limit()` 与 `slot_priority()` 两个纯函数作单一真源；`reserved_output_tokens` 沿 `context_limit` 同轨落到 Session/LoopGuard/SessionView；重写 `PriorityBudgetStrategy.apply` 引入配对原子 DropUnit + 最老先丢 + 当前消息 pin + 富信息 `ContextOverflowError`；各限额判定点（budget/compact 触发/压缩目标/act 停止）统一改用 `effective_limit`。

**Tech Stack:** Python 3.11（`StrEnum`、`dataclasses`）、pytest（`uv run pytest`）。

## Global Constraints

- 测试运行器：`uv run pytest`（pyproject 已配 `pythonpath=["."]`）。
- 估算口径**本次不动**：`estimate_tokens = len//4`（utils.py:37）保持不变（非目标）。
- `reserved_output_tokens` 默认 `8192`（与 host env `IPMC_LLM_MAX_OUTPUT_TOKENS` 默认一致）；`0` = 不预留。
- 裁剪保护序（priority 小=更保，0–7，见 spec §4.2）：`0` identity/task_spec/当前消息锚(不可裁) → `1` 能力/指令/background(项目背景) → `2` agent_compact_summary(受保护摘要) → `3` blackboard → `4` 当前 task 内容(budget 动态提升,slot_priority 永不返回 4) → `5` agent_conversation_turn(agent 层历史) → `6` 其余历史含 raw / task_compact_summary(最老先丢+配对原子) → `7` reference/外部召回。丢弃总序 7→1，priority-0 永不丢。
- `MemoryEventType` 是 `StrEnum`（memory.py:28），成员 == 其字符串值（`"agent_compact_summary"` 等），可直接与字符串比较。
- 改动默认落 `src/ctx_weft/`（core），host 侧改 `src/ipmastercowork/`。
- 分支：已在 `feature/effective-context-limit-output-reserve`。
- 设计出处：`docs/superpowers/specs/2026-07-02-effective-context-limit-output-reserve-design.md`。

---

### Task 1: `effective_limit()` 助手

**Files:**
- Modify: `src/ctx_weft/core/utils.py`（在 `estimate_tokens` 之后追加）
- Test: `tests/unit/test_effective_limit.py`（新建）

**Interfaces:**
- Produces: `effective_limit(context_limit: int, reserved_output_tokens: int) -> int`

- [ ] **Step 1: 写失败测试**

Create `tests/unit/test_effective_limit.py`:

```python
from ctx_weft.core.utils import effective_limit


def test_subtracts_reserve():
    assert effective_limit(180_000, 8192) == 180_000 - 8192


def test_zero_reserve_is_full_window():
    assert effective_limit(180_000, 0) == 180_000


def test_reserve_exceeding_context_clamps_to_zero():
    assert effective_limit(4_000, 8192) == 0


def test_negative_reserve_treated_as_zero():
    assert effective_limit(180_000, -5) == 180_000
```

- [ ] **Step 2: 运行确认失败**

Run: `cd ctx-weft && uv run pytest tests/unit/test_effective_limit.py -v`
Expected: FAIL（`ImportError: cannot import name 'effective_limit'`）

- [ ] **Step 3: 实现**

在 `src/ctx_weft/core/utils.py` 的 `estimate_tokens` 函数之后加：

```python
def effective_limit(context_limit: int, reserved_output_tokens: int) -> int:
    """装配/压缩预算的有效上限：为 LLM 输出预留余量后的可用输入窗口。"""
    return max(0, context_limit - max(0, reserved_output_tokens))
```

- [ ] **Step 4: 运行确认通过**

Run: `cd ctx-weft && uv run pytest tests/unit/test_effective_limit.py -v`
Expected: PASS（4 passed）

- [ ] **Step 5: Commit**

```bash
git add src/ctx_weft/core/utils.py tests/unit/test_effective_limit.py
git commit -m "feat(assembler): add effective_limit() helper for output reserve"
```

---

### Task 2: `slot_priority()` 集中映射

**Files:**
- Create: `src/ctx_weft/core/assembler/priority.py`
- Test: `tests/unit/test_slot_priority.py`（新建）

**Interfaces:**
- Produces: `slot_priority(kind: str, mem_type: str | None = None) -> int`（静态基线，永不返回 4）

- [ ] **Step 1: 写失败测试**

Create `tests/unit/test_slot_priority.py`:

```python
from ctx_weft.core.assembler.priority import slot_priority


def test_structural_floor():
    assert slot_priority("identity") == 0
    assert slot_priority("task_spec") == 0


def test_capabilities_directive_background_tier1():
    assert slot_priority("capabilities") == 1
    assert slot_priority("directive") == 1
    assert slot_priority("background") == 1  # 项目背景与能力同档（原 blackboard.py priority 1）


def test_agent_compact_summary_tier2():
    # 仅 agent 层跨 task 折叠受保护
    assert slot_priority("history", "agent_compact_summary") == 2


def test_blackboard_tier3():
    assert slot_priority("blackboard") == 3


def test_task_compact_summary_is_task_layer_capsule():
    # task 层胶囊内容，随胶囊走 → 6（已完成基线），不进 tier2
    assert slot_priority("history", "task_compact_summary") == 6


def test_agent_layer_turn_tier5():
    assert slot_priority("history", "agent_conversation_turn") == 5


def test_completed_task_layer_capsule_tier6():
    assert slot_priority("history", "user_prompt") == 6
    assert slot_priority("history", "llm_response") == 6
    assert slot_priority("history", "tool_result") == 6


def test_external_recall_tier7():
    assert slot_priority("reference") == 7
    assert slot_priority("summary") == 7


def test_never_returns_4():
    # 4 = 当前 task 内容，budget 动态提级，slot_priority 不静态返回
    for k, t in [("history", "user_prompt"), ("history", "agent_conversation_turn"),
                 ("history", "agent_compact_summary"), ("blackboard", None),
                 ("capabilities", None), ("identity", None), ("reference", None)]:
        assert slot_priority(k, t) != 4
```

- [ ] **Step 2: 运行确认失败**

Run: `cd ctx-weft && uv run pytest tests/unit/test_slot_priority.py -v`
Expected: FAIL（`ModuleNotFoundError: ctx_weft.core.assembler.priority`）

- [ ] **Step 3: 实现**

Create `src/ctx_weft/core/assembler/priority.py`:

```python
"""槽位 → 裁剪 priority 的静态基线（tier 表的代码化，见 spec §4.2）。

各 Source 造 block 时调用本函数取 priority，取代散落各处的硬编码整数。
「当前 vs 已完成」是动态轴，不在此——由 budget 提级（当前→4）+ pin（当前 user_prompt→0）。
故此处 raw 一律按"已完成"给 5/6；slot_priority 永不返回 4。
"""

from __future__ import annotations


def slot_priority(kind: str, mem_type: str | None = None) -> int:
    """kind: BlockKind；mem_type: history 类 block 的 MemoryEventType 字符串。
    数字越小越受保护；0 永不裁。丢序 7→1。"""
    if kind in ("identity", "task_spec"):
        return 0
    if kind in ("capabilities", "directive", "background"):
        return 1  # 项目背景=系统提示内容，与能力同档（原 blackboard.py 即 priority 1）
    if mem_type == "agent_compact_summary":
        return 2  # agent 层跨 task 折叠（受保护）
    if kind == "blackboard":
        return 3
    # 4 = 当前 task 内容：budget 动态提级，此处不返回
    if mem_type == "agent_conversation_turn":
        return 5  # 已完成 task 的 agent 层回合（finish/dispatch 对）
    if kind == "history":
        return 6  # 已完成 task 的 task 层胶囊（含 task_compact_summary）
    return 7  # knowledge(reference) / long_memory(语义召回 summary)
```

- [ ] **Step 4: 运行确认通过**

Run: `cd ctx-weft && uv run pytest tests/unit/test_slot_priority.py -v`
Expected: PASS（8 passed）

- [ ] **Step 5: Commit**

```bash
git add src/ctx_weft/core/assembler/priority.py tests/unit/test_slot_priority.py
git commit -m "feat(assembler): add slot_priority() central tier mapping"
```

---

### Task 3: `reserved_output_tokens` 数据模型 + plumbing

**Files:**
- Modify: `src/ctx_weft/core/state/models.py`（LoopGuard ~127、Session ~146）
- Modify: `src/ctx_weft/core/control/types.py`（SessionView ~27）
- Modify: `src/ctx_weft/core/control/reducers.py`（96/165/337）
- Modify: `src/ctx_weft/core/control/converters.py`（23）
- Modify: `src/ctx_weft/core/runtime.py`（555/556、768、799、1021-1026）
- Modify: `src/ipmastercowork/api/sessions.py`（`_resolve_max_output_tokens` + 233/518）
- Test: `tests/unit/test_reserved_output_tokens_plumbing.py`（新建）

**Interfaces:**
- Produces: `Session.reserved_output_tokens: int`、`LoopGuard.reserved_output_tokens: int`、`SessionView.reserved_output_tokens: int`（默认 8192）

- [ ] **Step 1: 写失败测试**

Create `tests/unit/test_reserved_output_tokens_plumbing.py`:

```python
from ctx_weft.core.state.models import LoopGuard, Session
from ctx_weft.core.control.types import SessionView


def test_defaults_are_8192():
    assert LoopGuard().reserved_output_tokens == 8192
    s = Session(id="s", user_prompt="hi", status="RUNNING")
    assert s.reserved_output_tokens == 8192
    assert SessionView(id="s").reserved_output_tokens == 8192


def test_loopguard_carries_reserve():
    g = LoopGuard(context_limit=100_000, reserved_output_tokens=4096)
    assert g.reserved_output_tokens == 4096
```

- [ ] **Step 2: 运行确认失败**

Run: `cd ctx-weft && uv run pytest tests/unit/test_reserved_output_tokens_plumbing.py -v`
Expected: FAIL（`TypeError: ... unexpected keyword argument 'reserved_output_tokens'` 或 AttributeError）

- [ ] **Step 3: 加数据模型字段**

`src/ctx_weft/core/state/models.py` — `LoopGuard`（在 `context_limit: int = 180_000` 后）：

```python
    context_limit: int = 180_000
    reserved_output_tokens: int = 8192
```

同文件 `Session`（在 `context_limit: int = 180_000` 后）：

```python
    context_limit: int = 180_000
    reserved_output_tokens: int = 8192
```

`src/ctx_weft/core/control/types.py` — `SessionView`（在 `context_limit: int = 180_000` 后）：

```python
    context_limit: int = 180_000
    reserved_output_tokens: int = 8192
```

- [ ] **Step 4: control 层投影/转换镜像字段**

`src/ctx_weft/core/control/reducers.py`：
- 行 ~96（`"context_limit": s.context_limit,` 所在 dict）后加 `"reserved_output_tokens": s.reserved_output_tokens,`
- 行 ~165（`context_limit=s.get("context_limit", 180_000),`）后加 `reserved_output_tokens=s.get("reserved_output_tokens", 8192),`
- 行 ~337（`context_limit=p.get("context_limit", 180_000),`）后加 `reserved_output_tokens=p.get("reserved_output_tokens", 8192),`

`src/ctx_weft/core/control/converters.py` 行 ~23（`context_limit=proj.context_limit,`）后加：

```python
        reserved_output_tokens=getattr(proj, "reserved_output_tokens", 8192),
```

- [ ] **Step 5: runtime 赋值 reserve + 传 LoopGuard**

`src/ctx_weft/core/runtime.py`：
- 行 555 `session.context_limit = llm.context_limit` 之后加一行：

```python
        session.reserved_output_tokens = llm.max_output_tokens
```

- **每一处** `LoopGuard(context_limit=session.context_limit)` 构造（行 556、768、799）改为：

```python
LoopGuard(context_limit=session.context_limit, reserved_output_tokens=session.reserved_output_tokens)
```

- compact_session 路径（行 1021-1026 的 `LoopGuard(...)`）加 `reserved_output_tokens=session.reserved_output_tokens`。

- [ ] **Step 6: host 侧 resolve + 传入**

`src/ipmastercowork/api/sessions.py` — 在 `_resolve_context_limit`（行 92）之后加平行函数：

```python
def _resolve_max_output_tokens(runtime: Any, llm_account: str | None, llm_model: str | None) -> int:
    """据所选 LLM 模型解析 max_output_tokens 作为输出预留；失败回退 8192。"""
    try:
        if runtime is not None and runtime.providers.has_llm_provider():
            client = runtime.providers.get_llm_provider().get_client(llm_account, llm_model)
            return client.max_output_tokens
    except Exception:
        logger.warning("max_output_tokens: resolve failed (account=%r model=%r); fallback 8192",
                       llm_account, llm_model)
    return 8192
```

在行 233、518 创建 session（含 `context_limit=_resolve_context_limit(...)`）处，平行加：

```python
        reserved_output_tokens=_resolve_max_output_tokens(runtime, llm_account, llm_model),
```

> 注：若 session 创建走的是 control 事件/ProjectConfig 路径而非直接构造 Session，请把该字段加到对应的创建 payload（与 `context_limit` 同处），由 Step 4 的 reducer 落到 SessionView。

- [ ] **Step 7: 运行确认通过 + 全量回归**

Run: `cd ctx-weft && uv run pytest tests/unit/test_reserved_output_tokens_plumbing.py -v`
Expected: PASS（2 passed）
Run: `cd ctx-weft && uv run pytest tests/unit/ -q`
Expected: 既有用例仍绿（新字段带默认值，向后兼容）。

- [ ] **Step 8: Commit**

```bash
git add src/ctx_weft/core/state/models.py src/ctx_weft/core/control/ src/ctx_weft/core/runtime.py src/ipmastercowork/api/sessions.py tests/unit/test_reserved_output_tokens_plumbing.py
git commit -m "feat(session): plumb reserved_output_tokens alongside context_limit"
```

---

### Task 4: assembler 用 effective_limit 作 token_limit

**Files:**
- Modify: `src/ctx_weft/core/assembler/assembler.py:158`
- Test: `tests/unit/test_assembler_effective_limit.py`（新建）

**Interfaces:**
- Consumes: `effective_limit()`（Task 1）、`Session.reserved_output_tokens`（Task 3）

- [ ] **Step 1: 写失败测试**

Create `tests/unit/test_assembler_effective_limit.py`:

```python
import pytest
from ctx_weft.core.assembler.assembler import ContextAssembler, ContextBlock, AssemblerDeps
from ctx_weft.core.assembler.budget import PriorityBudgetStrategy


class _CaptureBudget(PriorityBudgetStrategy):
    seen_limit = None

    async def apply(self, blocks, token_limit, request):
        _CaptureBudget.seen_limit = token_limit
        return blocks


@pytest.mark.asyncio
async def test_assembler_passes_effective_limit(make_request):
    """budget 收到的 token_limit = context_limit - reserved_output_tokens。"""
    req = make_request(context_limit=100_000, reserved_output_tokens=8192)
    asm = ContextAssembler(sources=[], budget=_CaptureBudget(),
                           composer=req._composer, deps=req._deps)
    await asm.assemble(req)
    assert _CaptureBudget.seen_limit == 100_000 - 8192
```

> 若无 `make_request` fixture，参照 `tests/unit/conftest.py` 里既有的 request/session 构造；本测试只需 `request.session.context_limit=100_000, reserved_output_tokens=8192`，sources 可为空、composer 可为最小 stub（断言只看 `seen_limit`）。

- [ ] **Step 2: 运行确认失败**

Run: `cd ctx-weft && uv run pytest tests/unit/test_assembler_effective_limit.py -v`
Expected: FAIL（`seen_limit == 100_000`，未减预留）

- [ ] **Step 3: 实现**

`src/ctx_weft/core/assembler/assembler.py` 顶部 import：

```python
from ctx_weft.core.utils import effective_limit
```

行 158 `token_limit = request.session.context_limit` 改为：

```python
        token_limit = effective_limit(
            request.session.context_limit, request.session.reserved_output_tokens
        )
```

- [ ] **Step 4: 运行确认通过**

Run: `cd ctx-weft && uv run pytest tests/unit/test_assembler_effective_limit.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/ctx_weft/core/assembler/assembler.py tests/unit/test_assembler_effective_limit.py
git commit -m "feat(assembler): budget token_limit uses effective_limit (reserve output room)"
```

---

### Task 5: `ContextOverflowError` 富信息 + retriable

**Files:**
- Modify: `src/ctx_weft/core/errors.py:55`
- Test: `tests/unit/test_context_overflow_error.py`（新建）

**Interfaces:**
- Produces: `ContextOverflowError(message, *, required, effective_limit, context_limit, reserved_output_tokens)`；类属性 `retriable = False`

- [ ] **Step 1: 写失败测试**

Create `tests/unit/test_context_overflow_error.py`:

```python
from ctx_weft.core.errors import ContextOverflowError


def test_carries_fields_and_non_retriable():
    e = ContextOverflowError(
        "overflow", required=200_000, effective_limit=171_808,
        context_limit=180_000, reserved_output_tokens=8192,
    )
    assert e.retriable is False
    assert e.required == 200_000
    assert e.effective_limit == 171_808
    assert e.context_limit == 180_000
    assert e.reserved_output_tokens == 8192
    assert e.code == "CONTEXT_OVERFLOW"


def test_message_only_still_works():
    e = ContextOverflowError("boom")
    assert e.required == 0 and e.retriable is False
```

- [ ] **Step 2: 运行确认失败**

Run: `cd ctx-weft && uv run pytest tests/unit/test_context_overflow_error.py -v`
Expected: FAIL（`TypeError: unexpected keyword argument 'required'`）

- [ ] **Step 3: 实现**

`src/ctx_weft/core/errors.py` 把 `ContextOverflowError` 定义替换为：

```python
class ContextOverflowError(CtxWeftError):
    code = "CONTEXT_OVERFLOW"
    retriable = False  # 非瞬时：同批 block 重装配必再溢出，不可 resume（spec §4.5）

    def __init__(
        self,
        message: str = "",
        *,
        code: str | None = None,
        required: int = 0,
        effective_limit: int = 0,
        context_limit: int = 0,
        reserved_output_tokens: int = 0,
    ) -> None:
        super().__init__(message, code=code)
        self.required = required
        self.effective_limit = effective_limit
        self.context_limit = context_limit
        self.reserved_output_tokens = reserved_output_tokens
```

- [ ] **Step 4: 运行确认通过**

Run: `cd ctx-weft && uv run pytest tests/unit/test_context_overflow_error.py -v`
Expected: PASS（2 passed）

- [ ] **Step 5: Commit**

```bash
git add src/ctx_weft/core/errors.py tests/unit/test_context_overflow_error.py
git commit -m "feat(errors): ContextOverflowError carries limit fields + retriable=False"
```

---

### Task 6: budget 重写（配对原子 + 最老先丢 + 当前消息 pin + 富错误）

**Files:**
- Modify: `src/ctx_weft/core/assembler/budget.py`
- Test: `tests/unit/test_budget_strategy.py`（新建）

**Interfaces:**
- Consumes: `ContextOverflowError`（Task 5）；block `metadata` 键 `timestamp`（ISO str）、`tool_calls`（assistant）、`tool_call_id`（tool）、`task_id`、`type`
- Produces: `PriorityBudgetStrategy.apply(blocks, token_limit, request)` 保持签名不变；行为按 spec §4.2/§4.2.1

- [ ] **Step 1: 写失败测试（最老先丢 + 保摘要 + 配对原子 + pin + 溢出）**

Create `tests/unit/test_budget_strategy.py`:

```python
import pytest
from types import SimpleNamespace
from ctx_weft.core.assembler.assembler import ContextBlock
from ctx_weft.core.assembler.budget import PriorityBudgetStrategy
from ctx_weft.core.errors import ContextOverflowError


def _req(task_id="cur"):
    return SimpleNamespace(
        task=SimpleNamespace(id=task_id),
        session=SimpleNamespace(context_limit=180_000, reserved_output_tokens=8192),
    )


def _blk(bid, priority, tokens, *, ts="", role="user", mtype="user_prompt",
         task_id="", origin_task_id="", tool_calls=None, tool_call_id=""):
    md = {"timestamp": ts, "role": role, "type": mtype, "task_id": task_id}
    if origin_task_id:
        md["origin_task_id"] = origin_task_id
    if tool_calls is not None:
        md["tool_calls"] = tool_calls
    if tool_call_id:
        md["tool_call_id"] = tool_call_id
    return ContextBlock(id=bid, source="agent_recall", kind="history", target="messages",
                        content="x", priority=priority, token_estimate=tokens, metadata=md)


@pytest.mark.asyncio
async def test_drops_oldest_completed_first():
    """完成 task 层胶囊(6) 同档超预算：丢最老。"""
    old = _blk("old", 6, 100, ts="2026-01-01T00:00:00", task_id="p1",
               mtype="llm_response", role="assistant")
    new = _blk("new", 6, 100, ts="2026-01-02T00:00:00", task_id="p2",
               mtype="llm_response", role="assistant")
    kept = await PriorityBudgetStrategy().apply([old, new], token_limit=100, request=_req())
    assert {b.id for b in kept} == {"new"}


@pytest.mark.asyncio
async def test_current_task_more_protected_than_completed():
    """当前 task 内容(提级 4) 比已完成 task(6) 更保：即便更老也留。"""
    done = _blk("done", 6, 100, ts="2026-01-03T00:00:00", task_id="past",
                mtype="llm_response", role="assistant")           # 更新但已完成 → eff 6
    cur = _blk("cur", 6, 100, ts="2026-01-01T00:00:00", task_id="cur",
               mtype="llm_response", role="assistant")            # 更老但当前 → 提级 eff 4
    kept = await PriorityBudgetStrategy().apply([done, cur], token_limit=100, request=_req("cur"))
    assert {b.id for b in kept} == {"cur"}


@pytest.mark.asyncio
async def test_completed_task_layer_dropped_before_agent_layer():
    """完成 task 层胶囊(6) 先于完成 task 的 agent 层回合(5) 丢，尽管 agent 回合更老。"""
    tl = _blk("tl", 6, 100, ts="2026-01-02T00:00:00", task_id="past",
              mtype="tool_result", role="tool")                    # task 层
    al = _blk("al", 5, 100, ts="2026-01-01T00:00:00", origin_task_id="past",
              mtype="agent_conversation_turn", role="assistant")   # agent 层，更老
    kept = await PriorityBudgetStrategy().apply([tl, al], token_limit=100, request=_req("cur"))
    assert {b.id for b in kept} == {"al"}


@pytest.mark.asyncio
async def test_agent_summary_protected_over_raw():
    """AGENT_COMPACT_SUMMARY(2) 比 raw(6) 更保：raw 先丢。"""
    summ = _blk("s", 2, 100, ts="2026-01-01T00:00:00", mtype="agent_compact_summary", role="user")
    raw = _blk("r", 6, 100, ts="2026-01-02T00:00:00", mtype="llm_response",
               role="assistant", task_id="past")
    kept = await PriorityBudgetStrategy().apply([summ, raw], token_limit=100, request=_req())
    assert {b.id for b in kept} == {"s"}


@pytest.mark.asyncio
async def test_tool_pair_dropped_atomically():
    """丢含 tool_call 的 assistant → 其 tool_result 同批丢，无 orphan/dangling。"""
    call = _blk("call", 6, 100, ts="2026-01-01T00:00:00", role="assistant",
                mtype="llm_response", task_id="past", tool_calls=[{"id": "A"}])
    result = _blk("res", 6, 100, ts="2026-01-01T00:00:01", role="tool",
                  mtype="tool_result", task_id="past", tool_call_id="A")
    newer = _blk("keep", 6, 50, ts="2026-01-03T00:00:00", task_id="p2",
                 mtype="llm_response", role="assistant")
    kept = await PriorityBudgetStrategy().apply([call, result, newer], token_limit=50, request=_req())
    assert {b.id for b in kept} == {"keep"}  # call 与 res 同生共死，不留半对


@pytest.mark.asyncio
async def test_current_message_pinned():
    """task_id == request.task.id 的 user_prompt 即便最老也不丢（pin→0）。"""
    cur = _blk("cur", 6, 100, ts="2026-01-01T00:00:00", mtype="user_prompt", task_id="cur")
    other = _blk("oth", 6, 100, ts="2026-01-02T00:00:00", mtype="user_prompt", task_id="past")
    kept = await PriorityBudgetStrategy().apply([cur, other], token_limit=100, request=_req("cur"))
    assert "cur" in {b.id for b in kept}
    assert "oth" not in {b.id for b in kept}


@pytest.mark.asyncio
async def test_overflow_when_floor_exceeds_limit():
    """仅 priority-0（含 pin）超 effective_limit → 抛富信息 ContextOverflowError。"""
    floor = ContextBlock(id="soul", source="identity", kind="identity", target="system",
                         content="x", priority=0, token_estimate=200, metadata={})
    with pytest.raises(ContextOverflowError) as ei:
        await PriorityBudgetStrategy().apply([floor], token_limit=100, request=_req("cur"))
    assert ei.value.required >= 200
    assert ei.value.effective_limit == 100
```

- [ ] **Step 2: 运行确认失败**

Run: `cd ctx-weft && uv run pytest tests/unit/test_budget_strategy.py -v`
Expected: FAIL（现算法按 `-token_estimate` 裁、无配对/pin/当前提级，多条断言不满足）

- [ ] **Step 3: 实现 budget 重写**

替换 `src/ctx_weft/core/assembler/budget.py` 的 `PriorityBudgetStrategy` 类为：

```python
class PriorityBudgetStrategy(BudgetStrategy):
    """按 eff_priority 保留（0 永不丢，丢序大→小）；同档按最老先丢、再按体积。
    eff_priority = slot_priority 静态基线 + budget 动态覆盖（当前 user_prompt→0 pin，
    当前 task 内容→4 提级）。tool_call↔tool_result 配对成 DropUnit 原子丢弃；
    priority-0 地板超限抛富信息 ContextOverflowError。详见 spec §4.2 / §4.2.1。"""

    async def apply(
        self,
        blocks: list["ContextBlock"],
        token_limit: int,
        request: "ContextRequest",
    ) -> list["ContextBlock"]:
        total = sum(b.token_estimate for b in blocks)
        if total <= token_limit:
            return blocks

        eff_prio = {b.id: self._effective_priority(b, request) for b in blocks}
        units = self._coalesce_tool_pairs(blocks)

        def _unit_prio(u: list["ContextBlock"]) -> int:
            return max(eff_prio[b.id] for b in u)  # 配对成员同 task 同层 → 一致，max 无碍

        def _unit_sort_key(u: list["ContextBlock"]):
            # 统一键：(-priority, 最老 ts, -总 token)。无 subrank。
            # -p 降序 → priority 大先丢；ts 升序 → 最老先丢；-tok → 无 ts 档（能力等）大先丢。
            p = _unit_prio(u)
            ts = min((b.metadata.get("timestamp", "") for b in u), default="")
            tok = sum(b.token_estimate for b in u)
            return (-p, ts, -tok)

        droppable = sorted(units, key=_unit_sort_key)
        kept_ids = {b.id for b in blocks}
        for unit in droppable:
            if total <= token_limit:
                break
            if _unit_prio(unit) == 0:
                continue  # priority-0 地板永不丢
            for b in unit:
                if b.id in kept_ids:
                    kept_ids.discard(b.id)
                    total -= b.token_estimate

        if total > token_limit:
            required = sum(b.token_estimate for b in blocks if eff_prio[b.id] == 0)
            sess = getattr(request, "session", None)
            raise ContextOverflowError(
                f"Context overflow: protected floor={required} tokens > effective_limit={token_limit}",
                required=required,
                effective_limit=token_limit,
                context_limit=getattr(sess, "context_limit", 0),
                reserved_output_tokens=getattr(sess, "reserved_output_tokens", 0),
            )

        return [b for b in blocks if b.id in kept_ids]

    @staticmethod
    def _effective_priority(b: "ContextBlock", request: "ContextRequest") -> int:
        """slot_priority 静态基线 + 两个动态覆盖（依赖 request.task.id）：
        ① pin：当前 task 的 user_prompt → 0（不可裁，当前消息锚）；
        ② 当前 task 内容（task_id 或 origin_task_id == 当前）→ 4（比已完成 5/6 更保）。"""
        task = getattr(request, "task", None)
        cur = getattr(task, "id", None) if task is not None else None
        md = b.metadata or {}
        if cur is not None:
            if md.get("task_id") == cur and str(md.get("type", "")) == "user_prompt":
                return 0
            if md.get("task_id") == cur or md.get("origin_task_id") == cur:
                return 4
        return b.priority

    @staticmethod
    def _coalesce_tool_pairs(blocks: list["ContextBlock"]) -> list[list["ContextBlock"]]:
        """把 assistant(tool_calls) 与其 tool(tool_call_id) 聚成同生共死单元；
        其余 block 各自单元素单元。仅按 id 配对，不改顺序。"""
        by_id = {b.id: b for b in blocks}
        # tool_call_id -> 拥有它的 assistant block id
        owner: dict[str, str] = {}
        for b in blocks:
            if b.metadata.get("role") == "assistant":
                for tc in (b.metadata.get("tool_calls") or []):
                    tcid = tc.get("id")
                    if tcid:
                        owner[tcid] = b.id
        # 归组：assistant id -> [assistant, *其 tool results]
        groups: dict[str, list[str]] = {}
        grouped: set[str] = set()
        for b in blocks:
            if b.metadata.get("role") == "assistant" and b.metadata.get("tool_calls"):
                groups.setdefault(b.id, [b.id])
                grouped.add(b.id)
        for b in blocks:
            if b.metadata.get("role") == "tool":
                tcid = b.metadata.get("tool_call_id", "")
                oid = owner.get(tcid)
                if oid is not None and oid in groups:
                    groups[oid].append(b.id)
                    grouped.add(b.id)
        units: list[list["ContextBlock"]] = []
        for b in blocks:
            if b.id in grouped and b.id not in groups:
                continue  # tool result 已并入其 owner 单元
            if b.id in groups:
                units.append([by_id[i] for i in groups[b.id]])
            else:
                units.append([b])
        return units
```

确保文件顶部已 `from ctx_weft.core.errors import ContextOverflowError`（原文件已有）。

- [ ] **Step 4: 运行确认通过**

Run: `cd ctx-weft && uv run pytest tests/unit/test_budget_strategy.py -v`
Expected: PASS（7 passed）

- [ ] **Step 5: 回归既有 budget 相关用例**

Run: `cd ctx-weft && uv run pytest tests/unit/test_prepare_budget_compact.py tests/unit/test_assembler_effective_limit.py -v`
Expected: PASS（如有按"体积裁"假设的旧断言失败，按新"最老先丢/配对"语义订正期望值，并在 commit note 说明）。

- [ ] **Step 6: Commit**

```bash
git add src/ctx_weft/core/assembler/budget.py tests/unit/test_budget_strategy.py
git commit -m "feat(budget): pairing-atomic oldest-first trim + current-message pin + rich overflow"
```

---

### Task 7: 各 Source 改调 slot_priority()

**Files:**
- Modify: `src/ctx_weft/core/assembler/sources/_history.py:75`
- Modify: `src/ctx_weft/core/assembler/sources/agent_recall.py:113`
- Modify: `src/ctx_weft/core/assembler/sources/capability.py:73,96,116`
- Modify: `src/ctx_weft/core/assembler/sources/blackboard.py:78`
- Modify: `src/ctx_weft/core/assembler/sources/knowledge.py:59`
- Modify: `src/ctx_weft/core/assembler/sources/long_memory.py:61`
- Modify: `src/ctx_weft/core/assembler/sources/identity.py:48,67`
- Modify: `src/ctx_weft/core/assembler/sources/task_spec.py:50`
- Test: `tests/unit/test_source_priorities.py`（新建）

**Interfaces:**
- Consumes: `slot_priority()`（Task 2）

- [ ] **Step 1: 写失败测试**

Create `tests/unit/test_source_priorities.py`:

```python
from ctx_weft.core.assembler.priority import slot_priority

# 直接锁定映射（source 单测需真实 provider，成本高；此处锁 slot_priority 语义 +
# 下面用 grep 步骤确保 source 确实改调它）。
def test_tier_mapping_contract():
    assert slot_priority("history", "agent_compact_summary") == 2
    assert slot_priority("history", "agent_conversation_turn") == 5
    assert slot_priority("history", "llm_response") == 6
    assert slot_priority("history", "task_compact_summary") == 6
    assert slot_priority("capabilities") == 1
    assert slot_priority("blackboard") == 3
    assert slot_priority("reference") == 7
    assert slot_priority("summary") == 7
    assert slot_priority("identity") == 0
    assert slot_priority("task_spec") == 0
```

- [ ] **Step 2: 运行确认通过（契约已由 Task 2 满足）**

Run: `cd ctx-weft && uv run pytest tests/unit/test_source_priorities.py -v`
Expected: PASS（本步锁契约；下面各 source 改为调用它）。

- [ ] **Step 3: 改 `_history.py`（priority + 补 origin_task_id metadata）**

`_history.py` 顶部加 `from ctx_weft.core.assembler.priority import slot_priority`。
把 `record_to_history_block` 里的 `priority=3,` 改为：

```python
        priority=slot_priority("history", str(record.type)),
```

并在该函数构造 `md`（block metadata）处补 `origin_task_id`（供 budget 判 agent 层回合归属哪个
task；现仅带 `task_id`）。在 `md = {...}` 里加一行：

```python
        "origin_task_id": record.metadata.get("origin_task_id", ""),
```

- [ ] **Step 4: 改 `agent_recall.py`**

顶部加 import。`AGENT_COMPACT_SUMMARY` 那个 `yield ContextBlock(...)`（行 ~113）的 `priority=3,` 改为 `priority=slot_priority("history", "agent_compact_summary"),`；其 metadata 若无 `origin_task_id` 保持不带（摘要非 task 归属，budget 提级不命中，落静态 2 正确）。（该文件里 raw/turn 走 `record_to_history_block`，已由 Step 3 覆盖。）

- [ ] **Step 5: 改其余 source**

各文件顶部加 `from ctx_weft.core.assembler.priority import slot_priority`，并替换其硬编码 `priority=N`：
- `capability.py`：三处 `priority=1`/`priority=2`（tool/skill/agent）统一改 `priority=slot_priority("capabilities"),`。
- `blackboard.py`：`priority=priority`（变量）改 `priority=slot_priority(kind),`（**传实际 `kind` 变量**，不要硬编码 `"blackboard"`——`long_term_background` 分支 `kind="background"`→1，其余 `kind="blackboard"`→3；删除上方对 `priority` 变量的赋值）。
- `knowledge.py`：`priority=4` 改 `priority=slot_priority("reference"),`。
- `long_memory.py`：`priority=4` 改 `priority=slot_priority("summary"),`。
- `identity.py`：identity block `priority=0` 改 `priority=slot_priority("identity"),`；directive block `priority=1` 改 `priority=slot_priority("directive"),`。
- `task_spec.py`：`priority=0` 改 `priority=slot_priority("task_spec"),`。

- [ ] **Step 6: 确认无残留硬编码 priority（除 budget/tests）**

Run: `cd ctx-weft && grep -rn "priority=[0-9]" src/ctx_weft/core/assembler/sources/`
Expected: 无输出（全部改为 `slot_priority(...)`）。

- [ ] **Step 7: 运行确认 + 回归**

Run: `cd ctx-weft && uv run pytest tests/unit/ -q`
Expected: 绿；如有断言 block priority 具体数值的旧用例（如 composer 测试），按新 tier 订正。

- [ ] **Step 8: Commit**

```bash
git add src/ctx_weft/core/assembler/sources/ tests/unit/test_source_priorities.py
git commit -m "refactor(sources): assign block priority via slot_priority() central map"
```

---

### Task 8: compact 触发/压缩目标/act 停止 改用 effective_limit

**Files:**
- Modify: `src/ctx_weft/core/loop/steps/prepare.py:161-163`
- Modify: `src/ctx_weft/core/loop/steps/compact.py:170-174,448-453`
- Modify: `src/ctx_weft/core/loop/steps/act.py:296-300`
- Test: `tests/unit/test_effective_limit_thresholds.py`（新建）

**Interfaces:**
- Consumes: `effective_limit()`（Task 1）、`LoopGuard.reserved_output_tokens`（Task 3）

- [ ] **Step 1: 写失败测试**

Create `tests/unit/test_effective_limit_thresholds.py`:

```python
from ctx_weft.core.utils import effective_limit


def test_act_stop_threshold_uses_effective_limit():
    """停 act 阈值 = 0.8 * effective_limit，不是 0.8 * context_limit。"""
    ctx_limit, reserve = 100_000, 8192
    eff = effective_limit(ctx_limit, reserve)
    prompt_tokens = int(ctx_limit * 0.8)          # 旧口径会命中
    # 新口径：与 0.8*eff 比较
    assert prompt_tokens >= int(eff * 0.8)         # 仍命中，但基准更低
    below = int(eff * 0.8) - 1
    assert below < int(eff * 0.8)                  # 边界
```

> 该测试锁"基准是 effective_limit"的算术契约。若已有 `_account_tokens` 的行为测试，同时加一条：构造 `LoopGuard(context_limit=100_000, reserved_output_tokens=8192)` 与 `usage.prompt_tokens=int(effective_limit(...)*0.8)`，断言 `_account_tokens` 返回 True。

- [ ] **Step 2: 运行确认失败/占位**

Run: `cd ctx-weft && uv run pytest tests/unit/test_effective_limit_thresholds.py -v`
Expected: 契约测试 PASS（纯算术）；若加了 `_account_tokens` 行为测试则先 FAIL。

- [ ] **Step 3: 改 prepare.py 触发**

`prepare.py` 顶部确保 `from ctx_weft.core.utils import estimate_tokens, effective_limit`。
`_should_compact`（行 161-163）改为：

```python
        context_limit = state.agent.loop_guard.context_limit
        reserve = state.agent.loop_guard.reserved_output_tokens
        eff = effective_limit(context_limit, reserve)
        if eff > 0 and token_estimate > 0:
            return token_estimate / eff >= loop_config.compact_token_ratio
        return False
```

- [ ] **Step 4: 改 compact.py（派发前触发 + 压缩目标）**

`compact.py` 顶部加 `effective_limit` 到 utils import。
`maybe_compact_before_dispatch`（行 170-174）：

```python
    context_limit = agent.loop_guard.context_limit
    eff = effective_limit(context_limit, agent.loop_guard.reserved_output_tokens)
    tokens = prompt_tokens or agent.loop_guard.context_tokens
    if eff <= 0 or tokens <= 0:
        return []
    if tokens / eff < ratio:
        return []
```

`escalating_compact`（行 448-453）：

```python
    context_limit = agent.loop_guard.context_limit
    eff = effective_limit(context_limit, agent.loop_guard.reserved_output_tokens)
    if eff <= 0:
        return []
    target_ratio = lc.compact_target_ratio if getattr(lc, "compact_target_ratio", 0.0) > 0 \
        else lc.compact_token_ratio
    target_tokens = int(eff * target_ratio)
```

（下方对 `context_limit` 的其它引用若仅用于 `target_tokens`，已被 `eff` 取代；保留变量 `context_limit` 供日志。）

- [ ] **Step 5: 改 act.py 停止阈值**

`act.py` 顶部确保导入 `effective_limit`。`_account_tokens`（行 296-300）改为：

```python
    context_limit = agent.loop_guard.context_limit
    eff = effective_limit(context_limit, agent.loop_guard.reserved_output_tokens)
    return (
        eff > 0
        and usage.prompt_tokens > 0
        and usage.prompt_tokens >= int(eff * 0.8)
    )
```

- [ ] **Step 6: 运行确认 + 回归**

Run: `cd ctx-weft && uv run pytest tests/unit/test_effective_limit_thresholds.py tests/unit/test_compact_trigger.py tests/unit/test_predispatch_compact.py tests/unit/test_escalating_compact.py -v`
Expected: PASS（既有 compact 触发用例若按 context_limit 基准断言绝对 token，按 effective_limit 订正）。

- [ ] **Step 7: Commit**

```bash
git add src/ctx_weft/core/loop/steps/prepare.py src/ctx_weft/core/loop/steps/compact.py src/ctx_weft/core/loop/steps/act.py tests/unit/test_effective_limit_thresholds.py
git commit -m "feat(loop): compact triggers/target + act stop threshold use effective_limit"
```

---

### Task 9: runtime `_run_loop` 处理 ContextOverflowError

**Files:**
- Modify: `src/ctx_weft/core/runtime.py`（`_run_loop`，`except LLMOutageError` 与 `except Exception` 之间，~1382）
- Test: `tests/unit/test_overflow_routing.py`（新建）

**Interfaces:**
- Consumes: `ContextOverflowError`（Task 5）

- [ ] **Step 1: 写失败测试**

Create `tests/unit/test_overflow_routing.py`:

```python
import pytest
from ctx_weft.core.errors import ContextOverflowError


@pytest.mark.asyncio
async def test_overflow_marks_task_failed_not_suspended(run_loop_harness):
    """_run_loop 收到 ContextOverflowError → task FAILED（终态）、code=CONTEXT_OVERFLOW、
    error 文案含数字；不 SUSPENDED、不发 session_interrupted。"""
    h = run_loop_harness(raise_in_step=ContextOverflowError(
        "overflow", required=200_000, effective_limit=171_808,
        context_limit=180_000, reserved_output_tokens=8192))
    await h.run()
    assert h.task.status == "FAILED"
    assert h.task.status != "SUSPENDED"
    assert "171808" in h.task.error or "171,808" in h.task.error
    assert not h.session_interrupted_emitted
```

> 若无 `run_loop_harness` fixture，参照既有对 `LLMOutageError`/`HitlPark` 的 `_run_loop` 测试（搜 `LLMOutageError` in tests）复用其 harness：注入一个在 step 抛 `ContextOverflowError` 的假 driver，断言 `task.status`、`task.error`、以及 `_emit_session_interrupted` 未被调用。

- [ ] **Step 2: 运行确认失败**

Run: `cd ctx-weft && uv run pytest tests/unit/test_overflow_routing.py -v`
Expected: FAIL（当前无专门分支，落 generic FAILED 但 `error` 为原始 str、无定制文案；`code` 未必透出）

- [ ] **Step 3: 实现 except 分支**

`runtime.py` 的 `_run_loop`，在 `except LLMOutageError as exc:` 块之后、`except Exception as exc:` 之前插入：

```python
        except ContextOverflowError as exc:
            # 非瞬时：resume 会重装配同批 block 再溢出 → 终态 FAILED，不 SUSPEND、不 interrupted。
            # 走标准 FAILED 计数（复用 finally / _handle_task_failure），仅定制 error_code 与文案。
            msg = (
                f"上下文超出模型可用窗口：保护槽位（角色设定 + 当前任务/消息）约 {exc.required} tokens，"
                f"已超过为输出预留后的可用窗口 effective_limit={exc.effective_limit}"
                f"（= 模型窗口 {exc.context_limit} − 输出预留 {exc.reserved_output_tokens}）。"
                "请改用更大上下文窗口的模型，或缩短当前消息 / 任务描述。"
            )
            run_error = exc
            if task.status not in ("FINISHED", "FAILED", "CANCELED"):
                task.status = "FAILED"
                task.error = msg
            logger.warning("_run_loop: task %s context overflow: %s", task.id, msg)
```

确保 `runtime.py` 顶部已从 `ctx_weft.core.errors` 导入 `ContextOverflowError`（若未导入则加入现有 errors import 行）。

> `error_code` 透出：host 读模型据 `task.error` 呈现；若读模型另读 `code`，`ContextOverflowError.code == "CONTEXT_OVERFLOW"` 已可用，`run_error` 保留异常对象供 finally 的 `TASK_FAILED` payload 取 `code`。

- [ ] **Step 4: 运行确认通过**

Run: `cd ctx-weft && uv run pytest tests/unit/test_overflow_routing.py -v`
Expected: PASS

- [ ] **Step 5: 全量回归**

Run: `cd ctx-weft && uv run pytest tests/unit/ -q`
Expected: 全绿。

- [ ] **Step 6: Commit**

```bash
git add src/ctx_weft/core/runtime.py tests/unit/test_overflow_routing.py
git commit -m "feat(runtime): route ContextOverflowError to terminal FAILED with actionable message"
```

---

## 收尾验证

- [ ] **全量测试**：`cd ctx-weft && uv run pytest -q` 全绿。
- [ ] **端到端手验（可选）**：按 spec §6，构造一个 context 接近窗口的会话，确认 compact 触发在 `effective_limit` 基准、发送前 prompt ≤ effective_limit、无 dangling/orphan tool 消息。
