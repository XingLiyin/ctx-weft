# HITL 重设计 · 段 2（原子替换）实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让段 1 建好的 `core/hitl` 子系统**接管全部 HITL 流量**，删除旧 `HitlManager`，且不留任何开关。

**Architecture:** 新增 `NeedsHuman` 授权结局与 `needs_human` 能力事件两个接缝；`HitlWaiter`（loop 层）持有热等待与驱逐；`CapabilityGateway` 成为唯一登记/等待/抛 park 的地方；`act` 的两处冷 park 改走 `HitlService.open`；Runtime 构造期接线（零 setter），应答入口据 `resolve` 返回值分流续跑；恢复期用 `fold_hitl_snapshot` 装填 registry。

**Tech Stack:** Python 3.11+、asyncio、dataclasses、pytest（`asyncio_mode = "auto"`）、ruff（line-length 100）。不引入新依赖。

**Spec:** `docs/superpowers/specs/2026-09-01-hitl-redesign-design.md`

**前置：** 段 1 已完成（`docs/superpowers/plans/2026-09-01-hitl-redesign-phase1.md`，提交 `98edebd..e4b4ede`）。`core/hitl/` 已有 `HitlRegistry` / `HitlService` / `ReplyIntake` / `HitlSnapshot`，`reducers.fold_hitl_snapshot` 可双读折叠，全部无消费方。

## Global Constraints

- **本段是原子替换，不留开关。** 不得引入「新旧两套 HITL 并存」的运行期分支、环境变量或配置项。理由见 spec §12.3.1：并存期的不变式只在过渡期存在、测了也白测，而它们出错的方式是静默丢用户回复。
- **`core/hitl/` 仍不得 import `core.loop` 或 `core.runtime`**，包括函数体内延迟 import。段 1 立住的这条不变式在本段只会被更多地方考验。
- **park 只有一个抛出点**：`CapabilityGateway`（`act` 的显式冷 park 除外，它本就在 loop 层且不经 gateway）。`core/hitl` 全程不认识 `HitlPark`。
- **安全不变式**：`Deny` 时 `provider.invoke` 绝不被调用。这条在本段被重写的代码路径上必须逐条保住。
- **`needs_human` 事件是流的终点**：gateway 收到即停止消费该 provider 流，其后 yield 一律不可见。
- **控制流不看 outcome**：core 只判「非空即终局」；outcome 的语义解释归发起方；core 对 outcome 的分支只允许出现在呈现层（注入文案的前缀）。
- ruff line-length = 100；`from __future__ import annotations` 置顶。
- **既有测试的验收口径是「不新增失败」**。本仓在段 1 起点已有 3 条既有失败：`tests/unit/test_golden_conformance.py::test_golden_dir_present`、`tests/unit/test_observe_outcomes.py::test_default_role_prompt_uses_two_fields`、`tests/integration/test_compact_flow_e2e.py::test_multiround_retry_accumulates_then_l3_collapses_e2e`（末条偶发）。

## 风险声明（执行者必读）

本段动 `restore`（`core/orchestrator/task_manager.py`）与 `recover_session`（`core/runtime.py`）——**所有会话恢复都走这两条路径**，不只是 HITL。spec/07 §14 把这里标为最高风险面：改错的表现是「人还没答，任务就自己跑起来了」。Task 9、Task 10 的回归范围是**全部恢复测试**，不是 HITL 测试。

---

## 文件结构

| 文件 | 动作 |
|---|---|
| `src/ctx_weft/protocols/capability.py` | 改：`AuthorizationDecision` 加 `needs_human`；`CapabilityEvent.kind` 加 `"needs_human"`；新增 `HumanGatedAuthorizer` / `HumanResumable` 两个可选接口 |
| `src/ctx_weft/core/loop/hitl_waiter.py` | 新建：`FutureWaitSlot` + `HitlWaiter` |
| `src/ctx_weft/core/loop/capability_gateway.py` | 改：授权步消费 `NeedsHuman`；流式步消费 `needs_human` 事件 |
| `src/ctx_weft/providers/authorizer/human.py` | 改：重写为无状态 |
| `src/ctx_weft/core/orchestrator/control_capability.py` | 改：`ask_user` 走 `needs_human` + `reply_as_result`；`ControlCapabilityProvider` 去掉 `hitl_manager` |
| `src/ctx_weft/core/loop/steps/act.py` | 改：两处冷 park 改走 `HitlService.open` |
| `src/ctx_weft/core/loop/driver.py` | 改：`LoopContext.hitl_manager` → `hitl` / `waiter` |
| `src/ctx_weft/core/runtime.py` | 改：构造期接线、应答入口、续跑分流、恢复装填 |
| `src/ctx_weft/core/orchestrator/task_manager.py` | 改：pending HITL 真相源换成 registry |
| `src/ctx_weft/core/orchestrator/hitl_manager.py` | **删除** |
| `src/ctx_weft/protocols/hitl.py` | 改：删除 legacy `HitlRequest` 及其常量 |
| `src/ctx_weft/core/control/reducers.py` | 改：删除 `fold_pending_hitl` / `fold_cold_hitl_decision`（双读折叠取代之） |

---

## Task 1: 两个接缝（protocols）

**Files:**
- Modify: `src/ctx_weft/protocols/capability.py`
- Test: `tests/unit_protocols/test_hitl_seams.py`（新建）

**Interfaces:**
- Consumes: 段 1 的 `HitlAsk` / `HitlDecision`
- Produces: `AuthorizationDecision.needs_human: HitlAsk | None`；`CapabilityEvent.kind` 增加 `"needs_human"`；`HumanGatedAuthorizer`、`HumanResumable`

- [ ] **Step 1: 写失败的测试**

```python
"""HITL 的两个 provider 侧接缝（段 2 · Task 1）。"""

from __future__ import annotations

from ctx_weft.protocols.capability import (
    AuthorizationDecision,
    CapabilityEvent,
    HumanGatedAuthorizer,
    HumanResumable,
)
from ctx_weft.protocols.hitl import HitlAsk, ToolResultDelivery


def _ask() -> HitlAsk:
    return HitlAsk(form="approval", delivery=ToolResultDelivery(tool_call_id="call_1"),
                   prompt="Allow?")


def test_authorization_decision_can_carry_a_needs_human_ask():
    d = AuthorizationDecision(allowed=False, needs_human=_ask())
    assert d.needs_human is not None and d.needs_human.form == "approval"


def test_needs_human_defaults_to_none_so_existing_decisions_are_unchanged():
    d = AuthorizationDecision(allowed=True)
    assert d.needs_human is None


def test_needs_human_implies_not_allowed():
    """安全不变式：带 ask 的决定绝不能同时是放行——gateway 会先看 allowed。"""
    d = AuthorizationDecision(allowed=False, needs_human=_ask())
    assert d.allowed is False


def test_capability_event_accepts_the_needs_human_kind():
    ev = CapabilityEvent(kind="needs_human", payload={"ask": _ask()})
    assert ev.kind == "needs_human" and ev.payload["ask"].prompt == "Allow?"


def test_optional_interfaces_are_structural_not_inherited():
    """加法式：实现者不必继承基类分叉，只需有对应方法。"""

    class Gated:
        async def on_decision(self, cap, ctx, args, tool_call_id, decision):
            return AuthorizationDecision(allowed=True)

    class Resumable:
        async def resume(self, ask_id, decision, resume_state, ctx):
            yield CapabilityEvent(kind="result", payload={"text": "ok"})

    assert isinstance(Gated(), HumanGatedAuthorizer)
    assert isinstance(Resumable(), HumanResumable)


def test_a_plain_authorizer_is_not_human_gated():
    class Plain:
        async def authorize(self, cap, ctx, args=None, *, tool_call_id=""):
            return AuthorizationDecision(allowed=True)

    assert not isinstance(Plain(), HumanGatedAuthorizer)
```

- [ ] **Step 2: 跑测试确认失败**

Run: `uv run pytest tests/unit_protocols/test_hitl_seams.py -v`
Expected: FAIL —— `ImportError: cannot import name 'HumanGatedAuthorizer'`

- [ ] **Step 3: 实现**

在 `src/ctx_weft/protocols/capability.py`：

① `CapabilityEvent` 的 `kind` 增加成员（`kind: Literal[...]` 那行）：

```python
    kind: Literal["progress", "stdout", "stderr", "result", "error", "needs_human"]
    # needs_human：provider 声明「我需要一个人的决定」，payload["ask"] 是 HitlAsk。
    # **必须是流的最后一个事件**——gateway 见之即停止消费本流，其后 yield 的一律不可见
    # （spec §2）。让出时生成器被关闭，局部状态随之消失，故让出前的工作要放进
    # ask.resume_state。
```

② `AuthorizationDecision` 增加字段（保留 `defer`，Task 4 才删）：

```python
    #: 「挂起并问这个问题」。取代只能说「挂起」的 `defer`——后者说不出问什么，
    #: 所以旧实现必须让 authorizer 自己先去登记请求（那正是耦合的源头）。
    #: 非 None 时 `allowed` 必须为 False；gateway 先判 allowed，安全不变式不依赖本字段。
    needs_human: "HitlAsk | None" = None
```

并在文件顶部 `TYPE_CHECKING` 块补 `from ctx_weft.protocols.hitl import HitlAsk, HitlDecision`。

③ 文件末尾追加两个可选能力接口：

```python
@runtime_checkable
class HumanGatedAuthorizer(Protocol):
    """**可选**能力接口：只有会问人的 authorizer 实现它。

    加法式而非分叉式（spec §2.1）：基础 `Authorizer` 的签名一个字不变，不问人的实现
    看不到任何 HITL 概念。分叉基类会连带要求分叉返回类型联合，否则「不需要 HITL 的
    基类」在类型上仍允许返回 NeedsHuman——非法组合只是换了个地方藏。
    """

    async def on_decision(
        self,
        capability: "Capability",
        ctx: "ProviderContext",
        arguments: dict[str, Any] | None,
        tool_call_id: str,
        decision: "HitlDecision",
    ) -> AuthorizationDecision: ...


@runtime_checkable
class HumanResumable(Protocol):
    """**可选**能力接口：只有会问人、且答复不直接作结果的工具 provider 实现它。

    同样是流式（spec §2）。重入是**重新调用**而非恢复挂起的生成器，故 `resume_state`
    承载让出前的全部状态。`reply_as_result=True` 的 ask（如 `ask_user`）不需要它。
    """

    def resume(
        self,
        ask_id: str,
        decision: "HitlDecision",
        resume_state: dict[str, Any] | None,
        ctx: "ProviderContext",
    ) -> "AsyncIterator[CapabilityEvent]": ...
```

