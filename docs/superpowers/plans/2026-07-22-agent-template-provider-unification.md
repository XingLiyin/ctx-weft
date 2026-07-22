# 方案 B：模板 provider 归一 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 删除 `TemplateResolver` 协议/`AgentTemplateSummary`/`TemplateAgentCapabilityProvider` 适配器；core ship 单根目录扫描版 `LocalAgentTemplateProvider`（loader 上移）；host 直接实现 `DirAgentCapabilityProvider` 并把管理面重定向到 store/provider。

**Architecture:** 依据 spec `docs/superpowers/specs/2026-07-22-agent-template-provider-unification-design.md`。模板进入 core 的唯一形态是 `AgentCapabilityProvider` 实现；模板目录格式（SOUL.md/ROLE.md）解析成为 core 资产（`ctx_weft/providers/agent_template_local/`，对标 `capability_skill_local`）；host 保留 TemplateStore/Syncer 管理面与任意路径注册，provider 层直接实现协议并复用 core loader。

**Tech Stack:** Python 3.11+，两仓均 `uv`。ctx-weft = `C:\Users\Xing\Documents\codes\Loome-02\ctx-weft`；host = `C:\Users\Xing\Documents\codes\IpMasterCoworkPy`（经 vendored wheel 消费引擎）。

## Global Constraints

- **`PROVIDER_NAME = "agent"` 是数据契约**（m010 存量数据 + host canonical 边界钉死）——不得改名，**无新 DB 迁移**。
- **运行时零可观测差异**：`TemplateLookup` 路由、构造校验（registry 须含 ≥1 `AgentCapabilityProvider` 否则 `ValueError`）、`TemplateNotFoundError`、get_template 的 None/异常契约、`version=None`、`rsplit(":", 1)` 全部不动。
- 破坏性变更两仓 lockstep，不留兼容垫片；core src **不 ship in-memory provider**（测试私有桩允许，仅存在于 tests/）。
- 分支：延续两仓现有 `feat/agent-capability-template-protocol` 分支（本 wave 基于方案 A 成果）。
- 测试命令：两仓均 `uv run pytest <path> -q`（仓根）。测试基线：ctx-weft 3 个存量环境性失败（compact_flow_e2e multiround / golden_conformance / observe_outcomes）、host 1 个（test_skills_reference）——验收=零新增。
- 提交纪律：提交前 `git status --short` 全检、**逐文件点名 add**、禁止 `git add -A` / `git add .`；信息结尾 `Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>`。
- Task 3（host）前必须先在 host 仓根 `powershell -File scripts\revendor-core.ps1` 重新 vendor 引擎 wheel。

---

### Task 1: core 新包 `agent_template_local`（loader 上移 + 目录版 provider）

**Files:**
- Create: `src/ctx_weft/providers/agent_template_local/__init__.py`
- Create: `src/ctx_weft/providers/agent_template_local/_loader.py`
- Create: `src/ctx_weft/providers/agent_template_local/provider.py`
- Test: `tests/unit/test_agent_template_local.py`（新建）

**Interfaces:**
- Consumes: `AgentCapabilityProvider`（get_template 抽象 + retrieve 默认 []）、`AgentCapability`、`CapabilityProviderInfo`（protocols/capability.py）；`AgentTemplate`/`CapabilityRef`/`IdentityFacet`/`LoopConfig`/`MemoryConfig`（protocols/template.py）
- Produces（Task 2/3 依赖）:
  - `ctx_weft.providers.agent_template_local.PROVIDER_NAME = "agent"`
  - `TemplateLoader`：`.load(template_dir: Path) -> AgentTemplate`、`.scan(root: Path) -> list[tuple[Path, AgentTemplate]]`
  - `merge_default_facets(template, default, purposes) -> None`、`DEFAULT_MERGE_PURPOSES = ("compact", "recognize_intent", "observe")`
  - `LocalAgentTemplateProvider(templates_root: Path, default_template_id="default", loader=None)`

- [ ] **Step 1: 写失败测试**——新建 `tests/unit/test_agent_template_local.py`：

