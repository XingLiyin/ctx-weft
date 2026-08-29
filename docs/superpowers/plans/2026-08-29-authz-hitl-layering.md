# 授权与 HITL 契约分层 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把授权与 HITL 的**契约**提到 `protocols/`、**实现**降到 `providers/`，让 core 只保留编排与机制；顺带修掉 `Authorizer.authorize` 传错 ctx 类型的 latent bug，并让 `runtime` 的 `event_bus` 可注入。

**Architecture:** 分五步、五个 commit，每步都保持全绿。① 先收口 `Authorizer.authorize` 签名（去掉 `Agent`/`Task`，改走 `ProviderContext`），这是契约能落到 protocols 的前提；② 把 `HitlForm`/`HitlStatus`/`HitlRequest` 提到 `protocols/hitl.py` 并把 `HitlForm` 从闭 `Literal` 放宽为 `str`；③ 契约本体搬 `protocols/capability.py`；④ 三个内置实现搬 `providers/authorizer/`，删掉 `core/auth/`；⑤ `runtime` 的 `event_bus` 加构造参数、兜底 import 一致化。

**Tech Stack:** Python 3.11+ / dataclasses / abc / pytest + pytest-asyncio（`asyncio_mode=auto`）/ ruff

**Spec:** `docs/superpowers/specs/2026-08-29-authz-hitl-layering-design.md`

**权威行为 spec（本计划会修改它）:** `docs/spec/05-authz-and-hitl.md`

## Global Constraints

- **不留任何 re-export shim。** 对齐既定做法（commit 256d3b9「删除 events / event_store 两个 re-export shim」），所有搬迁一律改调用点，旧路径直接消失。
- **层序方向：`protocols` ← `core` ← `providers` 的反向依赖零容忍。** `protocols/*.py` 不得出现模块级 `from ctx_weft.core...`，`TYPE_CHECKING` 下也不行。唯一既有例外是 `protocols/context.py` 函数体内那处带说明的惰性 import。
- **`protocols` 内不得 import `core.utils.now_utc`**；需要 UTC 时间就在 `protocols/hitl.py` 内写私有 `_now_utc()`。
- **安全不变式不得回归**：`decision.allowed == False` 时 `provider.invoke` 绝不被调用。
- **字段名一律不改。** `HitlRequest` 所有字段名保持原样，搬迁只改 import 路径。
- **每个 Task 结束时全量测试不得引入新失败**：`python -m pytest -q`。
  本仓存在 8 个与本次改动无关的既有失败（Windows 子进程 / 缺 golden 资源 / 一条 prompt 断言），
  基线清单见 ledger Ruling R5。判据是与基线逐条 diff，不是「全绿」。
- **每个 Task 结束时 lint 不得引入新 finding**：`python -m ruff check src tests --statistics`，
  **除 RUF001/002/003 外任何规则的计数不得增加**。本仓基线即 18000+ findings，其中 95% 是
  ambiguous-unicode（注释与 docstring 为中文），这三条规则在此仓是噪声。见 ledger Ruling R4。
- 所有命令的运行目录为仓库根 `ctx-weft/`。

---

### Task 1: `Authorizer.authorize` 签名收口 + 修 ctx 类型错传

**Files:**
- Modify: `src/ctx_weft/protocols/context.py`（`ProviderContext` 增字段）
- Modify: `src/ctx_weft/core/auth/authorizer.py`（ABC + 三个实现的签名）
- Modify: `src/ctx_weft/core/loop/capability_gateway.py:178-181`（传 `ctx.provider_ctx`）
- Modify: `src/ctx_weft/core/runtime.py:1933-1941`（`_build_provider_ctx` 填 `agent_template_id`）
- Modify: `docs/spec/05-authz-and-hitl.md`（§1 签名、§2 步骤 2、§5 清单第 1 条）
- Test: `tests/unit/test_authorizer.py`
- Test: `tests/unit/test_gateway_error_records_result.py:61`
- Test: `tests/unit/test_tool_result_content_parts.py:70`
- Test: `tests/unit/test_hitl_park.py:56`

**Interfaces:**
- Consumes: 无（首个 Task）
- Produces（后续 Task 全部依赖这两个签名）:

```python
# ProviderContext 新增字段（位置：agent_id 之后）
agent_template_id: str = ""

# Authorizer
async def authorize(
    self,
    capability: Capability,
    ctx: ProviderContext,
    arguments: dict[str, Any] | None = None,
    *,
    tool_call_id: str = "",
) -> AuthorizationDecision: ...

async def filter(
    self,
    capabilities: list[Capability],
    ctx: ProviderContext,
    arguments: dict[str, Any] | None = None,
) -> list[Capability]: ...
```

- [ ] **Step 1: 写失败测试 —— gateway 必须把 `ProviderContext` 传给 authorizer**

这是本 Task 的核心回归测试：证明今天传的是 `LoopContext`。追加到 `tests/unit/test_authorizer.py` 末尾。

```python
async def test_gateway_passes_provider_context_not_loop_context() -> None:
    """gateway 必须把 ProviderContext（而非 LoopContext）交给 authorizer。

    回归 latent bug：capability_gateway 曾把 LoopContext 传给形参 ctx: ProviderContext。
    三个内置实现都不读 ctx 所以没爆，但 host 自写 authorizer 读 ctx.session_id 会 AttributeError。
    """
    seen: dict = {}

    class _CtxSpy(Authorizer):
        async def authorize(self, capability, ctx, arguments=None, *, tool_call_id=""):
            seen["ctx"] = ctx
            return AuthorizationDecision(allowed=True)

    provider = _EchoProvider()
    gw, ctx = _gateway(provider, authorizers={"test:echo": _CtxSpy()})
    await gw.invoke("test__echo", {"text": "hi"}, _state(), ctx)

    got = seen["ctx"]
    assert isinstance(got, ProviderContext)
    assert got.session_id == "s1"
    assert got.agent_template_id == "tmpl_a"
```

该文件已有 `_gateway()` 与构造 `LoopState` 的 helper（见 `tests/unit/test_authorizer.py:133` 附近）。
沿用文件内既有名称；若构造 `LoopState` 的 helper 不叫 `_state()`，改用实际名称。
必须确保其 `LoopContext.provider_ctx` 的 `session_id == "s1"` 且 `agent_template_id == "tmpl_a"`
（Step 9 会把 `_ctx()` helper 改成填这两个字段）。

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m pytest tests/unit/test_authorizer.py::test_gateway_passes_provider_context_not_loop_context -v`

预期：FAIL。先是 `TypeError`（`_CtxSpy.authorize` 只收新签名，而 gateway 仍多传 `agent`/`task`），
签名改完后转为 `AssertionError`（传进来的是 `LoopContext` 不是 `ProviderContext`）。

- [ ] **Step 3: `ProviderContext` 增字段**

`src/ctx_weft/protocols/context.py`，在 `agent_id: str | None = None` 那行之后插入一行：

```python
    agent_id: str | None = None
    agent_template_id: str = ""  # 该 agent 的模板 id；授权按模板维度做策略（AllowListAuthorizer）