顶部 import 补 `Protocol, runtime_checkable`（`typing`）。

- [ ] **Step 4: 跑测试确认通过**

Run: `uv run pytest tests/unit_protocols/test_hitl_seams.py -v`
Expected: PASS（6 passed）

- [ ] **Step 5: 零回归**

Run: `uv run pytest tests -q`
Expected: 仍恰好是那 3 条既有失败

- [ ] **Step 6: 提交**

```bash
git add src/ctx_weft/protocols/capability.py tests/unit_protocols/test_hitl_seams.py
git commit -m "feat(hitl): 两个 provider 侧接缝——NeedsHuman 授权结局 + needs_human 能力事件"
```

---

## Task 2: HitlWaiter（loop 层的热等待）

**Files:**
- Create: `src/ctx_weft/core/loop/hitl_waiter.py`
- Test: `tests/unit/test_hitl_waiter.py`

**Interfaces:**
- Consumes: 段 1 的 `HitlRegistry`（`attach_slot` / `detach_slot`）、`WaitSlot` 协议、`HitlDecision`
- Produces:
  - `FutureWaitSlot`（实现 `deliver(decision) -> bool`）
  - `HitlWaiter(registry, timeout_sec=None)`，方法 `async wait(hitl_id) -> HitlDecision | None`（`None` = 被驱逐）

- [ ] **Step 1: 写失败的测试**

```python
"""HitlWaiter：热等待与超时驱逐（段 2 · Task 2）。

驱逐**不是失败**，是热→冷降级：返回 None，请求保持未决，由 gateway 翻译成 park。
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

import pytest

from ctx_weft.core.hitl.registry import HitlRegistry
from ctx_weft.core.loop.hitl_waiter import FutureWaitSlot, HitlWaiter
from ctx_weft.protocols.hitl import HitlAsk, HitlDecision, ToolResultDelivery

T0 = datetime(2026, 9, 1, tzinfo=UTC)


def _open(reg: HitlRegistry, hitl_id: str = "hit_1") -> None:
    reg.open(HitlAsk(form="approval", delivery=ToolResultDelivery(tool_call_id="call_1")),
             hitl_id=hitl_id, session_id="s1", task_id="t1", tool_call_id="call_1",
             created_at=T0)


async def test_wait_returns_the_decision_when_it_is_delivered():
    reg = HitlRegistry()
    _open(reg)
    waiter = HitlWaiter(reg)
    task = asyncio.create_task(waiter.wait("hit_1"))
    await asyncio.sleep(0)                      # 让 waiter 挂上槽
    res = reg.resolve("hit_1", HitlDecision(outcome="accepted", message="go"), T0)
    assert res is not None
    _req, slot = res
    assert slot is not None and slot.deliver(HitlDecision(outcome="accepted", message="go"))
    assert (await task).message == "go"


async def test_wait_returns_none_when_the_slot_is_evicted_by_timeout():
    reg = HitlRegistry()
    _open(reg)
    assert await HitlWaiter(reg, timeout_sec=0).wait("hit_1") is None


async def test_eviction_leaves_the_request_pending():
    """驱逐只释放内存，不改持久状态——请求仍未决，答案晚到照常走冷路径。"""
    reg = HitlRegistry()
    _open(reg)
    await HitlWaiter(reg, timeout_sec=0).wait("hit_1")
    assert reg.get("hit_1").resolved is False
    assert reg.get("hit_1").slot is None         # 槽已撤销


async def test_answer_arriving_first_wins_over_a_later_eviction():
    """竞态单一权威：应答先到即热投递，随后的驱逐是 no-op。"""
    reg = HitlRegistry()
    _open(reg)
    waiter = HitlWaiter(reg, timeout_sec=30)
    task = asyncio.create_task(waiter.wait("hit_1"))
    await asyncio.sleep(0)
    _req, slot = reg.resolve("hit_1", HitlDecision(outcome="accepted"), T0)
    assert slot.deliver(HitlDecision(outcome="accepted")) is True
    assert (await task) is not None


async def test_deliver_on_an_evicted_slot_returns_false():
    """槽已被撤销 ⟹ 投递不被接受 ⟹ 调用方据此走冷续跑，不当成已消费。"""
    slot = FutureWaitSlot()
    slot.abandon()
    assert slot.deliver(HitlDecision(outcome="accepted")) is False


async def test_deliver_twice_returns_false_the_second_time():
    slot = FutureWaitSlot()
    assert slot.deliver(HitlDecision(outcome="accepted")) is True
    assert slot.deliver(HitlDecision(outcome="rejected")) is False


async def test_wait_on_unknown_id_raises_keyerror():
    with pytest.raises(KeyError):
        await HitlWaiter(HitlRegistry()).wait("nope")


async def test_none_timeout_waits_indefinitely():
    """默认永不超时——超时是内存旋钮，不是 UX 语义。"""
    reg = HitlRegistry()
    _open(reg)
    waiter = HitlWaiter(reg, timeout_sec=None)
    task = asyncio.create_task(waiter.wait("hit_1"))
    await asyncio.sleep(0.05)
    assert not task.done()
    _req, slot = reg.resolve("hit_1", HitlDecision(outcome="accepted"), T0)
    slot.deliver(HitlDecision(outcome="accepted"))
    assert (await task) is not None
```

- [ ] **Step 2: 跑测试确认失败**

Run: `uv run pytest tests/unit/test_hitl_waiter.py -v`
Expected: FAIL —— `ModuleNotFoundError: ctx_weft.core.loop.hitl_waiter`

- [ ] **Step 3: 实现**

创建 `src/ctx_weft/core/loop/hitl_waiter.py`：

```python
"""HitlWaiter：热等待的持有者。**协程栈的事只发生在这一层。**

`core/hitl` 管账、不认识协程；本模块管栈、不管账。旧实现把两者混在一个类里，
于是必须在函数体内延迟 import `core.loop.park` 来躲循环依赖——那个延迟 import
是边界画错的自白（spec §0/§3）。

驱逐**不是失败**：它是热→冷降级。`wait()` 返回 `None`，请求保持未决，答案晚到
照常走冷路径。把「被驱逐」翻译成 `HitlPark` 是 `CapabilityGateway` 的事，不是本
模块的事——本模块连 park 都不认识。
"""

from __future__ import annotations

import asyncio
import logging

from ctx_weft.core.hitl.registry import HitlRegistry
from ctx_weft.protocols.hitl import HitlDecision

logger = logging.getLogger(__name__)


class FutureWaitSlot:
    """`WaitSlot` 的 asyncio 实现：一个 future 的薄包装。

    刻意只有 `deliver` 一个对外操作（`abandon` 供本模块驱逐时用）——registry 因此
    只接触一个并发原语，不接触任何 loop 类型，依赖箭头保持朝下（spec §3.2）。
    """

    def __init__(self) -> None:
        self._future: asyncio.Future[HitlDecision] = asyncio.get_running_loop().create_future()

    def deliver(self, decision: HitlDecision) -> bool:
        """True = 已被热投递消费（`claimed`）；False = 等待方已放弃/已消费。"""
        if self._future.done():
            return False
        self._future.set_result(decision)
        return True

    def abandon(self) -> None:
        """驱逐：此后 `deliver` 一律返回 False，应答改走冷路径。"""
        if not self._future.done():
            self._future.cancel()

    async def result(self) -> HitlDecision:
        return await self._future


class HitlWaiter:
    """把一个 hitl_id 变成一次可等待的会合。"""

    def __init__(self, registry: HitlRegistry, timeout_sec: int | None = None) -> None:
        #: None = 永不超时（默认）。这是**纯内存/存活旋钮**——热窗口多久后驱逐，
        #: 与「人类该多久回复」无关。
        self._timeout_sec = timeout_sec
        self._registry = registry

    async def wait(self, hitl_id: str) -> HitlDecision | None:
        """阻塞至应答；**被驱逐返回 `None`，不抛**。未知 id 抛 `KeyError`。

        不抛是刻意的：抛一个 core 内部的 `BaseException` 就是旧实现让 provider 层
        被迫 catch 它的那条路。翻译成 park 的权力留在 gateway。
        """
        req = self._registry.get(hitl_id)
        if req is None:
            raise KeyError(f"No HITL request found: {hitl_id}")
        if req.resolved:
            return req.decision                      # 已终局：不建槽，直接给
        slot = FutureWaitSlot()
        self._registry.attach_slot(hitl_id, slot)
        try:
            async with asyncio.timeout(self._timeout_sec):
                return await slot.result()
        except (TimeoutError, asyncio.CancelledError):
            # 驱逐与「应答刚好到达」的竞态由 registry 的同步 resolve 裁决：它已原子地
            # 取走槽，则本处 detach 是 no-op、future 已有结果、上面的 await 早已返回。
            current = self._registry.get(hitl_id)
            if current is not None and current.resolved:
                return current.decision              # 应答先到：走热已解决
            self._registry.detach_slot(hitl_id)
            slot.abandon()
            logger.info("HITL hot window evicted → cold (hitl=%s)", hitl_id)
            return None
```

- [ ] **Step 4: 跑测试确认通过**

Run: `uv run pytest tests/unit/test_hitl_waiter.py -v`
Expected: PASS（8 passed）

- [ ] **Step 5: 分层校验**

Run: `grep -rn "core.loop\|core.runtime" src/ctx_weft/core/hitl/`
Expected: 无输出（本任务不该把 loop 的东西漏进 core/hitl）

- [ ] **Step 6: 提交**

```bash
git add src/ctx_weft/core/loop/hitl_waiter.py tests/unit/test_hitl_waiter.py
git commit -m "feat(hitl): HitlWaiter——热等待与驱逐下沉到 loop 层，core/hitl 不再碰协程栈"
```