```python
"""LocalAgentTemplateProvider：单根目录扫描 + 默认 facet 合并 + subagents refs（spec 方案 B）。"""

from __future__ import annotations

from pathlib import Path

from ctx_weft.protocols.capability import AgentCapability
from ctx_weft.protocols.context import ProviderContext
from ctx_weft.providers.agent_template_local import (
    PROVIDER_NAME, LocalAgentTemplateProvider, TemplateLoader,
)

_CTX = ProviderContext(session_id="s1", tenant_id="default")


def _make_dir(root: Path, name: str, *, extra_fm: str = "", compact: str | None = None,
              metadata: str | None = None) -> Path:
    d = root / name
    d.mkdir(parents=True)
    (d / "SOUL.md").write_text(
        f"---\nname: {name}\nversion: 1.0.0\ndescription: {name} agent\n{extra_fm}---\n{name} soul",
        encoding="utf-8",
    )
    if compact is not None:
        (d / "COMPACT.md").write_text(compact, encoding="utf-8")
    if metadata is not None:
        (d / "METADATA.md").write_text(metadata, encoding="utf-8")
    return d


async def test_list_scans_root_and_shapes_capabilities(tmp_path: Path) -> None:
    _make_dir(tmp_path, "default")
    _make_dir(tmp_path, "planner")
    (tmp_path / "not_a_template").mkdir()  # 无 SOUL.md → 忽略
    prov = LocalAgentTemplateProvider(tmp_path)
    caps = await prov.list(_CTX)
    assert {c.id for c in caps} == {"agent:default", "agent:planner"}
    cap = next(c for c in caps if c.id == "agent:planner")
    assert isinstance(cap, AgentCapability)
    assert cap.template_name == "planner"
    assert cap.version == "1.0.0"
    assert cap.description == "planner agent"
    assert PROVIDER_NAME == "agent"


async def test_get_template_miss_returns_none(tmp_path: Path) -> None:
    _make_dir(tmp_path, "default")
    prov = LocalAgentTemplateProvider(tmp_path)
    assert await prov.get_template("nope", None, _CTX) is None


async def test_default_facet_merge(tmp_path: Path) -> None:
    _make_dir(tmp_path, "default", compact="DEF-COMPACT", metadata="DEF-MD")
    _make_dir(tmp_path, "foo")
    prov = LocalAgentTemplateProvider(tmp_path)
    t = await prov.get_template("foo", None, _CTX)
    assert t.identity["compact"].text == "DEF-COMPACT"
    assert t.identity["recognize_intent"].text == "DEF-MD"
    assert t.identity["act"].text == "foo soul"  # 自己的 facet 不被覆盖


async def test_default_itself_not_self_merged(tmp_path: Path) -> None:
    _make_dir(tmp_path, "default", compact="DEF-COMPACT")
    prov = LocalAgentTemplateProvider(tmp_path)
    t = await prov.get_template("default", None, _CTX)
    assert t.identity["compact"].text == "DEF-COMPACT"


async def test_retrieve_defaults_empty(tmp_path: Path) -> None:
    _make_dir(tmp_path, "default")
    prov = LocalAgentTemplateProvider(tmp_path)
    assert await prov.retrieve(_CTX) == []


# ── loader：subagents frontmatter → 原样 capability_id（迁自 host test_loader_subagents）──

def _refs(tmp_path: Path, subagents_block: str) -> set[tuple[str, str]]:
    d = _make_dir(tmp_path, "default", extra_fm=subagents_block)
    template = TemplateLoader().load(d)
    return {(r.capability_id, r.mode) for r in template.capability_refs}


def test_subagents_used_as_exact_capability_id(tmp_path: Path) -> None:
    refs = _refs(tmp_path, "subagents:\n  - agent:planner\n")
    assert ("agent:planner", "required") in refs
    assert ("agent:agent:planner", "required") not in refs  # 无前缀补全


def test_bare_subagent_value_is_not_auto_prefixed(tmp_path: Path) -> None:
    refs = _refs(tmp_path, "subagents:\n  - planner\n")
    assert ("planner", "required") in refs
    assert ("agent:planner", "required") not in refs
```

- [ ] **Step 2: 跑测试确认失败**

Run: `uv run pytest tests/unit/test_agent_template_local.py -q`
Expected: FAIL——`ModuleNotFoundError: ctx_weft.providers.agent_template_local`。

- [ ] **Step 3: 实现 `_loader.py`**——把 host 仓 `C:\Users\Xing\Documents\codes\IpMasterCoworkPy\src\ipmastercowork\providers\templates\loader.py` 的**全部内容原样拷贝**为 `src/ctx_weft/providers/agent_template_local/_loader.py`（该文件只 import `ctx_weft.protocols.template`，无 host 依赖，零改动即可用；模块 docstring 首行改为 `"""TemplateLoader — 目录 → AgentTemplate 解析（core 资产，spec 2026-07-22 方案 B 自 host 上移）。`，目录结构示例等其余 docstring 保留），然后在文件末尾追加（迁自 host `resolver.py` 的 `DEFAULT_MERGE_PURPOSES` 与 `merge_default_facets`）：