```

同时把类 docstring「携带：」列表里的
`- 当前 session/task/agent 标识（限定操作范围）`
改为
`- 当前 session/task/agent 标识 + agent 模板 id（限定操作范围 / 授权维度）`

- [ ] **Step 4: 改 `Authorizer` ABC 与 `filter` 默认实现**

`src/ctx_weft/core/auth/authorizer.py`。删掉 `from ctx_weft.core.state.models import Agent, Task` 整行，并把 ABC 改为：

```python
class Authorizer(ABC):
    """对一次 capability 调用作授权决定。

    只收 ``ProviderContext``（session/task/agent/模板 标识齐备），不收 core 的 Agent/Task
    状态对象——契约层不依赖 core 状态，host 自实现时也只需面对 protocols。
    """

    @abstractmethod
    async def authorize(
        self,
        capability: Capability,
        ctx: ProviderContext,
        arguments: dict[str, Any] | None = None,
        *,
        tool_call_id: str = "",
    ) -> AuthorizationDecision: ...

    async def filter(
        self,
        capabilities: list[Capability],
        ctx: ProviderContext,
        arguments: dict[str, Any] | None = None,
    ) -> list[Capability]:
        """批量可见性过滤（基于 authorize 的默认实现）。"""
        result = []
        for cap in capabilities:
            if (await self.authorize(cap, ctx, arguments)).allowed:
                result.append(cap)
        return result
```

- [ ] **Step 5: 改三个内置实现**

同一文件。`AllowAllAuthorizer`：

```python
@dataclass
class AllowAllAuthorizer(Authorizer):
    """默认：放行全部。"""

    async def authorize(self, capability, ctx, arguments=None, *, tool_call_id="") -> AuthorizationDecision:
        return AuthorizationDecision(allowed=True)
```

`AllowListAuthorizer`（`agent.template_id` → `ctx.agent_template_id`，其余规则一字不改）：

```python
    async def authorize(self, capability, ctx, arguments=None, *, tool_call_id="") -> AuthorizationDecision:
        tmpl = ctx.agent_template_id
        allowed = self.allow_map.get(tmpl)
        denied = self.deny_map.get(tmpl, set())
        if capability.id in denied:
            return AuthorizationDecision(allowed=False, message=self.deny_message)
        if allowed is not None and capability.id not in allowed:
            return AuthorizationDecision(allowed=False, message=self.deny_message)
        return AuthorizationDecision(allowed=True)
```

其 docstring 末尾补一行：`模板维度取自 ``ctx.agent_template_id``。`

`HumanConfirmationAuthorizer`（四处 `agent.` / `task.` → `ctx.`）：

```python
    async def authorize(self, capability, ctx, arguments=None, *, tool_call_id="") -> AuthorizationDecision:
        # 决定缓存命中直接用（cold reconcile，spec/07 §6）——内存优先,未命中回落事件日志
        # （否则跨重启再入会重新求批一遍）;内存 pending 由 request() 幂等复用。
        approval = await self.hitl_manager.find_resolved_for_tool_call(
            ctx.session_id, tool_call_id)
        if approval is None:
            hitl_id = await self.hitl_manager.request(
                form="approval",
                session_id=ctx.session_id,
                task_id=ctx.task_id or "",
                agent_id=ctx.agent_id or "",
                capability_id=capability.id,
                arguments=arguments or {},
                question=f"Allow tool '{capability.name}'?",
                context=capability.description,
                tool_call_id=tool_call_id,
            )
            approval = await self.hitl_manager.wait(hitl_id)   # may raise HitlPark on eviction
        if approval.accepted:
            return AuthorizationDecision(
                allowed=True,
                message=content_to_text(approval.message),
                modified_arguments=approval.modified_arguments,
            )
        logger.info("HITL blocked '%s' (status=%s)", capability.id, approval.status)
        return AuthorizationDecision(allowed=False, message=content_to_text(approval.message))
```

改完后该文件应不再出现 `Agent` / `Task` 两个名字。

- [ ] **Step 6: 改 gateway 调用点**

`src/ctx_weft/core/loop/capability_gateway.py:178-181`，把

```python
        authorizer = self._get_authorizer(cap.id)
        decision = await authorizer.authorize(
            cap, state.agent, state.task, ctx, arguments, tool_call_id=tool_call_id,
        )
```

改为

```python
        authorizer = self._get_authorizer(cap.id)
        # 交出 ProviderContext（不是 loop 的 LoopContext）——授权契约只认 protocols 类型。
        decision = await authorizer.authorize(
            cap, ctx.provider_ctx, arguments, tool_call_id=tool_call_id,
        )
```

- [ ] **Step 7: `_build_provider_ctx` 填模板 id**

`src/ctx_weft/core/runtime.py:1933` 的 `_build_provider_ctx`，在 `agent_id=agent.id,` 之后加一行：

```python
            agent_id=agent.id,
            agent_template_id=agent.template_id,
```

- [ ] **Step 8: 更新四个测试文件里的自定义 Authorizer 签名**

四处 `async def authorize(self, capability, agent, task, ctx, ...)` 全部去掉 `agent, task` 两个形参：

- `tests/unit/test_gateway_error_records_result.py:61` →
  `async def authorize(self, capability, ctx, arguments=None, *, tool_call_id="") -> AuthorizationDecision:`
- `tests/unit/test_tool_result_content_parts.py:70` →
  `async def authorize(self, capability, ctx, arguments=None, *, tool_call_id=""):`
  （原签名 `arguments` 无默认值且 `tool_call_id` 非 kw-only，一并订正为与 ABC 一致）
- `tests/unit/test_hitl_park.py:56` →
  `async def authorize(self, capability, ctx, arguments=None, *, tool_call_id=""):`
- `tests/unit/test_authorizer.py:185`（`_ModifyAuthorizer`）与 `:190`（`_DenyMsgAuthorizer`）→
  `async def authorize(self, capability, ctx, arguments=None, *, tool_call_id="") -> AuthorizationDecision:`

四处的方法体一律不动。

- [ ] **Step 9: 更新 `test_authorizer.py` 的直调点与 `_ctx()` helper**

把 `_ctx()` 改成能带模板 id 且字段齐备：

```python
def _ctx(template_id: str = "tmpl_a") -> ProviderContext:
    return ProviderContext(
        session_id="s1", tenant_id="default", agent_id="agt_1", agent_template_id=template_id,
    )
