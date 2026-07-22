# AgentCapabilityProvider 协议补全 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让 `AgentCapabilityProvider` 成为模板进入 core 的唯一通道——协议加 `get_template()`，新增前缀精确路由的 `TemplateLookup` 取代「qualified 反查 + 单例 TemplateResolver」双路径，两仓破坏性同步迁移 + host DB 存量事件规范化。

**Architecture:** 依据已批准 spec `docs/superpowers/specs/2026-07-22-agent-capability-template-protocol-design.md`。发现与加载同源：provider `list()` 出的每个模板它自己的 `get_template()` 必须能加载；加载按 `cap.id` 前缀（`rsplit(":", 1)`）路由到唯一 provider，裸 id 一律 `TemplateNotFoundError`；规范化推到两个边界（host `start_session` 传参、LLM 用 qualified 名）；存量事件/快照由 host m010 迁移抹平。

**Tech Stack:** Python 3.11+，两仓均 `uv`。ctx-weft（`C:\Users\Xing\Documents\codes\Loome-02\ctx-weft`）为引擎；IpMasterCoworkPy（`C:\Users\Xing\Documents\codes\IpMasterCoworkPy`）为 host，经 vendored wheel 消费引擎。

## Global Constraints

- 直接破坏性变更，**不留兼容垫片**：`CtxWeftRuntime(template_resolver=...)` 构造参数、`runtime.template_resolver` 属性、`runtime._resolve_subagent_template` 一律删除。
- `CtxWeftRuntime` 构造时 ProviderRegistry 中**必须已有 ≥1 个 `AgentCapabilityProvider`**，否则抛 `ValueError`（fail-fast，硬校验）。
- `get_template` 协议契约：入参为 provider 命名空间内的**局部模板名**（前缀已剥）；不认识 → 返回 `None`；仅真实故障（IO/网络/解析）才抛异常；`version=None` 取最新。
- 加载一律 `version=None`（保 `TemplateDirResolver` 热更新语义）；`AgentCapability.version` 仅信息性。
- 前缀拆分口径：`rsplit(":", 1)`，与 `assembler/sources/capability.py` 的 `_provider_meta` 一致。
- 任务顺序**必须** Task 1 → 2 → 3（ctx-weft，依次提交）→ revendor → Task 4 → 5（host）。host 测试前先跑 `powershell -File scripts\revendor-core.ps1`（在 IpMasterCoworkPy 仓根）把改后的 ctx-weft 重新打 wheel 并 `uv sync`。
- 测试命令：两仓均 `uv run pytest <path> -q`（仓根执行）。
- 提交信息风格随仓库现状：中文、conventional 前缀（`feat(protocols): ...`），结尾加 `Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>`。
- host 迁移 id 固定为 `m010_canonical_template_id_prefix`（现有列表止于 m009；**绝不**改动/重排既有 id）。

---

### Task 1: 协议层——`get_template` 抽象 + `retrieve()` 默认空 + 适配器实现（ctx-weft）

**Files:**
- Modify: `src/ctx_weft/protocols/capability.py`（`AgentCapability` ~82 行、`AgentCapabilityProvider` ~212 行）
- Modify: `src/ctx_weft/core/orchestrator/agent_capability.py`
- Test: `tests/unit/test_agent_capability_provider.py`

**Interfaces:**
- Consumes: `TemplateResolver.get(template_id, version, ctx) -> AgentTemplate`（raise `KeyError` on miss，现状不变）
- Produces（Task 2/3 依赖）:
  - `AgentCapabilityProvider.get_template(self, template_id: str, version: str | None, ctx: ProviderContext) -> AgentTemplate | None`（abstract async）
  - `AgentCapabilityProvider.retrieve(ctx) -> list[Capability]`（默认返回 `[]`）
  - `AgentCapability.version: str = ""`
  - `TemplateAgentCapabilityProvider.get_template`：委托 resolver，`KeyError → None`

- [ ] **Step 1: 写失败测试**——`tests/unit/test_agent_capability_provider.py` **整文件替换**为以下内容（原有 list 用例已并入并扩展 version 断言；`_Resolver` 升级为可加载版）：

```python
"""TemplateAgentCapabilityProvider maps template summaries to AgentCapability."""

from __future__ import annotations

from ctx_weft.core.orchestrator.agent_capability import (
    PROVIDER_NAME, TemplateAgentCapabilityProvider,
)
from ctx_weft.protocols.capability import AgentCapability
from ctx_weft.protocols.context import ProviderContext
from ctx_weft.protocols.template import (
    AgentTemplate, AgentTemplateSummary, LoopConfig, MemoryConfig,
)

_TEMPLATE = AgentTemplate(
    id="planner", name="Planner", version="1",
    identity={}, capability_refs=[],
    memory_config=MemoryConfig(), loop_config=LoopConfig(),
)


class _Resolver:
    def __init__(self) -> None:
        self.get_calls: list[tuple[str, str | None]] = []

    async def list_summaries(self, ctx):
        return [AgentTemplateSummary(id="planner", name="Planner", version="1",
                                     description="plans things")]

    async def get(self, template_id, version, ctx):
        self.get_calls.append((template_id, version))
        if template_id == "planner":
            return _TEMPLATE
        raise KeyError(template_id)


_CTX = ProviderContext(session_id="s1", tenant_id="default")


async def test_lists_templates_as_agent_capabilities() -> None:
    prov = TemplateAgentCapabilityProvider(_Resolver())
    caps = await prov.list(_CTX)
    assert len(caps) == 1
    cap = caps[0]
    assert isinstance(cap, AgentCapability)
    assert cap.id == "agent:planner"
    assert cap.name == "planner"
    assert cap.template_name == "planner"
    assert cap.description == "plans things"
    assert cap.version == "1"          # 新增：listing 携带信息性 version
    assert PROVIDER_NAME == "agent"


async def test_get_template_delegates_to_resolver() -> None:
    resolver = _Resolver()
    prov = TemplateAgentCapabilityProvider(resolver)
    t = await prov.get_template("planner", None, _CTX)
    assert t is _TEMPLATE
    assert resolver.get_calls == [("planner", None)]


async def test_get_template_unknown_id_returns_none() -> None:
    prov = TemplateAgentCapabilityProvider(_Resolver())
    assert await prov.get_template("nope", None, _CTX) is None


async def test_retrieve_defaults_to_empty_allowlist() -> None:
    """allowlist 语义在基类：list 出全目录，retrieve 永不自动召回。"""
    prov = TemplateAgentCapabilityProvider(_Resolver())
    assert await prov.retrieve(_CTX) == []
```