```python
# ── 默认 facet 合并（自 host resolver.py 上移）───────────────────────────────

DEFAULT_MERGE_PURPOSES = ("compact", "recognize_intent", "observe")


def merge_default_facets(template: AgentTemplate, default: AgentTemplate, purposes) -> None:
    """Fill the template's missing identity facets (in-place) from the default template."""
    for purpose in purposes:
        if purpose not in template.identity and purpose in default.identity:
            template.identity[purpose] = default.identity[purpose]
```

- [ ] **Step 4: 实现 `provider.py`**：

```python
"""LocalAgentTemplateProvider：单根目录扫描版 agent template provider（core 内置参考实现）。

根目录下每个含 SOUL.md 的子目录即一个模板；list()/get_template() 每次重新扫盘，
热更新友好（与 capability_skill_local 同族）。非 default 模板缺
compact/recognize_intent/observe facet 时从 default 模板补齐（merge_default_facets）。
"""

from __future__ import annotations

import logging
from pathlib import Path

from ctx_weft.protocols.capability import (
    AgentCapability,
    AgentCapabilityProvider,
    Capability,
    CapabilityProviderInfo,
)
from ctx_weft.protocols.context import ProviderContext
from ctx_weft.protocols.template import AgentTemplate
from ctx_weft.providers.agent_template_local._loader import (
    DEFAULT_MERGE_PURPOSES,
    TemplateLoader,
    merge_default_facets,
)

logger = logging.getLogger(__name__)

# ⚠ 数据契约：存量事件/快照（host m010 迁移）与 host canonical 边界均以 "agent:" 为
# 前缀——此常量不可改名（spec 2026-07-22 方案 B「不变量」）。
PROVIDER_NAME = "agent"


class LocalAgentTemplateProvider(AgentCapabilityProvider):
    name = PROVIDER_NAME

    def __init__(
        self,
        templates_root: Path,
        default_template_id: str = "default",
        loader: TemplateLoader | None = None,
    ) -> None:
        self._root = Path(templates_root)
        self._default_template_id = default_template_id
        self._loader = loader or TemplateLoader()

    async def list(self, ctx: ProviderContext) -> list[Capability]:
        return [
            AgentCapability(
                id=f"{PROVIDER_NAME}:{t.id}",
                name=t.id,
                template_name=t.id,
                description=t.description,
                version=t.version,
            )
            for _, t in self._loader.scan(self._root)
        ]

    async def get_template(
        self, template_id: str, version: str | None, ctx: ProviderContext,
    ) -> AgentTemplate | None:
        found: AgentTemplate | None = None
        default: AgentTemplate | None = None
        for _, t in self._loader.scan(self._root):
            if t.id == template_id:
                found = t
            if t.id == self._default_template_id:
                default = t
        if found is None:
            return None
        if found.id != self._default_template_id and default is not None:
            merge_default_facets(found, default, DEFAULT_MERGE_PURPOSES)
        return found

    async def describe(self, ctx: ProviderContext) -> CapabilityProviderInfo:
        return CapabilityProviderInfo(
            name=self.name,
            capability_count=len(self._loader.scan(self._root)),
            supports_streaming=False,
            supports_cancel=False,
            description=self.description,
        )
```

- [ ] **Step 5: `__init__.py`**：

```python
"""agent_template_local：单根目录扫描版 agent template provider + 模板目录格式 loader。"""

from ctx_weft.providers.agent_template_local._loader import (
    DEFAULT_MERGE_PURPOSES,
    TemplateLoader,
    merge_default_facets,
)
from ctx_weft.providers.agent_template_local.provider import (
    PROVIDER_NAME,
    LocalAgentTemplateProvider,
)

__all__ = [
    "DEFAULT_MERGE_PURPOSES",
    "PROVIDER_NAME",
    "LocalAgentTemplateProvider",
    "TemplateLoader",
    "merge_default_facets",
]
```

- [ ] **Step 6: 跑测试确认通过**

Run: `uv run pytest tests/unit/test_agent_template_local.py -q`
Expected: PASS（7 个用例全绿）。

- [ ] **Step 7: 提交**