```

该文件里所有形如 `.filter(caps, _agent(), _task(), _ctx())` 的调用改为 `.filter(caps, _ctx())`；
所有 `.authorize(cap, _agent(), _task(), _ctx())` 改为 `.authorize(cap, _ctx())`。
原先靠 `_agent(template_id="X")` 区分模板的 AllowList 用例，改为 `_ctx(template_id="X")`。

改完后若 `_agent()` / `_task()` 已无任何引用，删除这两个 helper 及其 `SimpleNamespace` import（若 `SimpleNamespace` 别处仍用则保留）。

- [ ] **Step 10: 跑相关测试确认通过**

Run:
```
python -m pytest tests/unit/test_authorizer.py tests/unit/test_hitl.py tests/unit/test_hitl_park.py tests/unit/test_gateway_error_records_result.py tests/unit/test_tool_result_content_parts.py -v
```

预期：全 PASS，含 Step 1 新增的 `test_gateway_passes_provider_context_not_loop_context`。

- [ ] **Step 11: 全量测试 + lint**

Run:
```
python -m pytest -q
python -m ruff check src tests
```

预期：全 PASS。若别处还有旧签名调用残留，此步会暴露。

- [ ] **Step 12: 更新权威 spec**

`docs/spec/05-authz-and-hitl.md`：

(a) §1 的签名块改为：

```
authorize(capability, ctx, arguments?, *, tool_call_id="") -> AuthorizationDecision
AuthorizationDecision { allowed: bool, message: str = "", modified_arguments: dict | None = None, defer: bool = False }
```

(b) 该块下方的要点列表补两条：

```
- `defer`：挂起本次调用（不放行也不拒绝；gateway 绝不 invoke，上抛 HitlPark）。见 spec/07 §7。
- `ctx` 是 `ProviderContext`，携带 session/task/agent 标识与 `agent_template_id`；
  授权契约不依赖 core 的 Agent/Task 状态对象。
```

(c) §1「内置实现」表中 `AllowList` 一行的 `agent.template_id` 改为 `ctx.agent_template_id`；
「AllowList 规则」段里的 `deny_map[template_id]` / `allow_map.get(template_id)`，
把 `template_id` 明确为 `ctx.agent_template_id`。

(d) §2 步骤 2 的
`decision = authorizer.authorize(cap, agent, task, ctx, arguments)`
改为
`decision = authorizer.authorize(cap, ctx.provider_ctx, arguments, tool_call_id=…)`，
并在其后补一句：**传入的必须是 `ProviderContext`，不是 loop 的 `LoopContext`。**

(e) §5 清单第 1 条改为：

```
- [ ] `authorize(cap, ctx: ProviderContext, arguments?, *, tool_call_id) -> AuthorizationDecision{allowed, message, modified_arguments, defer}`；`filter` 为基于它的默认。
```

- [ ] **Step 13: Commit**

```bash
git add src/ctx_weft/protocols/context.py src/ctx_weft/core/auth/authorizer.py \
        src/ctx_weft/core/loop/capability_gateway.py src/ctx_weft/core/runtime.py \
        docs/spec/05-authz-and-hitl.md tests/unit/
git commit -m "refactor(auth): Authorizer 签名收口到 ProviderContext——去 Agent/Task 依赖,修 gateway 错传 LoopContext"
```

---

### Task 2: HITL 契约提层 → `protocols/hitl.py`，`HitlForm` 放宽为 `str`

**Files:**
- Create: `src/ctx_weft/protocols/hitl.py`
- Modify: `src/ctx_weft/protocols/__init__.py`（导出）
- Modify: `src/ctx_weft/core/state/models.py:288-330`（删除三个符号）
- Modify: `src/ctx_weft/core/control/reducers.py:17`
- Modify: `src/ctx_weft/core/control/types.py:12`
- Modify: `src/ctx_weft/core/orchestrator/hitl_manager.py:32`
- Modify: `src/ctx_weft/core/runtime.py:59`
- Modify: `docs/spec/05-authz-and-hitl.md`（`HitlRequest 字段` 段 + kind→form 订正）
- Test: `tests/unit/test_hitl_form_extensible.py`（新建）

**Interfaces:**
- Consumes: 无（与 Task 1 正交，可独立审阅）
- Produces:

```python
# ctx_weft.protocols.hitl —— 同时经 ctx_weft.protocols 顶层导出
HITL_FORM_APPROVAL = "approval"
HITL_FORM_QUESTION = "question"
HITL_FORM_WAIT = "wait"
HitlForm = str                                                         # 开放：host 可自定义
HitlStatus = Literal["pending", "accepted", "rejected", "cancelled"]   # 闭集
@dataclass class HitlRequest: ...   # 字段与原 core.state.models.HitlRequest 完全一致
```

- [ ] **Step 1: 写失败测试 —— 自定义 form 必须能贯通**

新建 `tests/unit/test_hitl_form_extensible.py`：

```python
"""HitlForm 是开放扩展点：host 可定义自己的 form，core 原样透传、不 assert。"""

from __future__ import annotations

from ctx_weft.core.orchestrator.hitl_manager import HitlManager
from ctx_weft.protocols.hitl import (
    HITL_FORM_APPROVAL,
    HITL_FORM_QUESTION,
    HITL_FORM_WAIT,
    HitlRequest,
)


def test_wellknown_form_constants() -> None:
    assert (HITL_FORM_APPROVAL, HITL_FORM_QUESTION, HITL_FORM_WAIT) == (
        "approval", "question", "wait",
    )


def test_hitl_request_accepts_custom_form() -> None:
    req = HitlRequest(id="h1", form="diff_review", session_id="s1", task_id="t1")
    assert req.form == "diff_review"
    assert req.status == "pending"
    assert req.accepted is False


async def test_manager_round_trips_custom_form() -> None:
    """core 不得对未知 form 做白名单校验：request → get 原样返回。"""
    mgr = HitlManager()
    hitl_id = await mgr.request(
        form="diff_review",
        session_id="s1",
        task_id="t1",
        question="Review this diff?",
    )
    got = await mgr.get(hitl_id)
    assert got is not None
    assert got.form == "diff_review"
```

若 `HitlManager.get()` 是同步方法，去掉该处 `await`（以 `hitl_manager.py` 实际签名为准）。

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m pytest tests/unit/test_hitl_form_extensible.py -v`