---

## Task 3: 无状态 HumanConfirmationAuthorizer

**Files:**
- Modify: `src/ctx_weft/providers/authorizer/human.py`（整体重写）
- Test: `tests/unit/test_authorizer_human_stateless.py`

**Interfaces:**
- Consumes: Task 1 的 `AuthorizationDecision.needs_human` / `HumanGatedAuthorizer`
- Produces: `HumanConfirmationAuthorizer()`——**零构造参数**

- [ ] **Step 1: 写失败的测试**

```python
"""HumanConfirmationAuthorizer 退化为无状态判断（段 2 · Task 3）。"""

from __future__ import annotations

import dataclasses

from ctx_weft.protocols.capability import HumanGatedAuthorizer, ToolCapability
from ctx_weft.protocols.context import ProviderContext
from ctx_weft.protocols.hitl import HitlDecision, ToolResultDelivery
from ctx_weft.providers.authorizer import HumanConfirmationAuthorizer

CAP = ToolCapability(id="fs:bash_exec", name="bash_exec", description="runs shell",
                     input_schema={})
CTX = ProviderContext(session_id="s1", tenant_id="default", task_id="t1", agent_id="a1")


async def test_constructing_it_takes_no_arguments_at_all():
    """host 再也拿不到 HITL 的把手——解耦成为结构性事实。"""
    a = HumanConfirmationAuthorizer()
    assert not any(f.name == "hitl_manager" for f in dataclasses.fields(a))


async def test_first_call_yields_a_needs_human_ask():
    d = await HumanConfirmationAuthorizer().authorize(CAP, CTX, {"command": "ls"},
                                                      tool_call_id="call_1")
    assert d.allowed is False and d.needs_human is not None
    ask = d.needs_human
    assert ask.form == "approval"
    assert ask.delivery == ToolResultDelivery(tool_call_id="call_1")
    assert ask.subject_id == "fs:bash_exec"
    assert ask.proposal == {"command": "ls"}
    assert ask.reply_as_result is False


async def test_it_implements_the_optional_gated_interface():
    assert isinstance(HumanConfirmationAuthorizer(), HumanGatedAuthorizer)


async def test_accepted_decision_allows_and_passes_note_and_modified_args():
    d = await HumanConfirmationAuthorizer().on_decision(
        CAP, CTX, {"command": "ls"}, "call_1",
        HitlDecision(outcome="accepted", message="careful",
                     modified_arguments={"command": "ls -l"}))
    assert d.allowed is True and d.message == "careful"
    assert d.modified_arguments == {"command": "ls -l"}


async def test_rejected_decision_denies_and_passes_the_guidance():
    d = await HumanConfirmationAuthorizer().on_decision(
        CAP, CTX, {}, "call_1", HitlDecision(outcome="rejected", message="先列目录"))
    assert d.allowed is False and d.message == "先列目录"


async def test_unknown_outcome_does_not_allow():
    """安全默认由**发起方**显式写出，不是 core 偷偷替它决定（spec §9.4）。"""
    d = await HumanConfirmationAuthorizer().on_decision(
        CAP, CTX, {}, "call_1", HitlDecision(outcome="escalated", message="转风控"))
    assert d.allowed is False


async def test_it_never_queries_a_decision_cache():
    """决定是喂进来的：authorizer 无状态、无查询、无 I/O，可纯函数式单测。"""
    import inspect

    import ctx_weft.providers.authorizer.human as mod

    src = inspect.getsource(mod)
    assert "find_resolved_for_tool_call" not in src
    assert "hitl_manager" not in src
```

- [ ] **Step 2: 跑测试确认失败**

Run: `uv run pytest tests/unit/test_authorizer_human_stateless.py -v`
Expected: FAIL —— `TypeError: __init__() missing 1 required positional argument: 'hitl_manager'`

- [ ] **Step 3: 重写实现**

`src/ctx_weft/providers/authorizer/human.py` 整体替换为：

```python
"""HITL 审批授权：每次工具调用前请求人工确认。

**无状态**：不持有任何 core 对象、不查任何缓存、不做 I/O。它只做两件纯判断——
第一次进来说「我需要一个人」，被重入时解释人给的决定。等待、登记、幂等、决定缓存
全部归 gateway 与 `core/hitl`（spec §2 / §9.2）。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from ctx_weft.protocols.capability import AuthorizationDecision, Authorizer
from ctx_weft.protocols.hitl import (
    HITL_FORM_APPROVAL,
    HITL_OUTCOME_ACCEPTED,
    HitlAsk,
    HitlDecision,
    ToolResultDelivery,
)

logger = logging.getLogger(__name__)


@dataclass
class HumanConfirmationAuthorizer(Authorizer):
    """每次工具调用前暂停，等待人工确认后再放行。

    host 装配只需 `set_authorizer(pattern, HumanConfirmationAuthorizer())`——**再也拿不到
    HITL 的把手**，这是解耦成为结构性事实而非纪律约定的直接体现。
    """

    async def authorize(self, capability, ctx, arguments=None, *, tool_call_id="",
                        ) -> AuthorizationDecision:
        """总是让出。决定缓存的短路由 gateway 完成——它先查 registry，命中就直接
        走 `on_decision`，根本不会调到这里。"""
        return AuthorizationDecision(allowed=False, needs_human=HitlAsk(
            form=HITL_FORM_APPROVAL,
            delivery=ToolResultDelivery(tool_call_id=tool_call_id),
            prompt=f"Allow tool '{capability.name}'?",
            detail=capability.description,
            proposal=dict(arguments or {}),
            subject_id=capability.id,
        ))

    async def on_decision(self, capability, ctx, arguments, tool_call_id,
                          decision: HitlDecision) -> AuthorizationDecision:
        """解释人给的决定。**未知 outcome 落 else 分支 = 不放行**——放行是安全决定，
        未知值必须落到拒绝侧，而这个默认由本实现显式写出。"""
        if decision.outcome == HITL_OUTCOME_ACCEPTED:
            return AuthorizationDecision(
                allowed=True,
                message=decision.message,
                modified_arguments=decision.modified_arguments,
            )
        logger.info("HITL blocked '%s' (outcome=%s)", capability.id, decision.outcome)
        return AuthorizationDecision(allowed=False, message=decision.message)
```

- [ ] **Step 4: 跑测试确认通过**

Run: `uv run pytest tests/unit/test_authorizer_human_stateless.py -v`
Expected: PASS（7 passed）

- [ ] **Step 5: 记下已知的连带失败**

此时 `tests/unit/test_hitl.py` 等旧端到端测试会失败（它们仍按旧契约构造 authorizer）。
**这是预期的**——它们在 Task 10 随旧实现一并重写/删除。在报告里列出失败清单，不要试图修它们。

- [ ] **Step 6: 提交**

```bash
git add src/ctx_weft/providers/authorizer/human.py tests/unit/test_authorizer_human_stateless.py
git commit -m "feat(hitl): HumanConfirmationAuthorizer 退化为无状态判断，零构造依赖"
```

---

## Task 4: gateway 的授权路径

**Files:**
- Modify: `src/ctx_weft/core/loop/capability_gateway.py`（授权步）
- Modify: `src/ctx_weft/core/loop/driver.py`（`LoopContext` 字段）
- Test: `tests/unit/test_gateway_authz_hitl.py`

**Interfaces:**
- Consumes: Task 1 接缝、Task 2 `HitlWaiter`、段 1 `HitlService` / `HitlRegistry`
- Produces: gateway 私有方法 `_resolve_human(ask, state, ctx, tool_call_id) -> HitlDecision`（被驱逐则抛 `HitlPark`）；`LoopContext.hitl: HitlService | None`、`LoopContext.waiter: HitlWaiter | None`

- [ ] **Step 1: 写失败的测试**

```python
"""gateway 的授权侧 HITL 路径（段 2 · Task 4）。

覆盖四条：热放行、热拒绝、驱逐→park、决定缓存短路（跨重启再入不重问）。
"""

from __future__ import annotations

import asyncio

import pytest

from ctx_weft.core.hitl.registry import HitlRegistry
from ctx_weft.core.hitl.reply_intake import ReplyIntake
from ctx_weft.core.hitl.service import HitlService
from ctx_weft.core.loop.hitl_waiter import HitlWaiter
from ctx_weft.core.loop.park import HitlPark
from ctx_weft.protocols.hitl import HitlDecision, HitlReply
from ctx_weft.providers.authorizer import HumanConfirmationAuthorizer

# `_make_gateway` / `_state` 见 tests/unit/conftest 或本文件底部的现有 helper 惯例；
# 若仓内已有 gateway 测试 helper，复用之，不要新造一套。


async def test_hot_approval_lets_the_call_through_with_modified_arguments():
    gw, reg, svc = _make_gateway(authorizer=HumanConfirmationAuthorizer())
    task = asyncio.create_task(gw.invoke("fs:bash_exec", {"command": "ls"},
                                         _state(), _ctx(), tool_call_id="call_1"))
    await asyncio.sleep(0)
    pending = reg.list_pending()[0]
    await svc.resolve(HitlReply(hitl_id=pending.id, outcome="accepted",
                                modified_arguments={"command": "ls -l"}))
    result = await task
    assert result.is_error is False
    assert _last_invoked_args() == {"command": "ls -l"}      # 改参真正生效


async def test_hot_rejection_never_calls_the_provider():
    """安全不变式：Deny 时 provider.invoke 绝不被调用。"""
    gw, reg, svc = _make_gateway(authorizer=HumanConfirmationAuthorizer())
    task = asyncio.create_task(gw.invoke("fs:bash_exec", {"command": "rm -rf /"},
                                         _state(), _ctx(), tool_call_id="call_1"))
    await asyncio.sleep(0)
    await svc.resolve(HitlReply(hitl_id=reg.list_pending()[0].id, outcome="rejected",
                                message="别删"))
    result = await task
    assert result.is_error is True
    assert "别删" in _text_of(result.content)
    assert _provider_invocations() == 0


async def test_eviction_raises_hitl_park_and_does_not_invoke():
    gw, reg, _svc = _make_gateway(authorizer=HumanConfirmationAuthorizer(), timeout_sec=0)
    with pytest.raises(HitlPark):
        await gw.invoke("fs:bash_exec", {"command": "ls"}, _state(), _ctx(),
                        tool_call_id="call_1")
    assert _provider_invocations() == 0
    assert reg.list_pending()[0].resolved is False      # 请求仍未决


async def test_a_cached_decision_short_circuits_without_asking_again():
    """冷路径重入：registry 已有该 tool_call 的决定 → 直接 on_decision，不新建请求。"""
    gw, reg, _svc = _make_gateway(authorizer=HumanConfirmationAuthorizer())
    reg.load_snapshot(_snapshot_with_decision("call_1",
                                              HitlDecision(outcome="accepted")))
    result = await gw.invoke("fs:bash_exec", {"command": "ls"}, _state(), _ctx(),
                             tool_call_id="call_1")
    assert result.is_error is False
    assert reg.list_pending() == []                    # 没有新建 pending
    assert _provider_invocations() == 1


async def test_a_plain_authorizer_never_touches_the_hitl_path():
    """不问人的 authorizer 走的路径与 HITL 无关，一行 HITL 代码都不执行。"""
    gw, reg, _svc = _make_gateway(authorizer=_AllowAll())
    result = await gw.invoke("fs:bash_exec", {"command": "ls"}, _state(), _ctx(),
                             tool_call_id="call_1")
    assert result.is_error is False and reg.list_pending() == []


async def test_needs_human_without_the_gated_interface_is_a_contract_violation():
    """声明要问人却没实现 on_decision = 契约违例，当场报错，不静默降级。"""
    gw, reg, svc = _make_gateway(authorizer=_AsksButNotGated())
    task = asyncio.create_task(gw.invoke("fs:bash_exec", {}, _state(), _ctx(),
                                         tool_call_id="call_1"))
    await asyncio.sleep(0)
    await svc.resolve(HitlReply(hitl_id=reg.list_pending()[0].id, outcome="accepted"))
    result = await task
    assert result.is_error is True
    assert "does not implement" in _text_of(result.content)
```