```bash
git add src/ctx_weft/providers/agent_template_local/__init__.py src/ctx_weft/providers/agent_template_local/_loader.py src/ctx_weft/providers/agent_template_local/provider.py tests/unit/test_agent_template_local.py
git commit -m "feat(providers): agent_template_local——loader 上移 core + 单根目录扫描版 provider"
```

---

### Task 2: ctx-weft 删除面 + 测试基建迁移（原子破坏点）

**Files:**
- Modify: `src/ctx_weft/protocols/template.py`（删 `AgentTemplateSummary` ~125-133、`TemplateResolver` ~135-158；模块 docstring 去 resolver 表述）
- Modify: `src/ctx_weft/protocols/__init__.py`（删 `AgentTemplateSummary`、`TemplateResolver` 两处 import + `__all__` 条目）
- Delete: `src/ctx_weft/core/orchestrator/agent_capability.py`
- Delete: `tests/unit/test_agent_capability_provider.py`
- Modify: `tests/integration/test_minimal_loop.py`（`InMemoryTemplateResolver` → `InlineAgentTemplateProvider` + `make_runtime` 改造）
- Modify: 全部引用旧符号的测试文件（grep 驱动，见 Step 3）
- Modify: `tests/unit/test_subagent_scoping.py`（整文件替换）
- Modify: `README.md`（构造示例）

**Interfaces:**
- Consumes: Task 1 的包（本任务不直接用，但 README 示例指向它）；`AgentCapabilityProvider` / `AgentCapability` / `CapabilityProviderInfo`
- Produces:
  - 测试桩 `InlineAgentTemplateProvider`（`tests/integration/test_minimal_loop.py`，`name="agent"`，`.register(template)`）
  - `make_runtime(agent_provider=..., **kwargs)`（参数改名，不再收 `template_resolver`）
  - src/ 零残留 `TemplateResolver` / `AgentTemplateSummary` / `TemplateAgentCapabilityProvider`

- [ ] **Step 1: 改造测试基建**——`tests/integration/test_minimal_loop.py`：删除 `InMemoryTemplateResolver` 类与 `make_runtime`，替换为（并把顶部 import 中的 `AgentTemplateSummary`、`TemplateResolver` 移除，加 `AgentCapability`、`AgentCapabilityProvider`、`CapabilityProviderInfo` 到 `ctx_weft.protocols` import——`CapabilityProviderInfo` 若不在 protocols/__init__ 导出则从 `ctx_weft.protocols.capability` import）：

```python
class InlineAgentTemplateProvider(AgentCapabilityProvider):
    """测试私有桩：内存 dict 装 template。

    core src 不 ship in-memory provider（spec 方案 B 决策 4）——32 个测试文件的
    fixture 是内联 AgentTemplate（含 LoopConfig 全字段），SOUL.md 表达不了，故测试
    侧保留此桩；真实目录版实现见 ctx_weft.providers.agent_template_local。
    """

    name = "agent"

    def __init__(self) -> None:
        self._templates: dict[str, AgentTemplate] = {}

    def register(self, template: AgentTemplate) -> None:
        self._templates[template.id] = template

    async def list(self, ctx: ProviderContext) -> list:
        return [
            AgentCapability(
                id=f"{self.name}:{t.id}", name=t.id, template_name=t.id,
                description=t.metadata.get("description", ""), version=t.version,
            )
            for t in self._templates.values()
        ]

    async def get_template(self, template_id, version, ctx) -> AgentTemplate | None:
        return self._templates.get(template_id)

    async def describe(self, ctx) -> CapabilityProviderInfo:
        return CapabilityProviderInfo(
            name=self.name, capability_count=len(self._templates),
            supports_streaming=False, supports_cancel=False,
        )


def make_runtime(**kwargs) -> CtxWeftRuntime:
    """测试构造入口：把 agent_provider 注册进 registry 后构造 runtime（方案 B：无适配器）。"""
    provider = kwargs.pop("agent_provider")
    providers = kwargs.pop("providers", None) or ProviderRegistry()
    providers.register_capability(provider)
    return CtxWeftRuntime(providers=providers, **kwargs)
```

- [ ] **Step 2: 源码删除面**——
  - `protocols/template.py`：删 `AgentTemplateSummary` 与 `TemplateResolver` 两个定义及其分节注释；模块 docstring 中 resolver 相关行删除，补一句 `模板进入 core 的唯一通道是 AgentCapabilityProvider（spec 2026-07-22 方案 B）；目录格式 loader 见 providers/agent_template_local。`
  - `protocols/__init__.py`：删两个符号的 import 与 `__all__` 条目。
  - 删除 `src/ctx_weft/core/orchestrator/agent_capability.py` 与 `tests/unit/test_agent_capability_provider.py`（`git rm`）。