- [ ] **Step 2: 跑测试确认失败**

Run: `uv run pytest tests/unit/test_agent_capability_provider.py -q`
Expected: FAIL——`version` 断言失败 + `get_template` 属性不存在。

- [ ] **Step 3: 改协议**——`src/ctx_weft/protocols/capability.py`：

3a. 文件头 import 区加 TYPE_CHECKING 块（**不可**直接 import template.py——template.py 反向 import 本模块的 `Purpose`，会循环）：

```python
from typing import TYPE_CHECKING, Any, Literal

if TYPE_CHECKING:
    from ctx_weft.protocols.template import AgentTemplate
```

3b. `AgentCapability`（~82 行）加字段：

```python
@dataclass
class AgentCapability(Capability):
    """Sub-agent 模板描述符，由 orchestrator 负责 spawn。"""
    kind: str = "agent"
    template_name: str = ""
    version: str = ""  # 信息性（listing 展示）；加载一律 version=None 取最新
```

3c. `AgentCapabilityProvider`（~212 行）整体替换：

```python
class AgentCapabilityProvider(CapabilityProvider, ABC):
    """列出可用 sub-agent 模板，并负责加载自己列出的模板。

    发现与加载同源（spec 2026-07-22）：list() 返回的每个 AgentCapability.template_name，
    本 provider 的 get_template() 必须能加载。TemplateLookup 按 cap.id 前缀路由到本
    provider 后，传入的是**局部模板名**（前缀已剥掉）。
    """

    @abstractmethod
    async def get_template(
        self, template_id: str, version: str | None, ctx: ProviderContext,
    ) -> "AgentTemplate | None":
        """加载模板定义。不认识该 id → 返回 None（由 TemplateLookup 转成
        TemplateNotFoundError）；仅真实故障（IO/网络/解析错误）才抛异常。
        version=None 取最新。"""
        ...

    async def retrieve(self, ctx: ProviderContext) -> list[Capability]:
        """默认不自动召回：sub-agent 只经模板声明的 `subagents` required refs 绑定
        （allowlist），永不把整个模板目录泄漏给 agent。确需语义召回的 provider 可覆盖。"""
        return []
```

- [ ] **Step 4: 改适配器**——`src/ctx_weft/core/orchestrator/agent_capability.py`：

4a. `list()` 的 `AgentCapability(...)` 构造加 `version=s.version`。

4b. **删除**现有 `retrieve()` override（连同其 docstring——语义已上移基类）。

4c. 在 `list()` 之后加：

```python
    async def get_template(
        self, template_id: str, version: str | None, ctx: ProviderContext,
    ) -> AgentTemplate | None:
        try:
            return await self._resolver.get(template_id, version=version, ctx=ctx)
        except KeyError:
            return None
```

同时把文件头 import 补 `from ctx_weft.protocols.template import AgentTemplate, TemplateResolver`（orchestrator 层 import template.py 无循环问题），并把模块 docstring 里「list 型 provider」句改为「发现与加载同源 provider」。

- [ ] **Step 5: 跑测试确认通过**

Run: `uv run pytest tests/unit/test_agent_capability_provider.py tests/unit/test_subagent_scoping.py -q`
Expected: PASS（scoping 测试验证 allowlist 语义经基类默认 `retrieve()` 仍成立）。

- [ ] **Step 6: 改 `protocols/template.py` docstring**——`TemplateResolver` 类 docstring 从「core 与 host 之间关于 template 的唯一接口。host 实现。」改为：

```python
    """目录/注册表型模板源的 SPI——经 TemplateAgentCapabilityProvider 适配接入 core。

    不再是 core↔host 的模板接口（spec 2026-07-22）：模板进入 core 的唯一通道是
    AgentCapabilityProvider；host 可实现本协议后用适配器注册，也可直接实现
    AgentCapabilityProvider。
    """
```

- [ ] **Step 7: 提交**

```bash
git add src/ctx_weft/protocols/capability.py src/ctx_weft/protocols/template.py src/ctx_weft/core/orchestrator/agent_capability.py tests/unit/test_agent_capability_provider.py
git commit -m "feat(protocols): AgentCapabilityProvider 加 get_template——发现与加载同源"
```

---

### Task 2: `TemplateNotFoundError` + `TemplateLookup` 前缀精确路由（ctx-weft）

**Files:**
- Modify: `src/ctx_weft/core/errors.py`
- Create: `src/ctx_weft/core/orchestrator/template_lookup.py`
- Test: `tests/unit/test_template_lookup.py`（新建）