> 实现者注意：`_make_gateway` / `_state` / `_ctx` / `_provider_invocations` / `_text_of` /
> `_snapshot_with_decision` 这些 helper——**先在 `tests/unit/` 下找现有的 gateway 测试**
> （`test_interrupt_tools.py`、`test_hitl.py` 等已有构造 gateway 的惯例），复用它们的搭法；
> 只有确实没有时才新写，且写在本测试文件里。不要新造一套与仓内不同的搭法。

- [ ] **Step 2: 跑测试确认失败**

Run: `uv run pytest tests/unit/test_gateway_authz_hitl.py -v`
Expected: FAIL（gateway 尚未消费 `needs_human`；断言 `HitlPark` 的那条会因未抛而失败）

- [ ] **Step 3: 改 `LoopContext`**

`src/ctx_weft/core/loop/driver.py:133` 的 `hitl_manager: "HitlManager|None" = None` 替换为：

```python
    # HITL：管账的 service 与管栈的 waiter 分开持有——旧实现把两者塞进一个对象，
    # 于是编排层被迫认识协程栈（spec §3）。
    hitl: "HitlService | None" = None
    waiter: "HitlWaiter | None" = None
```

并在该文件 `TYPE_CHECKING` 块把 `HitlManager` 的 import 换成：

```python
    from ctx_weft.core.hitl.service import HitlService
    from ctx_weft.core.loop.hitl_waiter import HitlWaiter
```

- [ ] **Step 4: 改 gateway 的授权步**

`capability_gateway.py` 中「2. Authorization」那段（`decision = await authorizer.authorize(...)` 到 `if decision.defer:` 分支）替换为：

```python
        # 2. Authorization：按 cap.id 前缀取 per-provider authorizer，无则用 default
        authorizer = self._get_authorizer(cap.id)
        # 决定缓存短路（冷路径重入）：registry 已有该 tool_call 的人工决定 → 不重问。
        # 内存 pending（活的等待）不算「已答过」，registry.decision_for 已保证这点。
        cached = ctx.hitl.registry.decision_for(tool_call_id) if ctx.hitl else None
        if cached is not None:
            decision, _resume_state = cached
            authz = await self._authz_after_human(
                authorizer, cap, ctx, arguments, tool_call_id, decision)
        else:
            # 交出 ProviderContext（不是 loop 的 LoopContext）——授权契约只认 protocols 类型。
            authz = await authorizer.authorize(
                cap, ctx.provider_ctx, arguments, tool_call_id=tool_call_id,
            )
            if authz.needs_human is not None:
                # 等待权归 gateway：authorizer 只是**声明**需要人，不自己等。
                human = await self._resolve_human(authz.needs_human, state, ctx, tool_call_id)
                authz = await self._authz_after_human(
                    authorizer, cap, ctx, arguments, tool_call_id, human)
        decision = authz
```

并在 gateway 类里新增两个私有方法：

```python
    async def _resolve_human(
        self, ask: "HitlAsk", state: "LoopState", ctx: LoopContext, tool_call_id: str,
    ) -> "HitlDecision":
        """登记 → 热等 → 拿到决定；被驱逐则抛 `HitlPark`。

        **全仓唯一的登记+等待+抛 park 的地方。** 热路径与冷路径在此收敛：冷路径由
        reconcile 经 `invoke` 再入，命中上面的决定缓存短路，根本走不到这里。
        """
        if ctx.hitl is None or ctx.waiter is None:
            raise RuntimeError("HITL requested but no HitlService/HitlWaiter wired")
        req = await ctx.hitl.open(
            ask,
            session_id=ctx.provider_ctx.session_id,
            task_id=state.task.id,
            agent_id=state.agent.id,
            tool_call_id=tool_call_id,
        )
        human = await ctx.waiter.wait(req.id)
        if human is None:
            # 热窗口被驱逐 → 不放行也不拒绝。守住安全不变式：绝不调 provider.invoke。
            from ctx_weft.core.loop.park import HitlPark
            raise HitlPark(hitl_id=req.id, tool_call_id=tool_call_id)
        return human

    @staticmethod
    async def _authz_after_human(
        authorizer, cap, ctx: LoopContext, arguments, tool_call_id: str,
        human: "HitlDecision",
    ) -> AuthorizationDecision:
        """把决定喂回发起方去解释。未实现可选接口 = 契约违例，当场报错。"""
        from ctx_weft.protocols.capability import HumanGatedAuthorizer
        if not isinstance(authorizer, HumanGatedAuthorizer):
            raise TypeError(
                f"{type(authorizer).__name__} returned NeedsHuman but does not implement "
                f"HumanGatedAuthorizer"
            )
        return await authorizer.on_decision(
            cap, ctx.provider_ctx, arguments, tool_call_id, human)
```

`TypeError` 由 gateway 现有的异常出口转成 `is_error` 结果（文案含 `does not implement`）；
若现有 `invoke` 没有把非 `HitlPark` 异常兜成 `is_error`，在 `_resolve_human` 的调用点用
`try/except TypeError` 转 `_error_and_record`，文案 `f"[Error: {exc}]"`。

**删除** 旧的 `if decision.defer:` 分支（连同它函数体内的 `from ...park import HitlPark`）。

- [ ] **Step 5: 跑测试确认通过**

Run: `uv run pytest tests/unit/test_gateway_authz_hitl.py -v`
Expected: PASS（6 passed）

- [ ] **Step 6: 提交**

```bash
git add src/ctx_weft/core/loop/capability_gateway.py src/ctx_weft/core/loop/driver.py tests/unit/test_gateway_authz_hitl.py
git commit -m "feat(hitl): gateway 接管授权侧等待——登记/热等/park 收敛到唯一一处"
```

---

## Task 5: gateway 的工具流路径

**Files:**
- Modify: `src/ctx_weft/core/loop/capability_gateway.py`（`_stream_tool` 及其调用点）
- Test: `tests/unit/test_gateway_tool_needs_human.py`

**Interfaces:**
- Consumes: Task 4 的 `_resolve_human`
- Produces: `_stream_tool` 额外返回 `needs_human_ask: HitlAsk | None`

- [ ] **Step 1: 写失败的测试**