- [ ] **Step 3: 机械清扫（grep 驱动）**——按以下规则逐文件改（先 `Grep pattern="InMemoryTemplateResolver|template_resolver=|TemplateAgentCapabilityProvider|AgentTemplateSummary|TemplateResolver" path=tests` 列全清单）：
  - `InMemoryTemplateResolver` → `InlineAgentTemplateProvider`（import 与实例化，约 32 文件）；
  - `make_runtime(...)` 调用里的 `template_resolver=` → `agent_provider=`（约 52 处）；
  - `TemplateAgentCapabilityProvider(resolver)` 包装（`test_compact_flow_e2e.py`、`test_session_event_order.py` 等 TemplateLookup 直构处）→ 直接 `register_capability(resolver)`（resolver 变量现在本身就是 provider），删除其 import；
  - 测试文件里残留的 `AgentTemplateSummary` / `TemplateResolver` import 一律移除（自建桩改用 `AgentCapability` 形状——`test_template_lookup.py` 已是，无需动）。
  - `tests/unit/test_subagent_scoping.py` **整文件替换**：

```python
"""Sub-agent scoping: an agent binds ONLY its declared `subagents`, not every template.

AgentCapabilityProvider 的协议默认 retrieve() 返回 []：全目录只经 list() 暴露给
required-ref 精确查找，永不自动召回——allowlist 语义在协议基类（spec 2026-07-22）。
"""

from __future__ import annotations

from types import SimpleNamespace

from ctx_weft.core.orchestrator.capability_resolver import CapabilityResolver
from ctx_weft.protocols.capability import (
    AgentCapability, AgentCapabilityProvider, CapabilityProviderInfo,
)
from ctx_weft.protocols.context import ProviderContext
from ctx_weft.protocols.template import CapabilityRef


class _CatalogProvider(AgentCapabilityProvider):
    name = "agent"

    async def list(self, ctx):
        return [
            AgentCapability(id="agent:planner", name="planner", template_name="planner"),
            AgentCapability(id="agent:default", name="default", template_name="default"),
        ]

    async def get_template(self, template_id, version, ctx):
        return None

    async def describe(self, ctx):
        return CapabilityProviderInfo(name=self.name)


def _template(refs):
    return SimpleNamespace(id="default", capability_refs=refs)


async def test_only_declared_subagent_is_bound() -> None:
    provider = _CatalogProvider()
    template = _template([CapabilityRef(capability_id="agent:planner", mode="required")])
    ctx = ProviderContext(session_id="s1", tenant_id="default")
    bound = await CapabilityResolver().resolve(template, task=None, providers=[provider], ctx=ctx)
    assert {c.id for c in bound} == {"agent:planner"}  # NOT agent:default
```

- [ ] **Step 4: 零残留断言 + 全量测试**

Run: `Grep pattern="TemplateResolver|AgentTemplateSummary|TemplateAgentCapabilityProvider|list_summaries" path=src/ctx_weft` → 期望零命中。
Run: `uv run pytest -q`
Expected: 除基线 3 个环境性失败外全绿、零新增。

- [ ] **Step 5: README 同步**——`Grep pattern="TemplateAgentCapabilityProvider|template_resolver|TemplateResolver" path=README.md`，构造示例改为：

```python
from ctx_weft.providers.agent_template_local import LocalAgentTemplateProvider
providers.register_capability(LocalAgentTemplateProvider(Path("resources/templates")))
runtime = CtxWeftRuntime(providers=providers)
```

- [ ] **Step 6: 提交**（`git status --short` 全检后逐文件点名 add：上述源码/测试/README + `git rm` 的两个文件）

```bash
git commit -m "feat(protocols)!: 删 TemplateResolver/AgentTemplateSummary/适配器——模板通道归一到 provider"
```

---

### Task 3: host 切换——DirAgentCapabilityProvider 直实现 + 管理面重定向（IpMasterCoworkPy）

**前置：** host 仓根 `powershell -File scripts\revendor-core.ps1`；验证 `uv run python -c "from ctx_weft.providers.agent_template_local import LocalAgentTemplateProvider, PROVIDER_NAME, TemplateLoader"`。