**Interfaces:**
- Consumes: Task 1 的 `AgentCapabilityProvider.get_template`；`ProviderRegistry.get_capability_providers() -> list[CapabilityProvider]`（runtime.py:292，现状）；`qualify(capability_id) -> str`（protocols/capability.py:32）
- Produces（Task 3 依赖）:
  - `TemplateLookup(providers: ProviderRegistry)`
  - `await lookup.resolve_qualified(qualified: str, ctx) -> str`（命中返回完整 cap.id；未命中原样返回）
  - `await lookup.get_template(ref: str, version: str | None, ctx) -> AgentTemplate`（miss 抛 `TemplateNotFoundError`）
  - `ctx_weft.core.errors.TemplateNotFoundError(template_ref, providers=[...])`

- [ ] **Step 1: 写失败测试**——新建 `tests/unit/test_template_lookup.py`：

```python
"""TemplateLookup：qualified 反查保 provider 归属 + cap.id 前缀精确路由（spec 2026-07-22）。"""

from __future__ import annotations

import pytest

from ctx_weft.core.errors import TemplateNotFoundError
from ctx_weft.core.orchestrator.template_lookup import TemplateLookup
from ctx_weft.core.runtime import ProviderRegistry
from ctx_weft.protocols.capability import (
    AgentCapability, AgentCapabilityProvider, CapabilityProviderInfo,
)
from ctx_weft.protocols.context import ProviderContext
from ctx_weft.protocols.template import AgentTemplate, LoopConfig, MemoryConfig

_CTX = ProviderContext(session_id="s1", tenant_id="default")


def _template(tid: str) -> AgentTemplate:
    return AgentTemplate(id=tid, name=tid, version="1", identity={},
                         capability_refs=[], memory_config=MemoryConfig(),
                         loop_config=LoopConfig())


class _Prov(AgentCapabilityProvider):
    """内存 agent provider 桩：name 可含冒号（多段），get_template 记录调用。"""

    def __init__(self, name: str, templates: dict[str, AgentTemplate],
                 list_error: bool = False, get_error: bool = False) -> None:
        self.name = name
        self._templates = templates
        self._list_error = list_error
        self._get_error = get_error
        self.get_calls: list[str] = []

    async def list(self, ctx):
        if self._list_error:
            raise RuntimeError("list boom")
        return [AgentCapability(id=f"{self.name}:{tid}", name=tid, template_name=tid)
                for tid in self._templates]

    async def get_template(self, template_id, version, ctx):
        self.get_calls.append(template_id)
        if self._get_error:
            raise RuntimeError("get boom")
        return self._templates.get(template_id)

    async def describe(self, ctx):
        return CapabilityProviderInfo(name=self.name)


def _lookup(*provs: _Prov) -> TemplateLookup:
    reg = ProviderRegistry()
    for p in provs:
        reg.register_capability(p)
    return TemplateLookup(reg)


# ── resolve_qualified ────────────────────────────────────────────────────────

async def test_resolve_qualified_returns_full_cap_id() -> None:
    lookup = _lookup(_Prov("agent", {"planner": _template("planner")}))
    assert await lookup.resolve_qualified("agent__planner", _CTX) == "agent:planner"


async def test_resolve_qualified_miss_passes_through() -> None:
    lookup = _lookup(_Prov("agent", {"planner": _template("planner")}))
    assert await lookup.resolve_qualified("nope__x", _CTX) == "nope__x"


async def test_resolve_qualified_skips_broken_provider() -> None:
    broken = _Prov("bad", {}, list_error=True)
    good = _Prov("agent", {"planner": _template("planner")})
    lookup = _lookup(broken, good)
    assert await lookup.resolve_qualified("agent__planner", _CTX) == "agent:planner"


# ── get_template ─────────────────────────────────────────────────────────────

async def test_get_template_routes_by_prefix() -> None:
    t = _template("planner")
    lookup = _lookup(_Prov("agent", {"planner": t}))
    assert await lookup.get_template("agent:planner", None, _CTX) is t


async def test_get_template_multisegment_provider_name() -> None:
    t = _template("researcher")
    lookup = _lookup(_Prov("mcp:github", {"researcher": t}))
    assert await lookup.get_template("mcp:github:researcher", None, _CTX) is t


async def test_routed_provider_miss_does_not_fall_through() -> None:
    """路由确定后其余 provider 不参与——即使别家有同名模板。"""
    empty = _Prov("agent", {})
    other = _Prov("other", {"planner": _template("planner")})
    lookup = _lookup(empty, other)
    with pytest.raises(TemplateNotFoundError):
        await lookup.get_template("agent:planner", None, _CTX)
    assert other.get_calls == []


async def test_bare_id_raises_with_canonical_hint() -> None:
    lookup = _lookup(_Prov("agent", {"planner": _template("planner")}))
    with pytest.raises(TemplateNotFoundError, match="provider:name"):
        await lookup.get_template("planner", None, _CTX)


async def test_unknown_prefix_raises() -> None:
    lookup = _lookup(_Prov("agent", {"planner": _template("planner")}))
    with pytest.raises(TemplateNotFoundError):
        await lookup.get_template("nope:planner", None, _CTX)


async def test_no_agent_providers_message() -> None:
    lookup = TemplateLookup(ProviderRegistry())
    with pytest.raises(TemplateNotFoundError, match="no AgentCapabilityProvider"):
        await lookup.get_template("agent:planner", None, _CTX)


async def test_provider_fault_propagates() -> None:
    lookup = _lookup(_Prov("agent", {"planner": _template("planner")}, get_error=True))
    with pytest.raises(RuntimeError, match="get boom"):
        await lookup.get_template("agent:planner", None, _CTX)
```