预期：FAIL，`ModuleNotFoundError: No module named 'ctx_weft.protocols.hitl'`。

- [ ] **Step 3: 新建 `protocols/hitl.py`**

把 `core/state/models.py` 的三个符号整段搬来，`now_utc` 换成本地 `_now_utc`，`HitlForm` 放宽为 `str`：

```python
"""HITL 契约：host-facing 的请求形态与状态。

``HitlRequest`` 是 core 与 host 之间的交换类型——host 读 ``form`` 决定 UI、经
``HitlManager.approve/answer/reject`` 回话。故它属于 protocols 而非 core 内部状态。

``form`` 是**开放扩展点**（``str`` 而非闭 ``Literal``）：host 可定义自己的等待形态，
core 只负责原样透传、不做白名单校验。``status`` 相反是**闭集**——状态机是 core 的
不变式，新增状态会破坏 reducer 投影。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Literal

if TYPE_CHECKING:
    from ctx_weft.protocols.context import ContentPart


def _now_utc() -> datetime:
    """UTC current time。

    刻意不复用 ``core.utils.now_utc``：protocols 是比 core 低的层，模块级反向 import
    会把依赖做反（同 ``protocols/context.py`` 顶部那处说明）。这一行重复的代价
    小于把 ``now_utc`` 下沉所牵动的连锁改动。
    """
    return datetime.now(UTC)


# 三种内建等待形态（spec 2026-07-05）：
#   approval — 审批门控：放行/拒绝一次工具调用（HumanConfirmationAuthorizer 触发）
#   question — ask_user 结构化提问，答复回灌 LLM
#   wait     — act 纯文本暂停 / 软打断（wait_for_user 冷 park）
# host 可自定义其它值；core 不校验。
HITL_FORM_APPROVAL = "approval"
HITL_FORM_QUESTION = "question"
HITL_FORM_WAIT = "wait"

HitlForm = str
HitlStatus = Literal["pending", "accepted", "rejected", "cancelled"]


@dataclass
class HitlRequest:
    """一次 HITL 请求（含其解析结果）。内存态与事件回放投影共用的单一实体。

    form 决定语义与应答形态：approval 用 approve/reject；question/wait 用 answer/reject。
    host 据 form 决定 UI（批准/拒绝按钮 vs 答题输入框 vs 普通输入框），并可自定义 form。
    """

    id: str                                       # 全局唯一，即 hitl_id
    form: HitlForm
    session_id: str
    task_id: str
    agent_id: str = ""
    capability_id: str = ""                       # approval: 被门控的工具；question: 触发提问的工具；wait: 保留 sentinel 值仅作信息
    tool_call_id: str = ""                        # 发起本次调用的 LLM tool_call id（短路门控的键）
    arguments: dict[str, Any] = field(default_factory=dict)
    question: str = ""                            # 展示给人类的问题（approval / wait 用）
    context: str = ""                             # wait 形态的来源（plain_text / interrupt / interrupt:edit）
    questions: list[dict[str, Any]] = field(default_factory=list)  # ask_user 的结构化批量问题（含 options/multi_select）
    status: HitlStatus = "pending"
    # 解析载荷
    # 人类附带的内容：答复 / 拒绝理由 / 备注。多模态回复（含图片）走同一字段。
    message: "str | list[ContentPart]" = ""
    modified_arguments: dict[str, Any] | None = None  # approval form：改写后的工具参数（暂仅记录，不生效）
    created_at: datetime = field(default_factory=_now_utc)
    resolved_at: datetime | None = None
    # resume-time LLM 覆盖：冷应答触发 session resume 时用的当前所选模型（host 据 entry 传入），
    # 仅供本次 cold-resolve 转发给 recover_session，不入事件、不持久化。
    resume_llm_account: str | None = None
    resume_llm_model: str | None = None

    @property
    def accepted(self) -> bool:
        return self.status == "accepted"
```

若原 `HitlRequest` 在 `accepted` 之后还有其它属性/方法，一并原样搬迁。

- [ ] **Step 4: 从 `core/state/models.py` 删除三个符号**

删掉 `# ── HITL ──` 分节：三行 form 说明注释、`HitlForm` / `HitlStatus` 两个别名、整个 `HitlRequest` 类。
删完后确认：`Literal` 仍被 `TaskStatus` 等使用故保留 import；
`ContentPart` 的 `TYPE_CHECKING` import 若已无引用则删除。

- [ ] **Step 5: 在 `protocols/__init__.py` 导出**

在既有的 `from ctx_weft.protocols.filesystem import ...` 之后插入：

```python
from ctx_weft.protocols.hitl import (
    HITL_FORM_APPROVAL,
    HITL_FORM_QUESTION,
    HITL_FORM_WAIT,
    HitlForm,
    HitlRequest,
    HitlStatus,
)
```

把这六个名字加进该文件的 `__all__`。模块 docstring 的契约清单补一行：
`- HitlForm / HitlStatus / HitlRequest（HITL 请求契约，host-facing）`

- [ ] **Step 6: 改四个 import 点**

`src/ctx_weft/core/control/reducers.py:17` —— 拆成两行：

```python
from ctx_weft.core.state.models import TaskStatus
from ctx_weft.protocols.hitl import HitlRequest
```

`src/ctx_weft/core/control/types.py:12`：

```python
from ctx_weft.protocols.hitl import HitlRequest
```

`src/ctx_weft/core/orchestrator/hitl_manager.py:32`：

```python
from ctx_weft.protocols.hitl import HitlForm, HitlRequest, HitlStatus  # noqa: F401  (HitlStatus re-export 供既有 import)
```

`src/ctx_weft/core/runtime.py:59` 所在的 `from ctx_weft.core.state.models import (...)` 块：
移除其中的 `HitlRequest,`，并在该块之后另起一行 `from ctx_weft.protocols.hitl import HitlRequest`。

- [ ] **Step 7: 扫残余 import**

Run: `grep -rn "state\.models import.*Hitl" --include=*.py src tests`

预期：无输出。有输出则按 Step 6 同法改掉。

- [ ] **Step 8: 跑相关测试确认通过**

Run: `python -m pytest tests/unit/test_hitl_form_extensible.py tests/unit/test_hitl.py tests/unit/test_hitl_park.py -v`

预期：全 PASS。若 `test_manager_round_trips_custom_form` 报「未知 form」类错误，
说明 `HitlManager` 或 `control/reducers.py` 里有 form 白名单校验——移除该校验（core 不得校验 form）。

- [ ] **Step 9: 全量测试 + lint + 层序检查**

Run:
```
python -m pytest -q
python -m ruff check src tests
grep -rn "from ctx_weft.core" src/ctx_weft/protocols/
```