**Files:**
- Create: `src/ipmastercowork/providers/templates/provider.py`（取代 resolver.py）
- Delete: `src/ipmastercowork/providers/templates/resolver.py`、`src/ipmastercowork/providers/templates/loader.py`
- Modify: `src/ipmastercowork/providers/templates/__init__.py`、`syncer.py`、`api/deps.py`、`api/templates.py`、`api/sessions.py`（默认模板段 ~179-188）、`cli.py`（build_runtime + cmd_serve）、`api/main.py`（create_app）、`create_app` 的其他调用方（grep，含 `_run.py`）
- Test: 新建 `tests/test_dir_agent_provider.py`；删 `tests/test_loader_subagents.py`、`tests/test_template_resolver_merge.py`；改 `tests/test_build_runtime_wiring.py`

**Interfaces:**
- Consumes: core 的 `PROVIDER_NAME` / `TemplateLoader` / `merge_default_facets` / `DEFAULT_MERGE_PURPOSES`（`ctx_weft.providers.agent_template_local`）；`TemplateStore`（不动）
- Produces:
  - `DirAgentCapabilityProvider(store, loader=None, default_template_id="default")`
  - `TemplateSyncer.store` property
  - `deps.set_template_store/get_template_store`、`deps.set_agent_template_provider/get_agent_template_provider`（替代 `set/get_template_registry`）
  - `build_runtime(args) -> (runtime, syncer, provider)`；`create_app(..., agent_template_provider=...)`

- [ ] **Step 1: 写失败测试**——新建 `tests/test_dir_agent_provider.py`（迁自 `test_template_resolver_merge.py`，改打 provider；`_FakeStore`/`_make_dir` 沿用原文件形状）：

```python
"""DirAgentCapabilityProvider：store 元数据 + core loader 加载 + 默认 facet 合并。"""

from __future__ import annotations

from ctx_weft.providers.agent_template_local import TemplateLoader
from ipmastercowork.providers.templates.provider import DirAgentCapabilityProvider


class _FakeStore:
    def __init__(self, mapping):
        self._m = mapping  # id -> Path

    async def get(self, tid):
        d = self._m.get(tid)
        return {"template_dir": str(d)} if d else None

    async def find_by_name(self, name):
        return await self.get(name)

    async def list_all(self):
        return [{"id": k, "name": k, "version": "1.0.0", "description": None}
                for k in self._m]


def _make_dir(tmp_path, name, *, compact=None, metadata=None):
    d = tmp_path / name
    d.mkdir()
    (d / "SOUL.md").write_text(
        f"---\nname: {name}\nversion: 1.0.0\n---\n{name} soul", encoding="utf-8"
    )
    if compact is not None:
        (d / "COMPACT.md").write_text(compact, encoding="utf-8")
    if metadata is not None:
        (d / "METADATA.md").write_text(metadata, encoding="utf-8")
    return d


async def test_get_template_inherits_default_facets(tmp_path):
    ddir = _make_dir(tmp_path, "default", compact="DEF-COMPACT", metadata="DEF-MD")
    fdir = _make_dir(tmp_path, "foo")
    prov = DirAgentCapabilityProvider(_FakeStore({"default": ddir, "foo": fdir}),
                                      TemplateLoader(), default_template_id="default")
    t = await prov.get_template("foo", None, None)
    assert t.identity["compact"].text == "DEF-COMPACT"
    assert t.identity["recognize_intent"].text == "DEF-MD"


async def test_get_template_miss_returns_none(tmp_path):
    prov = DirAgentCapabilityProvider(_FakeStore({}), TemplateLoader())
    assert await prov.get_template("nope", None, None) is None


async def test_default_itself_not_self_merged(tmp_path):
    ddir = _make_dir(tmp_path, "default", compact="DEF-COMPACT")
    prov = DirAgentCapabilityProvider(_FakeStore({"default": ddir}),
                                      TemplateLoader(), default_template_id="default")
    t = await prov.get_template("default", None, None)
    assert t.identity["compact"].text == "DEF-COMPACT"


async def test_list_null_description_coalesced(tmp_path):
    # 回归（原 test_list_summaries_null_description_coalesced）：DB 描述列可空 →
    # None 必须归一 ""，否则经装配层 content_to_text 崩溃。
    ddir = _make_dir(tmp_path, "a")
    prov = DirAgentCapabilityProvider(_FakeStore({"a": ddir}), TemplateLoader())
    caps = await prov.list(None)
    assert caps[0].id == "agent:a"
    assert caps[0].description == ""
```

