# AgentCapabilityProvider 协议补全：发现与加载同源

日期：2026-07-22
状态：已批准（用户确认设计后实施）

## 问题

`AgentCapabilityProvider` 协议是空壳：只有 `list()` 返回 `AgentCapability`，
`template_name` 只是字符串。协议文档声称「list 型 provider，可注册多个（本地模板 +
远端注册中心等）」，但派发 sub-agent 时的实际路径是：

1. `runtime._resolve_subagent_template()` 遍历所有 agent provider，把 qualified 名
   （`agent__planner`）映射回 `template_name` 字符串——**丢掉了是哪个 provider 匹配的**；
2. `LifecycleManager.instantiate_agent()` 用构造时注入的**单例 `TemplateResolver`**
   去 `get(template_id)` 加载模板。

于是第二个 agent provider 只能让模板**被看见**（进 prompt 的 Sub-Agents 列表），
不能让它**被加载**（spawn 时单例 resolver 查不到 → `KeyError`）。能力发现层与模板
加载层的关系是隐式的——恰好 `TemplateAgentCapabilityProvider` 包装了同一个 resolver
实例——协议上没有表达。

驱动力是协议正确性/自洽（暂无具体第二来源要接），因此不引入远端注册中心等机制，
只把「capability ↔ template」这层关系正式化。

## 决策

**统一到 provider：`AgentCapabilityProvider` 成为模板进入 core 的唯一通道。**

- 协议增加 `get_template()`——发现与加载同源：provider `list()` 出来的每个
  `AgentCapability.template_name`，它自己的 `get_template()` 必须能加载。
- `TemplateResolver` 协议**保留但降级**：不再是 core↔host 的模板接口，只是目录/
  注册表型模板源的 SPI，经内置 `TemplateAgentCapabilityProvider` 适配接入。host
  也可以跳过它直接实现 `AgentCapabilityProvider`。
- `CtxWeftRuntime` 构造签名删除 `template_resolver` 参数与同名公开属性；**构造时
  registry 中必须已注册至少一个 `AgentCapabilityProvider`，否则抛错（fail-fast）**——
  模板通道是 runtime 的运行前提（root agent 都无法实例化），不同于 MCP 工具那类
  可后到的增强能力。
- 直接破坏性变更，ctx-weft 与 IpMasterCoworkPy 两仓同步改，不留兼容垫片
  （一作者两仓库 lockstep，兼容期是纯仪式）。

## 协议契约（写进 protocols/capability.py）

```python
class AgentCapabilityProvider(CapabilityProvider, ABC):
    """列出可用 sub-agent 模板，并负责加载自己列出的模板。"""

    @abstractmethod
    async def get_template(
        self, template_id: str, version: str | None, ctx: ProviderContext,
    ) -> AgentTemplate | None: ...

    async def retrieve(self, ctx: ProviderContext) -> list[Capability]:
        return []
```

1. **get_template**：入参是本 provider 命名空间内的**局部模板名**（即它自己
   `list()` 出的 `AgentCapability.template_name`，前缀已由 TemplateLookup 剥掉）；
   不认识 → 返回 `None`（由 TemplateLookup 转成 `TemplateNotFoundError`）；仅真实
   故障（IO/网络/解析错误）才抛异常。`version=None` 取最新。
2. **retrieve() 默认 `[]`**：allowlist 语义从 `TemplateAgentCapabilityProvider` 的
   实现注释上移为协议默认——sub-agent 只经模板声明的 `subagents` required refs 绑定，
   永不自动召回；确需语义召回的 provider 可覆盖。
3. **AgentCapability 增加 `version: str = ""`**：信息性字段（listing 展示）。spawn
   与 root 实例化一律 `version=None` 取最新，保留 `TemplateDirResolver` 每次读盘的
   热更新语义；未来要 pin 时协议已备好参数，不属本次范围。

## 核心组件：TemplateLookup

新增 `core/orchestrator/template_lookup.py`，内部组件（**不是协议**），持有
`ProviderRegistry`，消灭隐式单例路径。**路由靠前缀，不做注册序扫描**——provider
归属本来就编码在 `cap.id`（`<provider名>:<模板名>`）里，扫描等于扔掉已有路由信息
再靠注册序猜，还引入撞名时的顺序依赖：

```python
class TemplateLookup:
    async def resolve_qualified(self, qualified: str, ctx) -> str:
        """agent__planner → 完整 cap.id（'agent:planner'）——保留 provider 归属，
        不降级成裸 template_name（原 runtime._resolve_subagent_template 的匹配逻辑
        平移，返回值升级）；未命中原样返回。"""

    async def get_template(self, ref: str, version, ctx) -> AgentTemplate:
        """规范 id 精确路由：rsplit(':', 1) 前段命中某已注册 AgentCapabilityProvider
        的 name → 只调该 provider 的 get_template(局部模板名)；该 provider 返回
        None → TemplateNotFoundError，不问别人（确定性）。rsplit 口径与现有
        _provider_meta 一致，兼容 'mcp:github:researcher' 类多段 provider 名。

        裸 id（无可路由前缀）→ 直接 TemplateNotFoundError，错误信息提示规范形式
        （边界强制前缀，见「边界语义」）。provider 抛异常 → 传播（路由已确定，
        不存在跳过语义）。"""
```

## 改动点

### ctx-weft

1. **`protocols/capability.py`**：`AgentCapabilityProvider` 按上述契约补全；
   `AgentCapability` 加 `version` 字段。
2. **`protocols/template.py`**：`TemplateResolver` docstring 改写为 SPI 定位；
   接口本身（`get` / `list_summaries`）与 `AgentTemplate` / `AgentTemplateSummary` 不动。