- [ ] **Step 2: 跑测试确认失败**

Run: `uv run pytest tests/unit/test_template_lookup.py -q`
Expected: FAIL——`ImportError: cannot import name 'TemplateNotFoundError'`。

- [ ] **Step 3: 加错误类型**——`src/ctx_weft/core/errors.py` 的「Capability 相关」段末尾追加：

```python
class TemplateNotFoundError(CtxWeftError):
    """模板查找失败：裸 id 无前缀 / 前缀路由不到 provider / provider 不认识局部名。

    模板引用必须是规范形式 'provider:name'（如 'agent:planner'）——边界强制前缀
    （spec 2026-07-22），core 不做扫描回落。"""

    code = "TEMPLATE_NOT_FOUND"

    def __init__(self, template_ref: str, *, providers: list[str] | None = None) -> None:
        self.template_ref = template_ref
        self.providers = list(providers or [])
        hint = (
            f"registered agent providers: {', '.join(self.providers)}"
            if self.providers else "no AgentCapabilityProvider registered"
        )
        super().__init__(
            f"Template {template_ref!r} not found ({hint}); template refs must be "
            f"canonical 'provider:name', e.g. 'agent:planner'"
        )
```

- [ ] **Step 4: 实现 TemplateLookup**——新建 `src/ctx_weft/core/orchestrator/template_lookup.py`：

```python
"""TemplateLookup：qualified 名反查 + cap.id 前缀精确路由的模板加载（内部组件，非协议）。

发现与加载同源（spec 2026-07-22）：模板进入 core 的唯一通道是 AgentCapabilityProvider。
- resolve_qualified：agent__planner → 完整 cap.id（'agent:planner'），保留 provider 归属；
- get_template：按 cap.id 前缀（rsplit(':', 1)，与装配层 _provider_meta 同口径）路由到
  唯一 provider；裸 id（无可路由前缀）直接 TemplateNotFoundError——边界强制规范 id，
  core 不做注册序扫描回落。
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from ctx_weft.core.errors import TemplateNotFoundError
from ctx_weft.protocols.capability import (
    AgentCapability, AgentCapabilityProvider, qualify,
)

if TYPE_CHECKING:
    from ctx_weft.core.runtime import ProviderRegistry
    from ctx_weft.protocols.context import ProviderContext
    from ctx_weft.protocols.template import AgentTemplate

logger = logging.getLogger(__name__)


class TemplateLookup:
    def __init__(self, providers: "ProviderRegistry") -> None:
        self._providers = providers

    def _agent_providers(self) -> list[AgentCapabilityProvider]:
        return [p for p in self._providers.get_capability_providers()
                if isinstance(p, AgentCapabilityProvider)]

    async def resolve_qualified(self, qualified: str, ctx: "ProviderContext") -> str:
        """qualified 工具名（agent__planner）→ 规范 cap.id（agent:planner）。

        未命中原样返回：字面值交给 get_template 判定（裸 id 在那里报错）。
        单 provider list() 失败 → log + 跳过（与装配路径吞异常口径一致）。"""
        for p in self._agent_providers():
            try:
                caps = await p.list(ctx)
            except Exception:
                logger.exception("TemplateLookup: provider %r list() failed", p.name)
                continue
            for cap in caps:
                if isinstance(cap, AgentCapability) and qualify(cap.id) == qualified:
                    return cap.id
        return qualified

    async def get_template(
        self, ref: str, version: str | None, ctx: "ProviderContext",
    ) -> "AgentTemplate":
        """规范 id（provider:name）前缀精确路由加载。

        裸 id / 未知前缀 / 路由到的 provider 返回 None → TemplateNotFoundError
        （路由已确定，不问其他 provider）。provider 真实故障原样传播。"""
        providers = self._agent_providers()
        names = [p.name for p in providers]
        if ":" in ref:
            provider_name, local_name = ref.rsplit(":", 1)
            for p in providers:
                if p.name == provider_name:
                    template = await p.get_template(local_name, version, ctx)
                    if template is None:
                        raise TemplateNotFoundError(ref, providers=names)
                    return template
        raise TemplateNotFoundError(ref, providers=names)
```

- [ ] **Step 5: 跑测试确认通过**

Run: `uv run pytest tests/unit/test_template_lookup.py -q`
Expected: PASS（11 个用例全绿）。

- [ ] **Step 6: 提交**

```bash
git add src/ctx_weft/core/errors.py src/ctx_weft/core/orchestrator/template_lookup.py tests/unit/test_template_lookup.py
git commit -m "feat(orchestrator): TemplateLookup 前缀精确路由 + TemplateNotFoundError"
```

---

### Task 3: runtime/lifecycle/driver 重接线 + 测试基建迁移（ctx-weft，原子破坏点）

**Files:**
- Modify: `src/ctx_weft/core/runtime.py`（构造 435-505、run_single_task 686、start_session 765/808、recover 1157-1158、relaunch 1255、compact 1371、driver 传参 1729、`_resolve_subagent_template` 544-559、assemble 调用点 1970）
- Modify: `src/ctx_weft/core/orchestrator/lifecycle_manager.py`
- Modify: `src/ctx_weft/core/loop/driver.py:132`（删死字段）
- Modify: `tests/integration/test_minimal_loop.py`（加 `make_runtime` 助手）
- Modify: 34 个测试文件的 `CtxWeftRuntime(` 调用点（清单见 Step 5）+ `template_id` 前缀清扫
- Delete: `tests/unit/test_subagent_template_resolution.py`（被 Task 2 的 test_template_lookup 取代）
- Modify: `README.md`（构造示例段）