Run: `uv run pytest tests/test_dir_agent_provider.py -q`
Expected: FAIL——`ModuleNotFoundError: ...provider`。

- [ ] **Step 2: 实现 provider + 删旧文件**——新建 `src/ipmastercowork/providers/templates/provider.py`：

```python
"""DirAgentCapabilityProvider：TemplateStore（DB 元数据）+ core TemplateLoader。

方案 B（spec 2026-07-22 unification）：TemplateResolver 协议已删，host 直接实现
AgentCapabilityProvider。get() 语义原样自 TemplateDirResolver 平移：store 查
meta（get → find_by_name 回落）→ 磁盘按需 load（热更新友好）→ 非 default 模板
从 default 补缺省 facet。miss 返回 None（协议契约，由 TemplateLookup 转
TemplateNotFoundError）。
"""

from __future__ import annotations

import logging
from pathlib import Path

from ctx_weft.protocols.capability import (
    AgentCapability,
    AgentCapabilityProvider,
    Capability,
    CapabilityProviderInfo,
)
from ctx_weft.protocols.context import ProviderContext
from ctx_weft.protocols.template import AgentTemplate
from ctx_weft.providers.agent_template_local import (
    DEFAULT_MERGE_PURPOSES,
    PROVIDER_NAME,
    TemplateLoader,
    merge_default_facets,
)
from ipmastercowork.providers.templates.store import TemplateStore

logger = logging.getLogger(__name__)


class DirAgentCapabilityProvider(AgentCapabilityProvider):
    name = PROVIDER_NAME

    def __init__(
        self,
        store: TemplateStore,
        loader: TemplateLoader | None = None,
        default_template_id: str = "default",
    ) -> None:
        self._store = store
        self._loader = loader or TemplateLoader()
        self._default_template_id = default_template_id

    async def list(self, ctx: ProviderContext) -> list[Capability]:
        try:
            rows = await self._store.list_all()
        except Exception:
            logger.exception("DirAgentCapabilityProvider: list_all failed")
            return []
        return [
            AgentCapability(
                id=f"{PROVIDER_NAME}:{d['id']}",
                name=d["id"],
                template_name=d["id"],
                # DB 描述列可空：None 必须归一 ""（原 resolver 的回归语义保留）
                description=d.get("description") or "",
                version=d.get("version") or "",
            )
            for d in rows
        ]

    async def get_template(
        self, template_id: str, version: str | None, ctx: ProviderContext,
    ) -> AgentTemplate | None:
        meta = await self._store.get(template_id)
        if meta is None:
            meta = await self._store.find_by_name(template_id)
        if meta is None:
            return None
        template = self._loader.load(Path(meta["template_dir"]))
        if template.id != self._default_template_id:
            default = await self._load_default()
            if default is not None:
                merge_default_facets(template, default, DEFAULT_MERGE_PURPOSES)
        return template

    async def _load_default(self) -> AgentTemplate | None:
        try:
            meta = await self._store.get(self._default_template_id)
            if meta is None:
                meta = await self._store.find_by_name(self._default_template_id)
            if meta is None:
                return None
            return self._loader.load(Path(meta["template_dir"]))
        except Exception:
            logger.exception("DirAgentCapabilityProvider: failed to load default template")
            return None

    async def describe(self, ctx: ProviderContext) -> CapabilityProviderInfo:
        try:
            count = len(await self._store.list_all())
        except Exception:
            count = 0
        return CapabilityProviderInfo(
            name=self.name, capability_count=count,
            supports_streaming=False, supports_cancel=False,
            description=self.description,
        )
```

`git rm src/ipmastercowork/providers/templates/resolver.py src/ipmastercowork/providers/templates/loader.py`（`InMemoryTemplateRegistry` 随 resolver.py 消亡）。

- [ ] **Step 3: 接线改造**——
  - `providers/templates/__init__.py`：`AGENT_PROVIDER_NAME` 改为 `from ctx_weft.providers.agent_template_local import PROVIDER_NAME as AGENT_PROVIDER_NAME`（注释保留数据契约说明）；`canonical_template_id` 不动；模块 docstring 的 resolver 行改为 provider。
  - `syncer.py`：`from ctx_weft.providers.agent_template_local import TemplateLoader`（替换 host loader import）；类内加：

```python
    @property
    def store(self) -> TemplateStore:
        return self._store
```

  - `api/deps.py`：删 `_template_registry`/`set_template_registry`/`get_template_registry`，替换为：