预期：前两条全 PASS；第三条只应输出 `protocols/context.py` 里那处**函数体内**的惰性
`from ctx_weft.core import content as _m`（既有的、带说明的例外），不得有新增反向 import。

- [ ] **Step 10: 更新权威 spec**

`docs/spec/05-authz-and-hitl.md`：

(a) `### HitlRequest 字段` 段整体替换为：

```
`id / form / session_id / task_id / agent_id / capability_id / tool_call_id / arguments /
question / context / questions / status / message / modified_arguments / created_at / resolved_at`
（另有不入事件、不持久化的 `resume_llm_account` / `resume_llm_model`）。
`message` 承载人类自由文本（答复 / 拒绝指导 / 备注），任何 decision 下都可有。
`accepted` 便捷属性 = `status == "accepted"`。

> **`form` 是开放扩展点**：类型为 `str`，内建值 `approval` / `question` / `wait`
> 以 `protocols.hitl.HITL_FORM_*` 常量给出；host 可自定义其它值，core 原样透传、
> 不做白名单校验。`status` 相反是闭集——状态机是 core 不变式。
> 契约位置：`ctx_weft.protocols.hitl`（host-facing，非 core 内部状态）。
```

(b) 订正既有 spec 漂移：该文档 §3 表格与正文里所有把 HITL 判别称作 **`kind`** 的地方改为 **`form`**，
取值 `approval / input` 改为 `approval / question / wait`（`input` 对应现实现的 `question` / `wait` 两种）。
`request(kind, ...)` 的接口表一行改为 `request(form, ...)`。

(c) §5 清单里
`HITL **kind 判别**（approval / input）+ 状态机 ...`
改为
`HITL **form 判别**（approval / question / wait；form 为开放 str，core 不校验）+ 状态机 ...`

- [ ] **Step 11: Commit**

```bash
git add src/ctx_weft/protocols/hitl.py src/ctx_weft/protocols/__init__.py \
        src/ctx_weft/core/state/models.py src/ctx_weft/core/control/ \
        src/ctx_weft/core/orchestrator/hitl_manager.py src/ctx_weft/core/runtime.py \
        docs/spec/05-authz-and-hitl.md tests/unit/test_hitl_form_extensible.py
git commit -m "refactor(hitl): HitlRequest/HitlForm/HitlStatus 提到 protocols,form 放宽为开放 str"
```

---

### Task 3: `Authorizer` / `AuthorizationDecision` → `protocols/capability.py`

**Files:**
- Modify: `src/ctx_weft/protocols/capability.py`（追加两个符号）
- Modify: `src/ctx_weft/protocols/__init__.py`（导出）
- Modify: `src/ctx_weft/core/auth/authorizer.py`（改为从 protocols import）
- Test: `tests/unit/test_authorizer.py`

**Interfaces:**
- Consumes: Task 1 定下的 `authorize` / `filter` 签名
- Produces: `ctx_weft.protocols.capability.Authorizer` / `AuthorizationDecision`，同时经 `ctx_weft.protocols` 顶层导出

- [ ] **Step 1: 写失败测试 —— 契约必须在 protocols 且零 core 依赖**

追加到 `tests/unit/test_authorizer.py` 末尾：

```python
def test_authorizer_contract_lives_in_protocols() -> None:
    from ctx_weft.protocols import AuthorizationDecision as PD
    from ctx_weft.protocols import Authorizer as PA
    from ctx_weft.protocols.capability import AuthorizationDecision as CD
    from ctx_weft.protocols.capability import Authorizer as CA

    assert PA is CA and PD is CD


def test_protocols_capability_has_no_core_dependency() -> None:
    """契约层不得反向依赖 core（含 TYPE_CHECKING）。"""
    import pathlib

    import ctx_weft.protocols.capability as m

    src = pathlib.Path(m.__file__).read_text(encoding="utf-8")
    assert "ctx_weft.core" not in src
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m pytest tests/unit/test_authorizer.py::test_authorizer_contract_lives_in_protocols -v`

预期：FAIL，`ImportError: cannot import name 'Authorizer' from 'ctx_weft.protocols.capability'`。

- [ ] **Step 3: 把两个符号搬进 `protocols/capability.py`**

在 `protocols/capability.py` 末尾追加（`Capability` 与 `ProviderContext` 该文件已有）：

```python
# ── 授权契约 ───────────────────────────────────────────────────────────────────


@dataclass
class AuthorizationDecision:
    """一次授权的结构化结果。"""

    allowed: bool
    message: str = ""                              # 反馈 / 拒绝指导，回灌给 LLM（allow / deny 都可带）
    modified_arguments: dict[str, Any] | None = None  # allow 时的有效参数（None = 用原参）
    defer: bool = False                            # spec/07 §7：挂起本次调用（不放行也不拒绝；gateway 绝不 invoke）


class Authorizer(ABC):
    """对一次 capability 调用作授权决定。

    核心方法 ``authorize`` 对**一次工具调用**作放行/拦截决定，并可携带回灌给 LLM 的
    ``message``（反馈/拒绝指导）与 allow 时的 ``modified_arguments``（改写参数）。
    ``filter`` 是基于 ``authorize`` 的批量便捷默认（可见性过滤），保留给装配期/外部用。

    只收 ``ProviderContext``（session/task/agent/模板 标识齐备），不收 core 的 Agent/Task
    状态对象——契约层不依赖 core 状态，host 自实现时也只需面对 protocols。
    内置实现见 ``ctx_weft.providers.authorizer``。
    """

    @abstractmethod
    async def authorize(
        self,
        capability: Capability,
        ctx: ProviderContext,
        arguments: dict[str, Any] | None = None,
        *,
        tool_call_id: str = "",
    ) -> AuthorizationDecision: ...

    async def filter(
        self,
        capabilities: list[Capability],
        ctx: ProviderContext,
        arguments: dict[str, Any] | None = None,
    ) -> list[Capability]:
        """批量可见性过滤（基于 authorize 的默认实现）。"""
        result = []
        for cap in capabilities:
            if (await self.authorize(cap, ctx, arguments)).allowed:
                result.append(cap)
        return result
```

该文件顶部已有 `from abc import ABC, abstractmethod`、`from dataclasses import dataclass, field`、
`from typing import ... Any ...`、`from ctx_weft.protocols.context import ProviderContext`；
若缺任何一项则补上。

- [ ] **Step 4: `protocols/__init__.py` 导出**

在既有的 `from ctx_weft.protocols.capability import (...)` 块内按字母序加入
`AuthorizationDecision,` 与 `Authorizer,`，并同步加进 `__all__`。
模块 docstring 契约清单补一行：`- Authorizer / AuthorizationDecision（capability 授权契约）`