**Interfaces:**
- Consumes: Task 2 的 `TemplateLookup` / `TemplateNotFoundError`；Task 1 的适配器
- Produces:
  - `CtxWeftRuntime(providers=..., llm=..., hitl_manager=..., event_store=..., config=...)`——**无** `template_resolver` 参数；registry 无 `AgentCapabilityProvider` → `ValueError`
  - `runtime._template_lookup: TemplateLookup`（内部属性，测试可用）
  - `LifecycleManager(template_lookup: TemplateLookup)`
  - 测试助手 `make_runtime(template_resolver=..., **kwargs)`（`tests/integration/test_minimal_loop.py`）

- [ ] **Step 1: 写失败测试**——`tests/unit/test_runtime_public_api.py` 追加（该文件现有 4 处 `template_resolver` 引用在 Step 4 一并改）：

```python
def test_runtime_requires_agent_capability_provider() -> None:
    """构造期硬校验：registry 无 AgentCapabilityProvider → ValueError（fail-fast）。"""
    import pytest
    from ctx_weft.core.runtime import CtxWeftRuntime, ProviderRegistry
    with pytest.raises(ValueError, match="AgentCapabilityProvider"):
        CtxWeftRuntime(providers=ProviderRegistry())
```

Run: `uv run pytest tests/unit/test_runtime_public_api.py::test_runtime_requires_agent_capability_provider -q`
Expected: FAIL（当前构造签名要求 template_resolver 位置参数，TypeError 而非 ValueError）。

- [ ] **Step 2: 源码重接线**——逐处修改：

2a. `src/ctx_weft/core/runtime.py` 构造函数（435-448）：删首参 `template_resolver: TemplateResolver` 与 `self._template_resolver = template_resolver`；在**自动注册 Control/SkillExecutor provider 之后**（原 473 行位置），把 `TemplateAgentCapabilityProvider` 自动注册两行**删除**，替换为：

```python
        # 模板通道硬校验（spec 2026-07-22）：模板进入 core 的唯一通道是
        # AgentCapabilityProvider；缺失则 root agent 都无法实例化，构造即失败。
        agent_provider_names = [
            p.name for p in self.providers.get_capability_providers()
            if isinstance(p, AgentCapabilityProvider)
        ]
        if not agent_provider_names:
            raise ValueError(
                "CtxWeftRuntime requires at least one AgentCapabilityProvider in the "
                "ProviderRegistry — register one before constructing, e.g. "
                "providers.register_capability(TemplateAgentCapabilityProvider(resolver))"
            )
        from ctx_weft.core.orchestrator.template_lookup import TemplateLookup
        self._template_lookup = TemplateLookup(self.providers)
```

2b. 删除 `template_resolver` 公开属性（502-504）。

2c. 删除 `_resolve_subagent_template` 方法全体（544-559）；其唯一调用点（1970，`_SessionTaskRunner.assemble`）改为：

```python
                sub_tmpl_id = (
                    await self._runtime._template_lookup.resolve_qualified(s.subagent_template, ctx)
                    if s.subagent_template else ""
                ) or self._template_id
```

2d. 5 处 `LifecycleManager(template_resolver=self._template_resolver)`（686、765、1157、1255、1371）→ `LifecycleManager(template_lookup=self._template_lookup)`。

2e. 2 处直接加载（808、1158）`await self._template_resolver.get(X, version=None, ctx=...)` → `await self._template_lookup.get_template(X, None, ctx=...)`（X 分别为 `params.template_id` / `template_id`，实参不变）。

2f. 1729 行传给 loop deps 的 `template_resolver=self._template_resolver,` 一行删除；`src/ctx_weft/core/loop/driver.py:131-132` 的 `# 模板解析器（Phase 4）` 注释与 `template_resolver: TemplateResolver|None = None` 字段删除（从未被读），并清掉 driver.py 顶部因此不再需要的 `TemplateResolver` import。

2g. runtime.py 顶部 import 清理：`TemplateResolver` 若仅存于已删代码则从 import 列表移除（`AgentTemplate` 等保留）。

2h. `src/ctx_weft/core/orchestrator/lifecycle_manager.py`：字段与加载改为：

```python
    template_lookup: "TemplateLookup"
```

`instantiate_agent` 内（50-52）：

```python
        template: AgentTemplate = await self.template_lookup.get_template(
            template_id, None, ctx=resolve_ctx,
        )
```

文件头 import 改为 `from ctx_weft.core.orchestrator.template_lookup import TemplateLookup`（TYPE_CHECKING 或直接 import 均可，无循环），删除不再用的 `TemplateResolver` import。docstring 补一句：「template_id 须为规范形式 provider:name；裸 id 由 TemplateLookup 抛 TemplateNotFoundError」。

- [ ] **Step 3: 加测试助手**——`tests/integration/test_minimal_loop.py` 在 `InMemoryTemplateResolver` 类定义之后加：

```python
def make_runtime(**kwargs) -> CtxWeftRuntime:
    """测试构造入口：把 template_resolver 参数包装成 TemplateAgentCapabilityProvider 注册。

    协议改造（spec 2026-07-22）后 CtxWeftRuntime 不再收 template_resolver——
    存量测试经本助手做最小迁移：实参形状与旧构造完全一致。
    """
    from ctx_weft.core.orchestrator.agent_capability import TemplateAgentCapabilityProvider
    from ctx_weft.core.runtime import ProviderRegistry
    resolver = kwargs.pop("template_resolver")
    providers = kwargs.pop("providers", None) or ProviderRegistry()
    providers.register_capability(TemplateAgentCapabilityProvider(resolver))
    return CtxWeftRuntime(providers=providers, **kwargs)
```