```python
_template_store: Any = None
_agent_template_provider: Any = None

def set_template_store(s: Any) -> None:
    global _template_store; _template_store = s

def set_agent_template_provider(p: Any) -> None:
    global _agent_template_provider; _agent_template_provider = p

def get_template_store() -> Any:
    if _template_store is None:
        raise HTTPException(status_code=503, detail="TemplateStore not initialized")
    return _template_store

def get_agent_template_provider() -> Any:
    if _agent_template_provider is None:
        raise HTTPException(status_code=503, detail="Agent template provider not initialized")
    return _agent_template_provider
```

  - `api/templates.py`：列表/详情两个端点改为：

```python
@router.get("", response_model=list[TemplateSummaryResponse])
async def list_templates(store=Depends(deps.get_template_store)):
    return [
        TemplateSummaryResponse(
            id=d["id"], name=d["name"], version=d["version"],
            description=d.get("description") or "",
        )
        for d in await store.list_all()
    ]


@router.get("/{template_id}", response_model=TemplateDetailResponse)
async def get_template(template_id: str, provider=Depends(deps.get_agent_template_provider)):
    from ctx_weft.protocols.context import ProviderContext
    ctx = ProviderContext(session_id="", tenant_id="system")
    template = await provider.get_template(template_id, None, ctx)
    if template is None:
        raise HTTPException(status_code=404, detail=f"Template '{template_id}' not found")
    return TemplateDetailResponse(
        id=template.id, name=template.name, version=template.version,
        description=template.description,
        tool_refs=[ref.capability_id for ref in template.capability_refs],
        has_soul="act" in template.identity,
        has_role="observe" in template.identity,
    )
```

  - `api/sessions.py` 默认模板段（~179-188）：

```python
    template_id = req.template_id
    if not template_id:
        store = deps._template_store
        if store is not None:
            rows = await store.list_all()
            template_id = rows[0]["id"] if rows else "echo"
        else:
            template_id = "tpl_echo"
```

  - `cli.py` `build_runtime()`：`TemplateLoader` 改 import core（`from ctx_weft.providers.agent_template_local import TemplateLoader`）；适配器注册两行替换为：

```python
    from ipmastercowork.providers.templates.provider import DirAgentCapabilityProvider
    provider = DirAgentCapabilityProvider(
        template_store, template_loader, default_template_id=cfg.default_template_id
    )
    providers.register_capability(provider)
```

    返回 `return runtime, syncer, provider`（原第三元 resolver 换 provider）；`cmd_serve` 解包变量名与 `create_app(..., agent_template_provider=provider, ...)` 跟改；`cmd_run` 解包 `runtime, _, _` 不变。
  - `api/main.py` `create_app`：参数 `template_registry` 改名 `agent_template_provider`；体内改：

```python
    deps.set_agent_template_provider(agent_template_provider)
    deps.set_template_store(template_syncer.store)
```

  - `Grep pattern="create_app\(|template_registry|get_template_registry|set_template_registry" path=src tests` 清残留（已知 `_run.py` 也调 create_app，跟改）。

- [ ] **Step 4: 测试迁移**——`git rm tests/test_loader_subagents.py tests/test_template_resolver_merge.py`（用例已由 core `test_agent_template_local.py` 与本仓 `test_dir_agent_provider.py` 承接）；`tests/test_build_runtime_wiring.py`：`TemplateAgentCapabilityProvider` import/断言改 `DirAgentCapabilityProvider`（`from ipmastercowork.providers.templates.provider import ...`），三元组第三元断言改 provider（`assert provider is not None`——变量语义即 create_app 要传的 agent_template_provider）；`canonical_template_id` 两个用例不动。

- [ ] **Step 5: 跑新测试 + 全量**

Run: `uv run pytest tests/test_dir_agent_provider.py tests/test_build_runtime_wiring.py -q`
Expected: PASS。
Run: `uv run pytest -q`
Expected: 除基线 1 个（test_skills_reference）外全绿、零新增（重点关注 templates API / sessions 相关存量测试，若有引用 `get_template_registry` 的按 Step 3 口径跟改）。

- [ ] **Step 6: 提交**（逐文件点名 add，含 revendor 产生的 `vendor/*.whl` 与 `uv.lock`）

```bash
git commit -m "feat(host)!: DirAgentCapabilityProvider 直实现——loader 引 core、管理面直读 store/provider"
```