```python
"""工具 provider 侧的 needs_human 路径（段 2 · Task 5）。"""

from __future__ import annotations

import asyncio

from ctx_weft.protocols.capability import CapabilityEvent
from ctx_weft.protocols.hitl import HitlAsk, HitlReply, ToolResultDelivery


class _AsksThenResumes:
    """实现了 HumanResumable：让出前算好 plan，重入时不重算。"""
    name = "deploy"

    def __init__(self) -> None:
        self.computed = 0
        self.applied_with = None

    async def invoke(self, capability_id, arguments, ctx):
        self.computed += 1
        yield CapabilityEvent(kind="progress", payload={"text": "planning"})
        yield CapabilityEvent(kind="needs_human", payload={"ask": HitlAsk(
            form="question",
            delivery=ToolResultDelivery(tool_call_id=ctx.extra["tool_call_id"]),
            prompt="确认部署？", resume_state={"plan": "deploy-7"})})
        yield CapabilityEvent(kind="result", payload={"text": "SHOULD NOT BE SEEN"})

    async def resume(self, ask_id, decision, resume_state, ctx):
        self.applied_with = (resume_state, decision.outcome)
        yield CapabilityEvent(kind="result", payload={"text": "deployed"})


class _AsksAsResult:
    """reply_as_result：答复即结果，不实现 resume。"""
    name = "askuser"

    async def invoke(self, capability_id, arguments, ctx):
        yield CapabilityEvent(kind="needs_human", payload={"ask": HitlAsk(
            form="question",
            delivery=ToolResultDelivery(tool_call_id=ctx.extra["tool_call_id"]),
            prompt="你的名字？", reply_as_result=True)})


class _AsksButNotResumable:
    name = "broken"

    async def invoke(self, capability_id, arguments, ctx):
        yield CapabilityEvent(kind="needs_human", payload={"ask": HitlAsk(
            form="question",
            delivery=ToolResultDelivery(tool_call_id=ctx.extra["tool_call_id"]),
            prompt="?")})


async def test_needs_human_stops_stream_consumption_immediately():
    """该事件是流的终点——其后的 result 事件必须不可见。"""
    provider = _AsksThenResumes()
    gw, reg, svc = _make_gateway(tool_provider=provider)
    task = asyncio.create_task(gw.invoke("deploy:apply", {}, _state(), _ctx(),
                                         tool_call_id="call_1"))
    await asyncio.sleep(0)
    await svc.resolve(HitlReply(hitl_id=reg.list_pending()[0].id, outcome="accepted"))
    result = await task
    assert "SHOULD NOT BE SEEN" not in _text_of(result.content)
    assert _text_of(result.content).strip().endswith("deployed")


async def test_resume_gets_the_resume_state_and_does_not_recompute():
    provider = _AsksThenResumes()
    gw, reg, svc = _make_gateway(tool_provider=provider)
    task = asyncio.create_task(gw.invoke("deploy:apply", {}, _state(), _ctx(),
                                         tool_call_id="call_1"))
    await asyncio.sleep(0)
    await svc.resolve(HitlReply(hitl_id=reg.list_pending()[0].id, outcome="accepted"))
    await task
    assert provider.computed == 1                                  # 没重算
    assert provider.applied_with == ({"plan": "deploy-7"}, "accepted")


async def test_reply_as_result_short_circuits_without_reentry():
    gw, reg, svc = _make_gateway(tool_provider=_AsksAsResult())
    task = asyncio.create_task(gw.invoke("control:ask_user", {}, _state(), _ctx(),
                                         tool_call_id="call_1"))
    await asyncio.sleep(0)
    await svc.resolve(HitlReply(hitl_id=reg.list_pending()[0].id, outcome="accepted",
                                message="小明"))
    result = await task
    assert "小明" in _text_of(result.content)


async def test_reply_as_result_carries_multimodal_answers_through():
    """人的答复带图时，图必须进最终 content，不能被拍成文本。"""
    from ctx_weft.protocols import ImagePart, TextPart

    gw, reg, svc = _make_gateway(tool_provider=_AsksAsResult())
    task = asyncio.create_task(gw.invoke("control:ask_user", {}, _state(), _ctx(),
                                         tool_call_id="call_1"))
    await asyncio.sleep(0)
    await svc.resolve(HitlReply(
        hitl_id=reg.list_pending()[0].id, outcome="accepted",
        message=[TextPart(text="就这张"),
                 ImagePart(data="abc", media_type="image/png", source_type="base64")]))
    result = await task
    assert any(isinstance(p, ImagePart) for p in result.content)


async def test_needs_human_without_resumable_is_a_contract_violation():
    gw, reg, svc = _make_gateway(tool_provider=_AsksButNotResumable())
    task = asyncio.create_task(gw.invoke("broken:x", {}, _state(), _ctx(),
                                         tool_call_id="call_1"))
    await asyncio.sleep(0)
    await svc.resolve(HitlReply(hitl_id=reg.list_pending()[0].id, outcome="accepted"))
    result = await task
    assert result.is_error is True and "does not implement" in _text_of(result.content)


async def test_eviction_in_the_tool_path_parks_without_a_result():
    import pytest
    from ctx_weft.core.loop.park import HitlPark

    gw, _reg, _svc = _make_gateway(tool_provider=_AsksThenResumes(), timeout_sec=0)
    with pytest.raises(HitlPark):
        await gw.invoke("deploy:apply", {}, _state(), _ctx(), tool_call_id="call_1")
```

- [ ] **Step 2: 跑测试确认失败**

Run: `uv run pytest tests/unit/test_gateway_tool_needs_human.py -v`
Expected: FAIL（`needs_human` 事件目前会落进 `_stream_tool` 的未知 kind 分支）

- [ ] **Step 3: 实现**

① `_stream_tool` 的事件循环里，在处理各 `kind` 的分支中最先加：

```python
            if ev.kind == "needs_human":
                # 流的终点：立刻停止消费，其后 yield 的一律不可见（spec §2）。
                # 生成器在此被关闭，provider 的局部状态随之消失——这正是 ask.resume_state
                # 存在的理由。
                needs_human_ask = ev.payload.get("ask")
                break
```

并让 `_stream_tool` 的返回值多带一项 `needs_human_ask`（默认 `None`）。

② 在 `invoke` 的「6. 执行（流式）」之后、拼 `text` 之前插入：

```python
        if needs_human_ask is not None:
            human = await self._resolve_human(needs_human_ask, state, ctx, tool_call_id)
            if needs_human_ask.reply_as_result:
                # 答复即结果：重入根本不发生（`ask_user` 走这条）。
                result_parts, metadata, is_error = _human_reply_as_result(human)
            else:
                from ctx_weft.protocols.capability import HumanResumable
                if not isinstance(provider, HumanResumable):
                    return await self._error_and_record(
                        state, ctx, tool_name, invocation_id,
                        f"[Error: {type(provider).__name__} yielded needs_human but does "
                        f"not implement HumanResumable]",
                        is_dispatch, is_silent, tool_call_id,
                    )
                result_parts, metadata, is_error = await self._stream_events(
                    provider.resume(needs_human_ask_id, human,
                                    needs_human_ask.resume_state, provider_ctx),
                    state, invocation_id,
                )
```

其中 `needs_human_ask_id` 是 `_resolve_human` 内登记出的 `req.id`——把 `_resolve_human`
改成返回 `(req_id, decision)` 二元组，Task 4 的授权侧调用点相应解包（授权侧丢弃 id）。

`_human_reply_as_result(human)` 是新的模块级 helper：

```python
def _human_reply_as_result(human: "HitlDecision") -> tuple[list[str], dict, bool]:
    """把人的答复直接变成工具结果。多模态部分经 metadata 的 CONTENT_PARTS_KEY 透出，
    与 provider 自己产出的非文本 part 走同一条路——否则带图答复会被拍成文本。"""
    text, parts = split_for_tool_result(human.message)
    metadata: dict = {CONTENT_PARTS_KEY: parts} if parts else {}
    return ([text] if text else []), metadata, False
```

③ 把 `_stream_tool` 里消费一个事件流的循环抽成 `_stream_events(aiter, state, invocation_id)`，
供 `invoke` 与 `resume` 两处复用——**不要复制粘贴那段循环**。

- [ ] **Step 4: 跑测试确认通过**

Run: `uv run pytest tests/unit/test_gateway_tool_needs_human.py -v`
Expected: PASS（6 passed）

- [ ] **Step 5: 提交**

```bash
git add src/ctx_weft/core/loop/capability_gateway.py tests/unit/test_gateway_tool_needs_human.py
git commit -m "feat(hitl): gateway 消费 needs_human 事件——reply_as_result 短路与 resume 重入"
```

---

## Task 6: `ask_user` 走新接缝

**Files:**
- Modify: `src/ctx_weft/core/orchestrator/control_capability.py`
- Test: `tests/unit/test_ask_user_needs_human.py`

**Interfaces:**
- Consumes: Task 1 的 `needs_human` 事件、Task 5 的 gateway 路径
- Produces: `ControlCapabilityProvider()` —— **去掉 `hitl_manager` 构造参数**

- [ ] **Step 1: 写失败的测试**

```python
"""ask_user 改走 needs_human + reply_as_result（段 2 · Task 6）。"""

from __future__ import annotations

import dataclasses

from ctx_weft.core.orchestrator.control_capability import ControlCapabilityProvider
from ctx_weft.protocols.hitl import HITL_FORM_QUESTION, ToolResultDelivery


async def test_provider_no_longer_takes_a_hitl_manager():
    ControlCapabilityProvider()                       # 不再需要任何 HITL 依赖
    assert "hitl_manager" not in ControlCapabilityProvider.__init__.__code__.co_varnames


async def test_ask_user_yields_a_needs_human_event_with_reply_as_result():
    provider = ControlCapabilityProvider()
    _register_session(provider)
    events = [ev async for ev in provider.invoke(
        "control:ask_user", {"questions": [{"text": "你的名字？"}]}, _ctx("call_1"))]
    kinds = [ev.kind for ev in events]
    assert kinds[-1] == "needs_human"                  # 且是最后一个
    ask = events[-1].payload["ask"]
    assert ask.form == HITL_FORM_QUESTION
    assert ask.reply_as_result is True                 # 答复即结果，不需要 resume
    assert ask.delivery == ToolResultDelivery(tool_call_id="call_1")
    assert ask.fields == [{"text": "你的名字？"}]


async def test_ask_user_does_not_implement_human_resumable():
    """reply_as_result 的 provider 不必实现重入接口（spec §2.1 的判定表）。"""
    from ctx_weft.protocols.capability import HumanResumable

    assert not isinstance(ControlCapabilityProvider(), HumanResumable)


async def test_ask_user_no_longer_sets_session_status_itself():
    """会话暂停态由 pending 集合推导，不再由工具函数体直接改（spec §7.1）。"""
    provider = ControlCapabilityProvider()
    session = _register_session(provider)
    before = session.status
    _ = [ev async for ev in provider.invoke(
        "control:ask_user", {"questions": []}, _ctx("call_1"))]
    assert session.status == before
```

- [ ] **Step 2: 跑测试确认失败**

Run: `uv run pytest tests/unit/test_ask_user_needs_human.py -v`
Expected: FAIL —— `TypeError`（构造签名）与 `needs_human` 事件不存在

- [ ] **Step 3: 实现**

① `ControlCapabilityProvider.__init__` 去掉 `hitl_manager` 参数与 `self._hitl_manager` 字段；
删除 `TYPE_CHECKING` 里的 `HitlManager` import。

② `ask_user` 工具函数体：删掉 `ctx.session.status = "PAUSED_HITL"` 这行（暂停态改由 pending 推导），
其余保持——它仍返回带 `HITL_REQUESTED` 元数据的 `ControlResult`。

③ `ControlCapabilityProvider._handle()` 里，原先据 `ControlMetaKey.HITL_REQUESTED` 去调
`hitl_manager` 的那段，替换为产出一个事件：