本文件自身的 2 处 `CtxWeftRuntime(llm=llm, template_resolver=resolver)` 也改为 `make_runtime(llm=llm, template_resolver=resolver)`。

- [ ] **Step 4: 机械清扫（源清单驱动，两条规则）**——

**规则 A（构造替换）**：`Grep pattern="CtxWeftRuntime\(" path=tests` 列出的每一处（34 文件 52 处，含多行调用），改为 `make_runtime(`，实参原样保留（`template_resolver=`、`llm=`、`config=`、`providers=` 均被助手兼容）。import 行跟随现有风格：这些文件已多数有 `from tests.integration.test_minimal_loop import InMemoryTemplateResolver`，在同一行追加 `make_runtime`；无该 import 的文件新加 `from tests.integration.test_minimal_loop import make_runtime`。

**规则 B（模板 id 规范化）**：凡把模板 id 送进加载路径的实参加 `agent:` 前缀——具体模式：`run_single_task(... template_id="X")`、`SessionStartParams.create(template_id="X", ...)`、`SessionStartParams(template_id="X", ...)`、`instantiate_agent(template_id="X", ...)` → `"agent:X"`。**注意**：`Agent(template_id=...)`/投影/断言里的字段值是元数据不改；`resolver.register(template)` 注册的模板自身 `id` 保持裸名。用 `Grep pattern="template_id=\"" path=tests -n` 逐处判定归属。

特殊点（不适用规则 A/B 的手工处）：
- `tests/integration/test_compact_flow_e2e.py:113` 与 `tests/unit/test_session_event_order.py:52` 直接构造 `LifecycleManager(template_resolver=...)` → 改为：

```python
from ctx_weft.core.orchestrator.agent_capability import TemplateAgentCapabilityProvider
from ctx_weft.core.orchestrator.template_lookup import TemplateLookup
from ctx_weft.core.runtime import ProviderRegistry

_reg = ProviderRegistry()
_reg.register_capability(TemplateAgentCapabilityProvider(resolver))
lm = LifecycleManager(template_lookup=TemplateLookup(_reg))
```

（`resolver` 为各测试原有实例；其后 `instantiate_agent(template_id=...)` 按规则 B 加前缀。）
- `tests/unit/test_resume_unfinished_guard.py:36` 的 `LifecycleManager(template_resolver=runtime.template_resolver)` → `LifecycleManager(template_lookup=runtime._template_lookup)`。
- `tests/unit/test_runtime_public_api.py` 现有 4 处 `template_resolver` 引用：构造处走规则 A；若有对 `runtime.template_resolver` 属性的断言，删除该断言（属性已死），保留/新增 Step 1 的 ValueError 用例。
- **删除** `tests/unit/test_subagent_template_resolution.py`（`_resolve_subagent_template` 已亡；等价覆盖在 `test_template_lookup.py`）。

- [ ] **Step 5: 全量测试**

Run: `uv run pytest -q`
Expected: 全绿。失败逐个修（常见残留：多行构造没换名、某处 template_id 漏加前缀导致 `TemplateNotFoundError`——错误信息自带提示）。

- [ ] **Step 6: README 同步**——`Grep pattern="template_resolver" path=README.md`，把构造示例改为先注册 provider 再构造（与 Step 3 助手同形），并把「AgentCapabilityProvider 是 list 型 provider」相关表述改为「发现与加载同源」。

- [ ] **Step 7: 提交**

```bash
git add -A
git commit -m "feat(runtime)!: 模板加载统一走 AgentCapabilityProvider——删 template_resolver 构造参数与单例路径"
```

---

### Task 4: host 接线 + 边界规范化（IpMasterCoworkPy）

**前置：** 在 IpMasterCoworkPy 仓根跑 `powershell -File scripts\revendor-core.ps1`（重新打包 vendored wheel 并 uv sync），确认 `uv run python -c "from ctx_weft.core.orchestrator.template_lookup import TemplateLookup"` 成功。

**Files:**
- Modify: `src/ipmastercowork/providers/templates/__init__.py`（加 `canonical_template_id`）
- Modify: `src/ipmastercowork/cli.py`（build_runtime 134、返回值 169、cmd_serve 207-209、cmd_run 217/249）
- Modify: `src/ipmastercowork/api/main.py`（create_app 签名 + 26 行）
- Modify: `src/ipmastercowork/api/sessions.py`（197、511 两处 SessionStartParams.create）
- Test: `tests/test_build_runtime_wiring.py`（新建；如已有 build_runtime 相关测试文件如 `tests/test_build_runtime_bash_authorizer.py` 因返回值改二→三元组需同步改 unpack）

**Interfaces:**
- Consumes: ctx-weft 的 `TemplateAgentCapabilityProvider`、`PROVIDER_NAME`（`ctx_weft.core.orchestrator.agent_capability`）、新构造契约
- Produces:
  - `canonical_template_id(template_id: str) -> str`（`ipmastercowork.providers.templates`）
  - `build_runtime(args) -> (runtime, syncer, resolver)`（三元组，原二元组）
  - `create_app(runtime, template_syncer, template_registry, ...)`

- [ ] **Step 1: 写失败测试**——新建 `tests/test_build_runtime_wiring.py`：