3. **`core/orchestrator/agent_capability.py`**：`TemplateAgentCapabilityProvider`
   补 `get_template()`——委托 `resolver.get(template_id, version, ctx)`，
   `KeyError` → `None`；删除其 `retrieve()` override（语义已上移基类）；
   `list()` 顺带把 summary.version 填进 `AgentCapability.version`。
4. **`core/orchestrator/template_lookup.py`**（新增）：如上。
5. **`core/errors.py`**：新增 `TemplateNotFoundError`。
6. **`core/runtime.py`**：
   - 构造签名删 `template_resolver` 参数、删 `template_resolver` 属性（502）、
     删自动注册内置 provider（473）；
   - 构造时校验 registry 含 ≥1 `AgentCapabilityProvider`，否则抛 `ValueError`；
   - `self._template_lookup = TemplateLookup(self.providers)`；
   - 删 `_resolve_subagent_template`（544），调用点（1970）改
     `template_lookup.resolve_qualified`；
   - 直接 `resolver.get()` 加载 root 模板处（808/1158）改 `template_lookup.get_template`；
   - 5 处 `LifecycleManager(template_resolver=...)` 改传 `template_lookup`；
   - 1729 处传给 driver 的 `template_resolver=` 删除。
7. **`core/orchestrator/lifecycle_manager.py`**：`template_resolver` 字段改
   `template_lookup: TemplateLookup`，`instantiate_agent` 走 `get_template`。
8. **`core/loop/driver.py:132`**：删除从未被读的 `template_resolver` 死字段。

### IpMasterCoworkPy

9. **`cli.py` `build_runtime()`**：构造 `TemplateDirResolver` 后
   `providers.register_capability(TemplateAgentCapabilityProvider(resolver))`，
   runtime 构造去掉 `template_resolver=` 参数。
10. **`api/main.py:26`**：`deps.set_template_registry(runtime.template_resolver)`
    改为 host 自持 resolver 引用（`build_runtime` 内直接 set，或返回二元组，实施时
    看装配顺序取顺手的）。`TemplateDirResolver` / `TemplateStore` / `TemplateLoader` /
    `merge_default_facets`（default facet 合并）全部不动。
11. **边界规范化 + 存量迁移**：
    - host 调 `start_session` / resume 传参处（`api/sessions.py`、`cli.py` 的
      default_template_id 使用点）把裸模板 id 规范化为 `agent:<id>`（前缀即 host
      注册适配器时的 provider 名，单一常量）；host 自己的模板管理 API / 前端
      继续用裸 id，转换只发生在 ctx-weft 边界；
    - `persistence/postgres/migrations.py` 加一次性迁移：事件表中 SessionCreated
      等载荷内的裸 `template_id` → `'agent:' + id`（仓库已有 payload 回填迁移
      先例）。快照/投影若冗余存了 template_id 一并覆盖。

## 边界语义

- **边界强制规范 id（`provider:模板名`），裸 id 报错**：
  - host 边界：`start_session(params.template_id)` 传规范形式（`agent:default`）——
    适配器 provider 是 host 自己注册的，前缀由 host 自己掌握；
  - LLM 边界：`subagent_template` 正常是 qualified 名（prompt 列表所示），
    `resolve_qualified` 命中后返回规范 cap.id；LLM 写了不在列表里的裸名 →
    原样透传 → `get_template` 报 `TemplateNotFoundError`（信息提示需用列表中的
    qualified 名），该任务失败——**字面裸名透传能加载的旧行为不再保留**；
  - `assemble()` 里 `or self._template_id` 的回落（subagent_template 为空时用会话
    根模板）不变——session 的 template_id 现已是规范形式，口径自洽。
- **存量数据**：resume / 冷 HITL / 手动 compact 的 template_id 来自 event store 重放
  （SessionCreated 载荷），升级前的存量会话存的是裸 id，严格模式下不可恢复。
  处理：**host DB 一次性迁移重写事件载荷**（见改动点 11），runtime 不留兼容逻辑。
- **派发失败口径**：`TemplateNotFoundError` 在派发路径传播、该任务失败，不影响其他
  任务——与今天 `KeyError` 的传播行为相同，只是错误可读。
- **describe()**：不为 `get_template` 增加 describe 字段（YAGNI）。

## 测试

ctx-weft：

1. 协议契约（`test_agent_capability_provider.py`）：`get_template` 委托 resolver、
   `KeyError`→`None`、version 透传；基类 `retrieve()` 默认 `[]`；`list()` 填 version。
2. `TemplateLookup` 新单测：qualified 反查命中返回完整 cap.id / 未命中透传；
   前缀精确路由（含多段 provider 名 rsplit 口径）；路由命中但 provider 返回 None →
   `TemplateNotFoundError` 且不问其他 provider；裸 id → `TemplateNotFoundError`
   （信息含规范形式提示）；provider 异常传播。
3. 构造期：registry 无 `AgentCapabilityProvider` 时构造 runtime 抛 `ValueError`。
4. 回归：`test_subagent_scoping.py` 语义不变（allowlist 仍靠 retrieve=[]），只改
   测试装配方式（显式注册适配器）；runtime 级冒烟——start_session + use_subagent
   派发全程不触碰已删除的 `_template_resolver`。

IpMasterCoworkPy：

5. `build_runtime()` 接线测试：registry 含适配器 provider、deps 拿到 resolver；
   start_session 边界传出规范 id。`test_template_resolver_merge.py` 等 resolver
   自身测试不动。
6. 迁移测试（`test_migrations.py`）：存量事件载荷裸 template_id 被重写为
   `agent:` 前缀形式；已规范化的载荷幂等不动。