```python
        if result.metadata.get(ControlMetaKey.HITL_REQUESTED):
            # 「答复即结果」：人给的答复直接作为本次 ask_user 的工具结果回灌，
            # 因此本 provider **不需要**实现 HumanResumable（spec §2.3）。
            yield CapabilityEvent(kind="needs_human", payload={"ask": HitlAsk(
                form=HITL_FORM_QUESTION,
                delivery=ToolResultDelivery(tool_call_id=ctx.extra.get("tool_call_id", "")),
                prompt=result.content,
                fields=list(result.metadata.get("questions") or []),
                subject_id=capability_id,
                reply_as_result=True,
            )})
            return                                     # needs_human 是流的终点
```

④ 删除 `WAIT_FOR_USER_CAPABILITY_ID` 常量——它的角色已由 `Delivery` 取代（Task 7 会移除其
最后一个使用点；若此刻仍被 `act.py` 引用，先留着，Task 7 删）。

- [ ] **Step 4: 跑测试确认通过**

Run: `uv run pytest tests/unit/test_ask_user_needs_human.py -v`
Expected: PASS（4 passed）

- [ ] **Step 5: 提交**

```bash
git add src/ctx_weft/core/orchestrator/control_capability.py tests/unit/test_ask_user_needs_human.py
git commit -m "feat(hitl): ask_user 改走 needs_human + reply_as_result，control provider 去掉 HITL 依赖"
```

---

## Task 7: `act` 的两处冷 park

**Files:**
- Modify: `src/ctx_weft/core/loop/steps/act.py`
- Test: `tests/unit/test_act_park_delivery.py`

**Interfaces:**
- Consumes: 段 1 `HitlService.open`、`UserTurnDelivery`
- Produces: `act` 不再引用 `WAIT_FOR_USER_CAPABILITY_ID`

- [ ] **Step 1: 写失败的测试**

```python
"""act 的纯文本暂停与软打断续接改用 UserTurn delivery（段 2 · Task 7）。"""

from __future__ import annotations

import pytest

from ctx_weft.core.loop.park import HitlPark
from ctx_weft.protocols.hitl import (
    PREFACE_AFTER_INTERRUPT,
    PREFACE_AFTER_INTERRUPT_EDIT,
    PREFACE_NORMAL,
    UserTurnDelivery,
)


async def test_plain_text_pause_opens_a_user_turn_request():
    state, ctx, reg = _act_env(interactive=True)
    with pytest.raises(HitlPark):
        await _run_plain_text_pause(state, ctx)
    req = reg.list_pending()[0]
    assert req.delivery == UserTurnDelivery(task_id=state.task.id, preface=PREFACE_NORMAL)
    assert req.tool_call_id == ""                      # 纯文本暂停没有 tool_call


async def test_interrupt_uses_the_after_interrupt_preface():
    state, ctx, reg = _act_env(interactive=True)
    with pytest.raises(HitlPark):
        await _run_interrupt(state, ctx, edit=False)
    assert reg.list_pending()[0].delivery.preface == PREFACE_AFTER_INTERRUPT


async def test_interrupt_edit_uses_its_own_preface():
    state, ctx, reg = _act_env(interactive=True)
    with pytest.raises(HitlPark):
        await _run_interrupt(state, ctx, edit=True)
    assert reg.list_pending()[0].delivery.preface == PREFACE_AFTER_INTERRUPT_EDIT


async def test_park_leaves_no_wait_slot_so_the_reply_goes_cold():
    """冷 park：不建槽，应答必然走冷续跑（等价于旧的 request_parked）。"""
    state, ctx, reg = _act_env(interactive=True)
    with pytest.raises(HitlPark):
        await _run_plain_text_pause(state, ctx)
    assert reg.list_pending()[0].slot is None


async def test_act_no_longer_references_the_wait_for_user_sentinel():
    import inspect

    import ctx_weft.core.loop.steps.act as mod

    assert "WAIT_FOR_USER_CAPABILITY_ID" not in inspect.getsource(mod)
```

- [ ] **Step 2: 跑测试确认失败**

Run: `uv run pytest tests/unit/test_act_park_delivery.py -v`
Expected: FAIL —— act 仍在用 sentinel + `request_parked`

- [ ] **Step 3: 实现**

`_park_wait_for_user` 整体替换为：

```python
async def _park_wait_for_user(
    state: LoopState, ctx: LoopContext, *, source: str, edit: bool = False,
) -> None:
    """起 wait_for_user 冷 park：会话 PAUSED、任务 SUSPENDED，抛 HitlPark。

    续跑方式由 **delivery 显式声明**，不再靠 `form == "wait"` + sentinel capability_id
    这组跨三个模块的魔法字符串（spec §5）。
    """
    preface = (PREFACE_AFTER_INTERRUPT_EDIT if (source == "interrupt" and edit)
               else PREFACE_AFTER_INTERRUPT if source == "interrupt"
               else PREFACE_NORMAL)
    req = await ctx.hitl.open(
        HitlAsk(
            form=HITL_FORM_WAIT,
            delivery=UserTurnDelivery(task_id=state.task.id, preface=preface),
        ),
        session_id=state.session.id,
        task_id=state.task.id,
        agent_id=state.agent.id,
    )
    # 不建等待槽 —— 本调用方随即 park 释放协程而非 await，应答必然走冷续跑。
    state.session.status = "PAUSED"
    state.task.status = "SUSPENDED"
    raise HitlPark(hitl_id=req.id)
```

顶部 import：删 `WAIT_FOR_USER_CAPABILITY_ID`，加

```python
from ctx_weft.protocols.hitl import (
    HITL_FORM_WAIT,
    PREFACE_AFTER_INTERRUPT,
    PREFACE_AFTER_INTERRUPT_EDIT,
    PREFACE_NORMAL,
    HitlAsk,
    UserTurnDelivery,
)
```

其余 `ctx.hitl_manager is not None` 的判据一律改为 `ctx.hitl is not None`。

- [ ] **Step 4: 跑测试确认通过**

Run: `uv run pytest tests/unit/test_act_park_delivery.py -v`
Expected: PASS（5 passed）

- [ ] **Step 5: 提交**

```bash
git add src/ctx_weft/core/loop/steps/act.py tests/unit/test_act_park_delivery.py
git commit -m "feat(hitl): act 的两处冷 park 改用 UserTurn delivery，sentinel capability_id 退场"
```

---

## Task 8: Runtime 构造期接线 + 应答入口

**Files:**
- Modify: `src/ctx_weft/core/runtime.py`
- Test: `tests/unit/test_runtime_hitl_wiring.py`

**Interfaces:**
- Consumes: 段 1 全部；Task 2 `HitlWaiter`
- Produces: `CtxWeftRuntime.hitl: HitlService`、`.hitl_registry: HitlRegistry`；`async reply_to_hitl(reply: HitlReply) -> HitlRequestView | None`

- [ ] **Step 1: 写失败的测试**

```python
"""Runtime 的 HITL 接线与应答入口（段 2 · Task 8）。"""

from __future__ import annotations

import inspect


async def test_no_setter_injection_remains():
    """三个 setter 全部消失——构造完即可用，没有半成品窗口。"""
    import ctx_weft.core.runtime as mod

    src = inspect.getsource(mod)
    for name in ("set_cold_resolve_handler", "set_cold_decision_lookup",
                 "set_content_normalizer"):
        assert name not in src


async def test_hitl_service_is_usable_immediately_after_construction():
    rt = _runtime()
    assert rt.hitl is not None and rt.hitl_registry is not None


async def test_reply_returns_the_view_and_drives_resume_by_delivery():
    """冷续跑由**返回值**驱动，不挂总线订阅（spec §7.3 订正）。"""
    rt, calls = _runtime_with_recorded_resume()
    req = await rt.hitl.open(_ask_tool_result("call_1"), session_id="s1", task_id="t1",
                             tool_call_id="call_1")
    view = await rt.reply_to_hitl(HitlReply(hitl_id=req.id, outcome="accepted"))
    assert view is not None and view.outcome == "accepted"
    assert calls == [("recover_session", "s1", "t1")]


async def test_user_turn_delivery_injects_instead_of_reconciling():
    rt, calls = _runtime_with_recorded_resume()
    req = await rt.hitl.open(_ask_user_turn("t1"), session_id="s1", task_id="t1")
    await rt.reply_to_hitl(HitlReply(hitl_id=req.id, outcome="accepted", message="继续"))
    assert calls[0][0] == "inject_user_turn"


async def test_no_resume_delivery_triggers_nothing():
    rt, calls = _runtime_with_recorded_resume()
    req = await rt.hitl.open(_ask_no_resume(), session_id="s1", task_id="t1")
    await rt.reply_to_hitl(HitlReply(hitl_id=req.id, outcome="cancelled"))
    assert calls == []


async def test_a_claimed_hot_reply_does_not_trigger_cold_resume():
    """热投递已就地续跑，再触发一次冷续跑就是双投。"""
    rt, calls = _runtime_with_recorded_resume()
    req = await rt.hitl.open(_ask_tool_result("call_1"), session_id="s1", task_id="t1",
                             tool_call_id="call_1")
    rt.hitl_registry.attach_slot(req.id, _AcceptingSlot())
    await rt.reply_to_hitl(HitlReply(hitl_id=req.id, outcome="accepted"))
    assert calls == []


async def test_replying_twice_resumes_at_most_once():
    """应答入口可能被重试（host 超时重发 / 用户连点）——第二次是 no-op。"""
    rt, calls = _runtime_with_recorded_resume()
    req = await rt.hitl.open(_ask_tool_result("call_1"), session_id="s1", task_id="t1",
                             tool_call_id="call_1")
    await rt.reply_to_hitl(HitlReply(hitl_id=req.id, outcome="accepted"))
    assert await rt.reply_to_hitl(HitlReply(hitl_id=req.id, outcome="accepted")) is None
    assert len(calls) == 1


async def test_resume_hint_overrides_the_model_for_this_resume_only():
    rt, calls = _runtime_with_recorded_resume()
    req = await rt.hitl.open(_ask_tool_result("call_1"), session_id="s1", task_id="t1",
                             tool_call_id="call_1")
    await rt.reply_to_hitl(HitlReply(hitl_id=req.id, outcome="accepted",
                                     resume_hint=ResumeHint(llm_model="big")))
    assert calls[0][-1] == "big"
```

- [ ] **Step 2: 跑测试确认失败**