```python
"""build_runtime 接线：注册 agent 适配器 provider + 边界规范化助手（spec 2026-07-22）。"""

from __future__ import annotations

from types import SimpleNamespace

from ctx_weft.core.orchestrator.agent_capability import TemplateAgentCapabilityProvider
from ipmastercowork.cli import build_runtime
from ipmastercowork.providers.templates import canonical_template_id


def _args():
    return SimpleNamespace(enable_tools=False, skills_dir=None)


def test_build_runtime_registers_agent_provider_and_returns_resolver():
    runtime, syncer, resolver = build_runtime(_args())
    provs = [p for p in runtime.providers.get_capability_providers()
             if isinstance(p, TemplateAgentCapabilityProvider)]
    assert len(provs) == 1
    assert resolver is not None  # create_app 经此喂 deps.set_template_registry


def test_canonical_template_id():
    assert canonical_template_id("default") == "agent:default"
    assert canonical_template_id("agent:default") == "agent:default"      # 幂等
    assert canonical_template_id("mcp:reg:x") == "mcp:reg:x"              # 已带前缀不动
```

Run: `uv run pytest tests/test_build_runtime_wiring.py -q`
Expected: FAIL——`ImportError: canonical_template_id` / unpack 数量错误。

- [ ] **Step 2: 实现**——

2a. `src/ipmastercowork/providers/templates/__init__.py` 追加：

```python
from ctx_weft.core.orchestrator.agent_capability import PROVIDER_NAME as AGENT_PROVIDER_NAME


def canonical_template_id(template_id: str) -> str:
    """ctx-weft 边界规范化：裸模板 id → 'agent:<id>'（spec 2026-07-22 边界强制前缀）。

    已含 ':'（任意 provider 前缀）原样返回。host 内部（模板管理 API / 前端 / DB 行）
    继续用裸 id——转换只发生在把 id 交给 ctx-weft 的边界。"""
    return template_id if ":" in template_id else f"{AGENT_PROVIDER_NAME}:{template_id}"
```

2b. `cli.py` `build_runtime()`：resolver 构造（81-83）之后、`providers = ProviderRegistry()`（85）之后插入注册；134 行删 `template_resolver=` 参数；169 行返回三元组：

```python
    from ctx_weft.core.orchestrator.agent_capability import TemplateAgentCapabilityProvider
    providers.register_capability(TemplateAgentCapabilityProvider(resolver))
    ...
    runtime = CtxWeftRuntime(providers=providers, config=cfg.to_runtime_config())
    ...
    return runtime, syncer, resolver
```

2c. `cli.py` 调用方：`cmd_serve` 207 → `runtime, syncer, resolver = build_runtime(args)`，209 的 `create_app(...)` 加 `template_registry=resolver`；`cmd_run` 217 → `runtime, _, _ = build_runtime(args)`，249 → `template_id=canonical_template_id(args.template)`（文件头补 `from ipmastercowork.providers.templates import canonical_template_id`）。

2d. `api/main.py` `create_app`：签名 `template_syncer` 后加 `template_registry: Any = None,`；26 行改 `deps.set_template_registry(template_registry)`。

2e. `api/sessions.py`：两处 `SessionStartParams.create(` 的 `template_id=` 实参包 `canonical_template_id(...)`（197 行 `template_id=canonical_template_id(template_id)`；511 行 `template_id=canonical_template_id(entry.template_id)`——后者兜住升级前的存量 host 会话行）。文件头补 import。`SessionEntry(template_id=template_id)`（218）**保持裸 id 不包**——host 记录归 host。

- [ ] **Step 3: 跑新测试 + 受影响存量测试**

Run: `uv run pytest tests/test_build_runtime_wiring.py tests/test_build_runtime_bash_authorizer.py -q`
Expected: PASS（后者若因 unpack 破坏先修 unpack）。

- [ ] **Step 4: host 全量测试**

Run: `uv run pytest -q`
Expected: 全绿（留意任何直接调 `runtime.template_resolver` / 传裸 template_id 进 start_session 的测试，按 2e 同法修）。

- [ ] **Step 5: 提交**

```bash
git add -A
git commit -m "feat(host)!: 显式注册 agent 适配器 provider + ctx-weft 边界模板 id 规范化"
```

---

### Task 5: m010 存量事件/快照 template_id 规范化迁移（IpMasterCoworkPy）

**Files:**
- Modify: `src/ipmastercowork/persistence/postgres/migrations.py`（m009 之后加 `_m010_canonical_template_id_prefix`，`MIGRATIONS` 列表追加）
- Test: `tests/test_migrations.py`（追加用例）

**Interfaces:**
- Consumes: `EventModel.payload_json`（Text，JSON 字符串）；`SnapshotModel.state_blob_json`（serialize_view 输出，`{"sessions": {sid: {"template_id": ...}}}`，reducers.py:156-174）；`run_pending(factory, dry_run)` 机制（migrations.py:580）
- Produces: 迁移 id `m010_canonical_template_id_prefix`，返回改写行数（events 行 + snapshots 行合计）

- [ ] **Step 1: 写失败测试**——`tests/test_migrations.py` 追加：