- [ ] **Step 5: `core/auth/authorizer.py` 改为 import**

删掉该文件里 `AuthorizationDecision` 与 `Authorizer` 两段定义，改为顶部 import：

```python
from ctx_weft.protocols.capability import AuthorizationDecision, Authorizer
```

删除因此不再使用的 import（`from abc import ABC, abstractmethod`；若 `field` 仍被
`AllowListAuthorizer` 使用则保留 `dataclass, field`）。模块 docstring 改为：

```python
"""Authorizer 的三个内置实现。契约本体在 ``protocols/capability.py``。"""
```

`core/auth/__init__.py` 本 Task 不动（Task 4 才删整个包）。

- [ ] **Step 6: 跑测试确认通过**

Run: `python -m pytest tests/unit/test_authorizer.py -v`

预期：全 PASS，含两个新测试。

- [ ] **Step 7: 全量测试 + lint**

Run:
```
python -m pytest -q
python -m ruff check src tests
```

预期：全 PASS。

- [ ] **Step 8: Commit**

```bash
git add src/ctx_weft/protocols/capability.py src/ctx_weft/protocols/__init__.py \
        src/ctx_weft/core/auth/authorizer.py tests/unit/test_authorizer.py
git commit -m "refactor(auth): Authorizer/AuthorizationDecision 契约落到 protocols/capability"
```

---

### Task 4: 三个内置实现 → `providers/authorizer/`，删除 `core/auth/`

**Files:**
- Create: `src/ctx_weft/providers/authorizer/__init__.py`
- Create: `src/ctx_weft/providers/authorizer/allow.py`
- Create: `src/ctx_weft/providers/authorizer/human.py`
- Delete: `src/ctx_weft/core/auth/`（整个目录）
- Modify: `src/ctx_weft/core/loop/capability_gateway.py:25` 与 `:140`
- Modify: `src/ctx_weft/core/runtime.py:34`
- Modify: `README.md:807`
- Modify: `docs/spec/05-authz-and-hitl.md`（顶部「真相源」）
- Test: `tests/unit/test_authorizer.py:16`、`test_hitl.py:18`、`test_hitl_park.py:23,30,224,246`、`test_gateway_error_records_result.py:19`、`test_tool_result_content_parts.py:13`

**Interfaces:**
- Consumes: Task 3 的 `ctx_weft.protocols.capability.Authorizer` / `AuthorizationDecision`
- Produces: `ctx_weft.providers.authorizer` 导出 `AllowAllAuthorizer` / `AllowListAuthorizer` / `HumanConfirmationAuthorizer`

- [ ] **Step 1: 写失败测试 —— 实现在 providers，`core.auth` 不复存在**

追加到 `tests/unit/test_authorizer.py` 末尾：

```python
def test_builtin_authorizers_live_in_providers() -> None:
    from ctx_weft.providers.authorizer import (
        AllowAllAuthorizer as A,
        AllowListAuthorizer as B,
        HumanConfirmationAuthorizer as C,
    )

    assert issubclass(A, Authorizer)
    assert issubclass(B, Authorizer)
    assert issubclass(C, Authorizer)


def test_core_auth_package_is_gone() -> None:
    """不留 re-export shim：旧路径必须彻底消失。"""
    import importlib

    import pytest

    with pytest.raises(ModuleNotFoundError):
        importlib.import_module("ctx_weft.core.auth")
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m pytest tests/unit/test_authorizer.py::test_builtin_authorizers_live_in_providers -v`

预期：FAIL，`ModuleNotFoundError: No module named 'ctx_weft.providers.authorizer'`。

- [ ] **Step 3: 新建 `src/ctx_weft/providers/authorizer/allow.py`**

```python
"""无状态授权策略：全放行 / 按模板白黑名单。契约见 ``protocols/capability.py``。"""

from __future__ import annotations

from dataclasses import dataclass, field

from ctx_weft.protocols.capability import AuthorizationDecision, Authorizer


@dataclass
class AllowAllAuthorizer(Authorizer):
    """默认：放行全部。"""

    async def authorize(self, capability, ctx, arguments=None, *, tool_call_id="") -> AuthorizationDecision:
        return AuthorizationDecision(allowed=True)


@dataclass
class AllowListAuthorizer(Authorizer):
    """按 agent 模板白/黑名单放行 capability id。

    allow_map: {template_id: set[capability_id]} —— 空集 = 全拦；模板不在表中 = 不限制。
    deny_map:  {template_id: set[capability_id]} —— deny 优先。
    deny_message: 被拦截时回灌给 LLM 的统一说明（可空）。
    模板维度取自 ``ctx.agent_template_id``。
    """

    allow_map: dict[str, set[str]] = field(default_factory=dict)
    deny_map: dict[str, set[str]] = field(default_factory=dict)
    deny_message: str = ""

    async def authorize(self, capability, ctx, arguments=None, *, tool_call_id="") -> AuthorizationDecision:
        tmpl = ctx.agent_template_id
        allowed = self.allow_map.get(tmpl)
        denied = self.deny_map.get(tmpl, set())
        if capability.id in denied:
            return AuthorizationDecision(allowed=False, message=self.deny_message)
        if allowed is not None and capability.id not in allowed:
            return AuthorizationDecision(allowed=False, message=self.deny_message)
        return AuthorizationDecision(allowed=True)
```

- [ ] **Step 4: 新建 `src/ctx_weft/providers/authorizer/human.py`**

```python
"""HITL 审批授权：每次工具调用前暂停，等人工确认后再放行。"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING

from ctx_weft.core.utils import content_to_text
from ctx_weft.protocols.capability import AuthorizationDecision, Authorizer

if TYPE_CHECKING:
    from ctx_weft.core.orchestrator.hitl_manager import HitlManager

logger = logging.getLogger(__name__)


@dataclass
class HumanConfirmationAuthorizer(Authorizer):
    """每次工具调用前暂停，等待人工确认后再放行。

    shell 侧持有同一个 HitlManager 实例，通过 approve() / reject() 响应；可在 approve 时
    携带 modified_arguments（改写参数），或在 reject 时携带 message（指导反馈）——二者经
    AuthorizationDecision 流出，由 CapabilityGateway 应用 / 回灌。
    """

    hitl_manager: "HitlManager"

    async def authorize(self, capability, ctx, arguments=None, *, tool_call_id="") -> AuthorizationDecision:
        # 决定缓存命中直接用（cold reconcile，spec/07 §6）——内存优先,未命中回落事件日志
        # （否则跨重启再入会重新求批一遍）;内存 pending 由 request() 幂等复用。
        approval = await self.hitl_manager.find_resolved_for_tool_call(
            ctx.session_id, tool_call_id)
        if approval is None:
            hitl_id = await self.hitl_manager.request(
                form="approval",
                session_id=ctx.session_id,
                task_id=ctx.task_id or "",
                agent_id=ctx.agent_id or "",
                capability_id=capability.id,
                arguments=arguments or {},
                question=f"Allow tool '{capability.name}'?",
                context=capability.description,
                tool_call_id=tool_call_id,
            )
            approval = await self.hitl_manager.wait(hitl_id)   # may raise HitlPark on eviction
        if approval.accepted:
            return AuthorizationDecision(
                allowed=True,
                message=content_to_text(approval.message),
                modified_arguments=approval.modified_arguments,
            )
        logger.info("HITL blocked '%s' (status=%s)", capability.id, approval.status)
        return AuthorizationDecision(allowed=False, message=content_to_text(approval.message))
```