Run: `uv run pytest tests/unit/test_runtime_hitl_wiring.py -v`
Expected: FAIL —— `AttributeError: reply_to_hitl`

- [ ] **Step 3: 实现构造期接线**

`runtime.py:472-502` 的 `hitl_manager` 参数与三处 setter 全部替换为：

```python
        # HITL：构造期一次性接线，**没有 setter、没有半成品窗口**。裸构造即生产形态。
        self.hitl_registry = HitlRegistry(max_resolved=self._config.hitl_max_resolved)
        self.hitl = HitlService(
            registry=self.hitl_registry,
            event_bus=self._event_bus,
            reply_intake=ReplyIntake(self._normalize_hitl_content),
        )
        self._hitl_timeout_sec = self._config.hitl_timeout_sec
```

`_normalize_hitl_content` 的签名改为 `async (content, session_id) -> (content, event_payload)`
（去掉 `HitlRequest` 参数，tenant 仍由 `session_id` 解出）。

`_execute_task` 构造 `LoopContext` 处，`hitl_manager=self.hitl_manager` 改为：

```python
            hitl=self.hitl,
            waiter=HitlWaiter(self.hitl_registry, timeout_sec=self._hitl_timeout_sec),
```

- [ ] **Step 4: 实现应答入口**

新增：

```python
    async def reply_to_hitl(self, reply: "HitlReply") -> "HitlRequestView | None":
        """host 应答的唯一入口。返回已终局请求的视图；已终局再答 → `None`。

        **冷续跑由本返回值驱动，不挂总线订阅**（spec §7.3 订正）：该总线的 handler
        订阅者在 `emit()` 内部同步 drain，且背压下丢事件——把控制流关键信号挂上去，
        「人答了但会话永不续跑」就成了可能。
        """
        resolved = await self.hitl.resolve(reply)
        if resolved is None:
            return None                       # 幂等：已终局，不重复续跑
        if resolved.decision is not None and _was_claimed_hot(resolved):
            return resolved.to_view()         # 热投递已就地续跑，不得双投
        await self._resume_after_hitl(resolved, reply.resume_hint)
        return resolved.to_view()

    async def _resume_after_hitl(self, req: "PendingHitl", hint: "ResumeHint") -> None:
        """按 **delivery** 分流续跑——不看 form，不看 capability_id（spec §5）。"""
        if isinstance(req.delivery, ToolResultDelivery):
            await self.recover_session(
                req.session_id, resumed_task_id=req.task_id,
                llm_account=hint.llm_account, llm_model=hint.llm_model,
            )
        elif isinstance(req.delivery, UserTurnDelivery):
            await self.recover_session(
                req.session_id, user_reply=req, resumed_task_id=req.delivery.task_id,
                llm_account=hint.llm_account, llm_model=hint.llm_model,
            )
        # NoResumeDelivery：纯通知 / 取消，无动作。
```

`_was_claimed_hot` 由 `HitlService.resolve` 顺带返回或在 `PendingHitl` 上留一个只读标记；
**实现者选其一并在报告里说明**——推荐让 `resolve` 返回 `(req, claimed)` 二元组，因为 claimed
本就是那一刻的原子结论，事后从状态推不出来。

`_inject_user_reply` 的两处改动：`req.message` 与 `req.outcome` 改从 `req.decision` 取；
注入的 `MemoryEvent` 加 `id=f"hitlreply:{req.id}"`（幂等键，§7.3/§12.2）；
`req.context == "interrupt:edit"` 的判据改为 `req.delivery.preface == PREFACE_AFTER_INTERRUPT_EDIT`。

- [ ] **Step 5: 跑测试确认通过**

Run: `uv run pytest tests/unit/test_runtime_hitl_wiring.py -v`
Expected: PASS（8 passed）

- [ ] **Step 6: 提交**

```bash
git add src/ctx_weft/core/runtime.py tests/unit/test_runtime_hitl_wiring.py
git commit -m "feat(hitl): Runtime 构造期接线（零 setter）+ 应答入口据返回值分流续跑"
```

---

## Task 9: 恢复期装填 + 暂停态推导

> **本任务动 `recover_session` 与 `restore`——所有会话恢复都走这两条路径。** 回归范围是全部
> 恢复测试，不是 HITL 测试。改错的表现是「人还没答，任务就自己跑起来了」。

**Files:**
- Modify: `src/ctx_weft/core/runtime.py`（`recover_session` / `rebuild_hitl` / `_pending_hitl`）
- Modify: `src/ctx_weft/core/orchestrator/task_manager.py`（pending HITL 真相源）
- Test: `tests/unit/test_hitl_recovery_v2.py`

**Interfaces:**
- Consumes: 段 1 `fold_hitl_snapshot` / `load_snapshot`
- Produces: `CtxWeftRuntime.rebuild_hitl(session_id) -> int` 改为装填 registry；
  `HitlRegistry.resolved_for_session(session_id) -> list[PendingHitl]`（新增只读查询，纯内存、同步）

- [ ] **Step 1: 写失败的测试**

```python
"""恢复期装填与暂停态推导（段 2 · Task 9）。"""

from __future__ import annotations


async def test_recovery_fills_the_registry_from_folded_events():
    rt = _runtime_with_events(_legacy_pending_approval_events())
    n = await rt.rebuild_hitl("s1")
    assert n == 1
    assert rt.hitl_registry.list_pending("s1")[0].tool_call_id == "call_1"


async def test_filled_requests_have_no_wait_slot():
    """重启后一切皆冷。"""
    rt = _runtime_with_events(_legacy_pending_approval_events())
    await rt.rebuild_hitl("s1")
    assert rt.hitl_registry.list_pending("s1")[0].slot is None


async def test_event_side_refs_are_hydrated_before_filling():
    """spec §12.3.3：event 侧 ref 直接进 memory 会写一个永远打不开的引用。"""
    rt = _runtime_with_events(_legacy_answered_with_event_blob_ref())
    await rt.rebuild_hitl("s1")
    decision, _ = rt.hitl_registry.decision_for("call_1")
    assert not _is_event_side_ref(decision.message)


async def test_hydration_failure_degrades_to_text_and_never_raises():
    rt = _runtime_with_events(_legacy_answered_with_broken_ref())
    await rt.rebuild_hitl("s1")                        # 不得抛
    decision, _ = rt.hitl_registry.decision_for("call_1")
    assert "[image" in _text_of(decision.message)


async def test_session_status_paused_hitl_for_tool_result_delivery():
    rt = _runtime_with_events(_pending_with_tool_result_delivery())
    assert await rt.session_status_after_recover("s1") == "PAUSED_HITL"


async def test_session_status_paused_for_user_turn_delivery():
    """UserTurn = 软待命，没有面板要答 —— 误标会让前端等一个不存在的面板。"""
    rt = _runtime_with_events(_pending_with_user_turn_delivery())
    assert await rt.session_status_after_recover("s1") == "PAUSED"


async def test_status_is_derived_from_delivery_not_from_form():
    """host 自定义 form 也能拿到正确的暂停态（旧实现按 form == "wait" 字面量判定）。"""
    rt = _runtime_with_events(_pending_with_custom_form_and_user_turn())
    assert await rt.session_status_after_recover("s1") == "PAUSED"


async def test_a_task_with_an_unresolved_hitl_stays_parked_and_is_not_requeued():
    """最高风险的一条：人还没答，任务绝不能自己跑起来。"""
    rt = _runtime_with_events(_pending_with_tool_result_delivery())
    await rt.recover_session("s1")
    assert _task_status(rt, "t1") == "SUSPENDED"
    assert _drained_tasks(rt) == []


async def test_a_task_parked_on_an_already_resolved_hitl_is_requeued():
    """崩溃窗口的兜底：决定已落盘、但进程在续跑之前死了。

    应答入口的返回值驱动（§7.3）挡住的是「丢事件」，挡不住「丢进程」。若恢复时
    不管这种任务，人已经答过的会话就永远停在 SUSPENDED——症状与被丢事件时一模一样。
    """
    rt = _runtime_with_events(_resolved_but_never_resumed_tool_result())
    await rt.recover_session("s1")
    assert "t1" in _drained_tasks(rt)


async def test_requeue_of_a_resolved_hitl_does_not_re_execute_the_tool():
    """重排是安全的，因为续跑动作本身幂等：reconcile 只补没有 TOOL_RESULT 的 dangling
    调用。所以恢复期**不需要**记「这次续跑到底跑没跑过」——那笔账要跨重启，又得多一份
    持久状态（绕回 §3.1 要消除的东西）。"""
    rt = _runtime_with_events(_resolved_and_already_resumed_tool_result())
    await rt.recover_session("s1")
    assert _tool_executions(rt, tool_call_id="call_1") == 1


async def test_requeue_of_a_resolved_user_turn_does_not_duplicate_the_injection():
    """UserTurn 侧的幂等靠 MemoryEvent.id = f"hitlreply:{hitl_id}"（§7.3/§12.2）。"""
    rt = _runtime_with_events(_resolved_and_already_injected_user_turn())
    await rt.recover_session("s1")
    assert _user_prompt_count(rt, task_id="t1") == 1
```

- [ ] **Step 2: 跑测试确认失败**

Run: `uv run pytest tests/unit/test_hitl_recovery_v2.py -v`
Expected: FAIL

- [ ] **Step 3: 实现**

① `_pending_hitl` / `rebuild_hitl` 改为：