```python
import json as _json

from ipmastercowork.persistence.postgres.models import SnapshotModel


async def _seed_template_id_rows(factory) -> None:
    async with factory() as db:
        async with db.begin():
            await db.merge(SessionModel(id="ses_m10", user_prompt="seed"))
            db.add(EventModel(
                id="evt_m10_bare", session_id="ses_m10", type="SESSION_CREATED",
                sequence=1,
                payload_json=_json.dumps({"template_id": "default", "user_prompt": "hi"}),
            ))
            db.add(EventModel(
                id="evt_m10_canon", session_id="ses_m10", type="SESSION_CREATED",
                sequence=2,
                payload_json=_json.dumps({"template_id": "agent:default"}),
            ))
            db.add(EventModel(
                id="evt_m10_none", session_id="ses_m10", type="TASK_CREATED",
                sequence=3, payload_json=_json.dumps({"title": "no tid"}),
            ))
            db.add(SnapshotModel(
                id="snp_m10", session_id="ses_m10", last_event_id="evt_m10_bare",
                last_event_sequence=1,
                state_blob_json=_json.dumps({"sessions": {
                    "ses_m10": {"id": "ses_m10", "template_id": "default"},
                    "ses_ok": {"id": "ses_ok", "template_id": "agent:default"},
                }}),
            ))


async def _payload(factory, event_id: str) -> dict:
    async with factory() as db:
        row = (await db.execute(
            select(EventModel.payload_json).where(EventModel.id == event_id))).scalar_one()
    return _json.loads(row)


async def test_m010_prefixes_bare_template_ids(tmp_path):
    factory = await _factory(tmp_path)
    await _seed_template_id_rows(factory)

    applied = await run_pending(factory)
    # 1 条裸事件 + 1 条含裸 id 的快照 = 2 行（已规范化的不计）
    assert applied["m010_canonical_template_id_prefix"] == 2

    assert (await _payload(factory, "evt_m10_bare"))["template_id"] == "agent:default"
    assert (await _payload(factory, "evt_m10_bare"))["user_prompt"] == "hi"  # 其余键不动
    assert (await _payload(factory, "evt_m10_canon"))["template_id"] == "agent:default"
    assert "template_id" not in (await _payload(factory, "evt_m10_none"))

    async with factory() as db:
        blob = (await db.execute(
            select(SnapshotModel.state_blob_json).where(SnapshotModel.id == "snp_m10"))).scalar_one()
    sessions = _json.loads(blob)["sessions"]
    assert sessions["ses_m10"]["template_id"] == "agent:default"
    assert sessions["ses_ok"]["template_id"] == "agent:default"


async def test_m010_idempotent_rerun_zero(tmp_path):
    factory = await _factory(tmp_path)
    await _seed_template_id_rows(factory)
    await run_pending(factory)
    # 抹掉标记强制重跑，验证第二次命中 0 行
    async with factory() as db:
        await db.execute(text("DELETE FROM applied_migrations WHERE id = 'm010_canonical_template_id_prefix'"))
        await db.commit()
    applied = await run_pending(factory)
    assert applied["m010_canonical_template_id_prefix"] == 0
```

Run: `uv run pytest tests/test_migrations.py -q -k m010`
Expected: FAIL——`KeyError: 'm010_canonical_template_id_prefix'`。

- [ ] **Step 2: 实现迁移**——`migrations.py` 在 `_m009_*` 之后追加，并在 `MIGRATIONS` 列表末尾登记 `("m010_canonical_template_id_prefix", _m010_canonical_template_id_prefix),`：

```python
def _canonical_tid(tid: str) -> str:
    return tid if ":" in tid else f"agent:{tid}"


async def _m010_canonical_template_id_prefix(db: AsyncSession) -> int:
    """模板加载改 cap.id 前缀精确路由（spec 2026-07-22）后，裸 template_id 一律
    TemplateNotFoundError。resume / 冷 HITL / 手动 compact 的 template_id 来自事件重放
    （SessionCreated 载荷）与快照（serialize_view 的 sessions.*.template_id），存量数据
    不迁移则旧会话全部不可恢复。

    改写两处：events.payload_json 顶层 "template_id" 键（防御性：不限事件类型）；
    snapshots.state_blob_json 的 sessions.*.template_id。
    幂等：已含 ':' 的 id 跳过，重跑命中 0 行。返回改写行数（events + snapshots）。
    """
    changed = 0

    rows = (await db.execute(
        select(EventModel.id, EventModel.payload_json)
        .where(EventModel.payload_json.like('%"template_id"%'))
    )).all()
    for eid, payload_json in rows:
        try:
            payload = json.loads(payload_json or "{}")
        except ValueError:
            continue
        tid = payload.get("template_id")
        if not isinstance(tid, str) or not tid or ":" in tid:
            continue
        payload["template_id"] = _canonical_tid(tid)
        await db.execute(
            update(EventModel).where(EventModel.id == eid)
            .values(payload_json=json.dumps(payload, ensure_ascii=False)))
        changed += 1

    from ipmastercowork.persistence.postgres.models import SnapshotModel
    srows = (await db.execute(
        select(SnapshotModel.id, SnapshotModel.state_blob_json)
        .where(SnapshotModel.state_blob_json.like('%"template_id"%'))
    )).all()
    for sid, blob in srows:
        try:
            state = json.loads(blob or "{}")
        except ValueError:
            continue
        dirty = False
        for sess in (state.get("sessions") or {}).values():
            tid = sess.get("template_id")
            if isinstance(tid, str) and tid and ":" not in tid:
                sess["template_id"] = _canonical_tid(tid)
                dirty = True
        if dirty:
            await db.execute(
                update(SnapshotModel).where(SnapshotModel.id == sid)
                .values(state_blob_json=json.dumps(state, ensure_ascii=False)))
            changed += 1

    return changed
```

- [ ] **Step 3: 跑迁移测试**

Run: `uv run pytest tests/test_migrations.py -q`
Expected: PASS（含存量 m001-m009 用例回归）。

- [ ] **Step 4: host 全量回归**

Run: `uv run pytest -q`
Expected: 全绿。

- [ ] **Step 5: 提交**

```bash
git add src/ipmastercowork/persistence/postgres/migrations.py tests/test_migrations.py
git commit -m "feat(migrations): m010 存量事件/快照裸 template_id 规范化为 agent: 前缀"
```