`content_to_text` 直接取自 `core.utils`（`core/content.py` 只是 re-export），少一层间接。
`providers` → `core` 方向是既有惯例（见 `providers/llm/provider.py`、`providers/memory/sql/provider.py`）。

- [ ] **Step 5: 新建 `src/ctx_weft/providers/authorizer/__init__.py`**

```python
"""Authorizer 的内置参考实现。契约在 ``ctx_weft.protocols.capability``。"""

from ctx_weft.providers.authorizer.allow import AllowAllAuthorizer, AllowListAuthorizer
from ctx_weft.providers.authorizer.human import HumanConfirmationAuthorizer

__all__ = [
    "AllowAllAuthorizer",
    "AllowListAuthorizer",
    "HumanConfirmationAuthorizer",
]
```

- [ ] **Step 6: 删除 `core/auth/`**

```bash
git rm -r --cached src/ctx_weft/core/auth
rm -rf src/ctx_weft/core/auth
```

- [ ] **Step 7: 改 gateway —— import + 默认惰性解析**

`src/ctx_weft/core/loop/capability_gateway.py:25`：

```python
from ctx_weft.protocols.capability import Authorizer  # noqa: F401
```

`:140` 的 `self._default_authorizer: Authorizer = default_authorizer or AllowAllAuthorizer()` 改为
（D6：默认只在 host 没给时才解析，避免「默认」从运行期选择退化成 import 期耦合）：

```python
        if default_authorizer is None:
            from ctx_weft.providers.authorizer import AllowAllAuthorizer
            default_authorizer = AllowAllAuthorizer()
        self._default_authorizer: Authorizer = default_authorizer
```

- [ ] **Step 8: 改 runtime import**

`src/ctx_weft/core/runtime.py:34` 改为：

```python
from ctx_weft.protocols.capability import Authorizer
```

然后确认 `AllowAllAuthorizer` 在 `runtime.py` 中已无引用：

Run: `grep -n "AllowAllAuthorizer" src/ctx_weft/core/runtime.py`

预期：无输出。若有残留，就地改为函数体内按需 import。

- [ ] **Step 9: 改测试 import**

契约类 → `ctx_weft.protocols.capability`；三个实现 → `ctx_weft.providers.authorizer`：

- `tests/unit/test_authorizer.py:16` 的整块 import 拆成两条：
  ```python
  from ctx_weft.protocols.capability import AuthorizationDecision, Authorizer
  from ctx_weft.providers.authorizer import AllowAllAuthorizer, AllowListAuthorizer
  ```
- `tests/unit/test_gateway_error_records_result.py:19` → `from ctx_weft.protocols.capability import AuthorizationDecision, Authorizer`
- `tests/unit/test_tool_result_content_parts.py:13` → 同上
- `tests/unit/test_hitl.py:18` → `from ctx_weft.providers.authorizer import HumanConfirmationAuthorizer`
- `tests/unit/test_hitl_park.py:23` → `from ctx_weft.protocols.capability import AuthorizationDecision`
- `tests/unit/test_hitl_park.py:30` → `from ctx_weft.protocols.capability import AuthorizationDecision, Authorizer`
- `tests/unit/test_hitl_park.py:224` 与 `:246` → `from ctx_weft.providers.authorizer import HumanConfirmationAuthorizer`

- [ ] **Step 10: 扫残余引用**

Run: `grep -rn "core\.auth" --include=*.py --include=*.md src tests README.md docs/spec`

预期：无输出。（`docs/superpowers/plans/2026-06-12-hitl-hot-cold-phase-c.md` 里的
`loomex_core.core.auth.*` 是旧包名的历史计划记录，不在本次扫描路径内，也不改。）

- [ ] **Step 11: 全量测试 + lint**

Run:
```
python -m pytest -q
python -m ruff check src tests
```

预期：全 PASS，含 Step 1 的两个新测试。

- [ ] **Step 12: 更新 README 与 spec 真相源**

`README.md:807` 起的 import 块，把

```python
from ctx_weft.core.auth import (
    Authorizer, AllowAllAuthorizer, AllowListAuthorizer, HumanConfirmationAuthorizer,
)
```

改为

```python
from ctx_weft.protocols.capability import Authorizer
from ctx_weft.providers.authorizer import (
    AllowAllAuthorizer, AllowListAuthorizer, HumanConfirmationAuthorizer,
)
```

（该代码块后续演示行不变；`README.md:815` 与 `:822` 的 `HumanConfirmationAuthorizer(...)` 用法照旧。）

`docs/spec/05-authz-and-hitl.md` 顶部「真相源」一行，把 `core/auth/authorizer.py` 换成
`protocols/capability.py`（契约）+ `providers/authorizer/`（实现），其余两项保持不变。

- [ ] **Step 13: Commit**

```bash
git add -A src/ctx_weft README.md docs/spec/05-authz-and-hitl.md tests/unit/
git commit -m "refactor(auth): 三个内置 Authorizer 落到 providers/authorizer,删除 core/auth 包"
```

---

### Task 5: `runtime` 的 `event_bus` 可注入 + 兜底 import 一致化

**Files:**
- Modify: `src/ctx_weft/core/runtime.py:89`（删模块级 import）
- Modify: `src/ctx_weft/core/runtime.py:468-509`（`__init__` 增参 + 兜底 import）
- Modify: `README.md:495` 附近的构造参数说明
- Test: `tests/unit/test_runtime_event_bus_injection.py`（新建）

**Interfaces:**
- Consumes: 无（与前四个 Task 正交）
- Produces:

```python
CtxWeftRuntime(
    providers=None, llm=None, hitl_manager=None,
    event_bus: EventBus | None = None,     # 新增，位置在 hitl_manager 之后、event_store 之前
    event_store=None, config=None, snapshot_every_n=0,
)
```