```python
    async def rebuild_hitl(self, session_id: str) -> int:
        """从事件装填该 session 的 HITL 内存态，返回 pending 条数。

        **恢复是「喂进来」，不是「查回去」**（spec §3.1）：装填之后 registry 的一切查询
        只读内存，绝不回落去 scan 日志。装填的完备性因此是本路径的责任。
        """
        from ctx_weft.core.control.reducers import HITL_FOLD_EVENT_TYPES, fold_hitl_snapshot

        events = await self._read_session_events_of_types(session_id, HITL_FOLD_EVENT_TYPES)
        snapshot = fold_hitl_snapshot(events)
        await self._hydrate_snapshot_messages(snapshot, session_id)
        return self.hitl_registry.load_snapshot(snapshot)

    async def _hydrate_snapshot_messages(self, snapshot, session_id: str) -> None:
        """把 decisions_for 里的 **event 侧** 内容还原成 memory 侧可用的形态。

        spec §12.3.3：折叠出来的 message 仍是事件形态（可能是 event blob store 的 ref）。
        直接喂进 memory 会写一个那个 store 永远打不开的引用——图就此消失。本方法是
        `fold_hitl_snapshot`（同步、纯函数，结构上做不了 I/O）之后的必经一步。

        **best-effort，绝不抛**：抛错会卡住整条恢复路径。失败降级为文本占位，与
        现行 `_cold_hitl_decision` 同口径。
        """
        for tool_call_id, (decision, resume_state) in list(snapshot.decisions_for.items()):
            try:
                decision.message = await self._hydrate_event_content(
                    decision.message, session_id)
            except Exception:
                logger.warning(
                    "HITL 恢复：event ref 还原失败，降级为文本占位 (tool_call=%s)", tool_call_id)
                decision.message = downgrade_images_to_text(decision.message)
```

② 会话状态推导（原 `runtime.py:1842` 的 `all(r.form == "wait")`）：

```python
                    pend = self.hitl_registry.list_pending(session_id=session_id)
                    # 由 **delivery** 推导，不看 form：UserTurn = 会话在等用户说话（软待命，
                    # 无面板）→ PAUSED；ToolResult = 等一个面板决定 → PAUSED_HITL。
                    # 旧实现按 form == "wait" 字面量判定，host 自定义 form 拿不到正确行为。
                    status = ("PAUSED"
                              if pend and all(isinstance(r.delivery, UserTurnDelivery)
                                              for r in pend)
                              else "PAUSED_HITL")
```

③ `task_manager.set_has_pending_hitl` / `set_cancel_pending_hitl` 的 lambda 改指
`self.hitl_registry.list_pending(...)` 与 `self.hitl.cancel(...)`。

④ **崩溃窗口的兜底：挂在「已终局」HITL 上的任务要重排。**

`restore` 今天区分「SUSPENDED-on-children」与「SUSPENDED-on-HITL」，后者一律保持挂起、不重排
（spec/07 §9.1）。这条规则漏了一种情形：**决定已落盘、但进程在续跑之前就死了**。此时 HITL 已
终局，任务却仍是 SUSPENDED，按现规则会被永远晾着——症状与「事件被丢」一模一样，而这正是本次
重设计要消灭的故障类。

判据从「这个任务有没有 HITL」改成「它挂着的 HITL **终局了没有**」：

```python
        # 有未决 pending HITL 的 task：保持 parked、不重排（人还没答，绝不能自己跑起来）。
        parked_task_ids = {
            r.task_id for r in self.hitl_registry.list_pending(session_id=session_id)
            if r.task_id
        }
        # 反过来：挂在**已终局** HITL 上的 task 要重排——决定已落盘、但续跑没跑成
        # （进程在应答入口返回之后、recover 之前崩了）。不重排它就永远停在 SUSPENDED。
        resumable_task_ids = {
            r.task_id for r in self.hitl_registry.resolved_for_session(session_id)
            if r.task_id and r.task_id not in parked_task_ids
        }
```

`HitlRegistry.resolved_for_session(session_id)` 是本任务给 registry 加的一个只读查询（纯内存、
同步，与 `list_pending` 同形），返回该 session 已终局的请求。

**为什么不需要记「这次续跑到底跑没跑过」**：因为两条续跑路径本身都幂等——`ToolResult` 走
reconcile，它只补没有 `TOOL_RESULT` 的 dangling 调用；`UserTurn` 的注入带 `hitl_id` 派生的
幂等键。所以「宁可重排一次」是安全的，而记这笔账要跨重启，就又需要一份持久状态，绕回 §3.1
想消除的东西。

> 装填的完备性在这里第二次成为承重点：`load_snapshot` 只装填 reconcile 需要的那些已终局决定
> （按 dangling tool_call 有界）。若某个已终局请求没被装进来，`resolved_for_session` 就看不见
> 它，兜底也就失效。Task 9 的实现者要确认这两处的集合口径一致，并在报告里说明。

- [ ] **Step 4: 跑测试确认通过**

Run: `uv run pytest tests/unit/test_hitl_recovery_v2.py -v`
Expected: PASS（8 passed）

- [ ] **Step 5: 全部恢复回归（本任务的真正验收）**

Run: `uv run pytest tests -q -k "recover or restore or crash or resume or reconcile or suspend"`
Expected: 无新增失败。**这一步不通过就不要提交**——它覆盖的是所有会话的恢复，不只是 HITL。

- [ ] **Step 6: 提交**

```bash
git add src/ctx_weft/core/runtime.py src/ctx_weft/core/orchestrator/task_manager.py tests/unit/test_hitl_recovery_v2.py
git commit -m "feat(hitl): 恢复期装填 registry + 暂停态改由 delivery 推导"
```

---

## Task 10: 删除旧实现

**Files:**
- Delete: `src/ctx_weft/core/orchestrator/hitl_manager.py`
- Modify: `src/ctx_weft/protocols/hitl.py`（删 legacy `HitlRequest` 及其专属常量）
- Modify: `src/ctx_weft/core/control/reducers.py`（删 `fold_pending_hitl` / `fold_cold_hitl_decision` / `HITL_STATUS_EVENT_TYPES`）
- Modify: 所有引用点（`runtime.py` 的 re-export、`cli.py`、`main.py`、host 装配点）
- Delete/Rewrite: 按旧契约写的 HITL 测试
- Test: 全量

- [ ] **Step 1: 列出所有引用点**

Run:
```bash
grep -rn "HitlManager\|hitl_manager\|HitlRequest\b\|fold_pending_hitl\|fold_cold_hitl_decision\|WAIT_FOR_USER_CAPABILITY_ID" --include=*.py src tests scripts | grep -v __pycache__
```
把清单贴进报告。**这就是本任务的工作范围**，逐条清零。

- [ ] **Step 2: 删除与改写**

- 删 `core/orchestrator/hitl_manager.py`。
- `protocols/hitl.py` 删 legacy `HitlRequest` 类与 `_now_utc`（若无其它使用者）。保留
  `HitlForm` / `HitlOutcome` / `HITL_FORM_*` / `HITL_OUTCOME_*`——新契约仍在用。
- `reducers.py` 删两个旧折叠函数与 `HITL_STATUS_EVENT_TYPES`（`fold_hitl_snapshot` 已覆盖其职责）。
- `runtime.py` 顶部的 `HitlManager` / `HitlRequest` re-export 换成 `HitlService` / `HitlRequestView` / `HitlReply`。
- host 装配点：`HumanConfirmationAuthorizer(hitl_manager=...)` → `HumanConfirmationAuthorizer()`；
  `ControlCapabilityProvider(hitl_manager=...)` → `ControlCapabilityProvider()`。

- [ ] **Step 3: 处理旧测试**

按旧契约写的 HITL 测试（`test_hitl.py`、`test_hitl_park.py`、`test_hitl_recovery.py` 等）：
**逐个判断**——断言的行为若在新实现里仍成立，改写到新契约上；若断言的是已被设计取代的机制
（如 `request_parked` 双入口、`wait` 的双出口、`form == "wait"` 分流），删除并在报告里逐条
说明删除理由。**不要为了让测试变绿而弱化断言。**

- [ ] **Step 4: 全量回归**

Run: `uv run pytest tests -q`
Expected: 无新增失败（仍是那 3 条既有失败）

- [ ] **Step 5: 不变式校验**

```bash
grep -rn "from ctx_weft.core" --include=*.py src/ctx_weft/providers/ | grep -v __pycache__ | grep -v "core.utils\|core.content"
grep -rn "core.loop\|core.runtime" src/ctx_weft/core/hitl/
grep -rn "form == \"wait\"\|form == 'wait'" --include=*.py src/
```
Expected: 三条全部无输出。第一条是本次重设计的**头号目标**——providers 不再反向依赖 core 编排类。

- [ ] **Step 6: 提交**

```bash
git add -A src tests
git commit -m "refactor(hitl)!: 删除旧 HitlManager 与 legacy HitlRequest，新子系统全面接管"
```

---

## Task 11: 端到端与不变式收口

**Files:**
- Test: `tests/integration/test_hitl_e2e_v2.py`

- [ ] **Step 1: 写端到端测试**

覆盖四条完整链路，每条都从真实 runtime 起，不打桩 core：

1. **热审批**：起会话 → 触发 bash 审批 → `reply_to_hitl(accepted, modified_arguments)` → 工具用改后参数执行 → 结果回灌 LLM。
2. **冷审批**：同上但先驱逐（`hitl_timeout_sec=0`）→ 任务落 SUSPENDED → 应答 → reconcile 精确重入 → 工具**只执行一次**。
3. **ask_user 冷路径**：`ask_user` → 驱逐 → 应答带图 → 图进工具结果 → 续跑。
4. **纯文本暂停**：interactive 任务纯文本回复 → PAUSED → 用户回话 → 作 USER_PROMPT 注入 → 续跑，且**重复应答不产生第二条注入**。

- [ ] **Step 2: 跑通并提交**

Run: `uv run pytest tests/integration/test_hitl_e2e_v2.py -v`

```bash
git add tests/integration/test_hitl_e2e_v2.py
git commit -m "test(hitl): 段 2 端到端——热/冷审批、ask_user 带图、纯文本暂停注入幂等"
```

---

## 段 2 完成判据

- [ ] `uv run pytest tests -q` 无新增失败
- [ ] `grep -rn "from ctx_weft.core" src/ctx_weft/providers/ | grep -v "core.utils\|core.content"` 无输出
- [ ] `grep -rn "core.loop\|core.runtime" src/ctx_weft/core/hitl/` 无输出
- [ ] `grep -rn "HitlManager\|hitl_manager" src tests` 无输出
- [ ] `grep -rn "form == \"wait\"" src/` 无输出
- [ ] 全部恢复相关测试通过（Task 9 Step 5 的口径）
- [ ] 恢复期两个方向都对：挂在**未决** HITL 上的任务保持 parked，挂在**已终局** HITL 上的任务被重排（spec §7.3.1）