- [ ] **Step 1: 写失败测试**

新建 `tests/unit/test_runtime_event_bus_injection.py`：

```python
"""event_bus 必须可由 host 注入（与 event_store / hitl_manager / llm 一致）。"""

from __future__ import annotations

from ctx_weft.core.runtime import CtxWeftRuntime
from ctx_weft.providers.events import InProcessEventBus


def test_event_bus_is_injectable(runtime_registry) -> None:
    bus = InProcessEventBus()
    rt = CtxWeftRuntime(providers=runtime_registry, event_bus=bus)
    assert rt.event_bus is bus


def test_event_bus_defaults_when_absent(runtime_registry) -> None:
    rt = CtxWeftRuntime(providers=runtime_registry)
    assert isinstance(rt.event_bus, InProcessEventBus)


def test_runtime_module_has_no_toplevel_provider_import() -> None:
    """兜底实现只在需要时才 import——不得在模块级把 providers 拖进来。"""
    import pathlib

    import ctx_weft.core.runtime as m

    lines = pathlib.Path(m.__file__).read_text(encoding="utf-8").splitlines()
    toplevel = [
        ln for ln in lines
        if ln.startswith("from ctx_weft.providers") or ln.startswith("import ctx_weft.providers")
    ]
    assert toplevel == [], f"模块级 providers import 残留: {toplevel}"
```

`runtime_registry` 是一个满足 runtime 硬校验（至少一个 `AgentCapabilityProvider`）的
`ProviderRegistry`。**先查 `tests/unit/conftest.py`**：若已有等价 fixture（构造 registry 或
runtime 的 helper），直接复用其名字；没有则在该测试文件内加：

```python
import pytest

from ctx_weft.core.runtime import ProviderRegistry
from ctx_weft.providers.agent_template_local import LocalAgentTemplateProvider


@pytest.fixture
def runtime_registry(tmp_path):
    reg = ProviderRegistry()
    reg.register_capability(LocalAgentTemplateProvider(str(tmp_path)))
    return reg
```

（`LocalAgentTemplateProvider` 的实际构造签名以 `providers/agent_template_local/` 为准。）

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m pytest tests/unit/test_runtime_event_bus_injection.py -v`

预期：`test_event_bus_is_injectable` FAIL（`TypeError: __init__() got an unexpected keyword argument 'event_bus'`），
`test_runtime_module_has_no_toplevel_provider_import` FAIL（`runtime.py:89` 残留）。

- [ ] **Step 3: 删模块级 import**

`src/ctx_weft/core/runtime.py:89`，删掉整行 `from ctx_weft.providers.events import InProcessEventBus`。
保留其上一行的 `from ctx_weft.protocols.events import EventBus`。

- [ ] **Step 4: `__init__` 增参并按需兜底**

`src/ctx_weft/core/runtime.py:468` 的签名，在 `hitl_manager` 之后插入一行：

```python
    def __init__(
        self,
        providers: ProviderRegistry | None = None,
        llm: LLMClient | None = None,
        hitl_manager: HitlManager | None = None,
        event_bus: EventBus | None = None,
        event_store: "Any | None" = None,
        config: "RuntimeConfig | None" = None,
        snapshot_every_n: int = 0,
    ) -> None:
```

`:481` 的 `self._event_bus = InProcessEventBus()` 改为：

```python
        # 默认实现只在 host 没给时才解析——避免「默认」从运行期选择退化成 import 期耦合。
        if event_bus is None:
            from ctx_weft.providers.events import InProcessEventBus
            event_bus = InProcessEventBus()
        self._event_bus: EventBus = event_bus
```

`:498-499` 的

```python
        from ctx_weft.providers.events import InMemoryEventStore, attach_persistence
        self.event_store = event_store or InMemoryEventStore()
```

改为（`attach_persistence` 始终需要，单独 import；默认 store 仅在缺省时解析）：

```python
        from ctx_weft.providers.events import attach_persistence
        if event_store is None:
            from ctx_weft.providers.events import InMemoryEventStore
            event_store = InMemoryEventStore()
        self.event_store = event_store
```

`attach_persistence(...)` 的调用与其上方那段说明注释**不动**。

- [ ] **Step 5: 跑测试确认通过**

Run: `python -m pytest tests/unit/test_runtime_event_bus_injection.py -v`

预期：三个测试全 PASS。

- [ ] **Step 6: 全量测试 + lint + 扫已删模块属性的引用**

Run:
```
python -m pytest -q
python -m ruff check src tests
grep -rn "runtime import InProcessEventBus\|runtime\.InProcessEventBus" --include=*.py src tests
```

预期：前两条全 PASS，第三条无输出（确认没人靠 `core.runtime` 转手拿 `InProcessEventBus`）。

- [ ] **Step 7: 更新 README 构造参数说明**

`README.md:495` 附近的构造参数清单里，在 `hitl_manager=None,  # 可选，默认自动创建` 之后补一行：

```
    event_bus=None,                   # 可选，默认 InProcessEventBus；注入以接 Redis / NATS / 跨进程
```

- [ ] **Step 8: Commit**

```bash
git add src/ctx_weft/core/runtime.py README.md tests/unit/test_runtime_event_bus_injection.py
git commit -m "refactor(runtime): event_bus 改为可注入,兜底实现按需 import"
```

---

## 完成后的验收

- [ ] `python -m pytest -q` 全绿
- [ ] `python -m ruff check src tests` 无告警
- [ ] `grep -rn "from ctx_weft.core" src/ctx_weft/protocols/` 只剩 `context.py` 里那处带说明的函数内惰性 import
- [ ] `grep -rn "core\.auth" --include=*.py src tests` 无输出
- [ ] `grep -rn "^from ctx_weft.providers" src/ctx_weft/core/runtime.py` 无输出
- [ ] `docs/spec/05-authz-and-hitl.md` 的签名、`form` 判别、`HitlRequest` 字段表、真相源路径均与代码一致

## 遗留（本计划不做，另议）

- **TS / Java 移植需同步**：`docs/spec/05-authz-and-hitl.md` 顶部声明「三份实现须复现同一断言」，
  §5 第 1 条正是 `authorize` 的签名。Task 1 是一次破坏性跨语言契约变更。
- **`Authorizer` 在 `ProviderRegistry` 上的旁挂设计**（`runtime.py:214` 的 `dict[str, Authorizer]`）：
  `providers/` 收的是 Provider 协议的实现，而 authorizer 是 provider 注册的**参数**而非并列条目。
  这个设计要不要重整，待议。
