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

1. **get_template**：不认识该 id → 返回 `None`（供跨 provider 扫描组合）；仅真实
   故障（IO/网络/解析错误）才抛异常。`version=None` 取最新。
2. **retrieve() 默认 `[]`**：allowlist 语义从 `TemplateAgentCapabilityProvider` 的
   实现注释上移为协议默认——sub-agent 只经模板声明的 `subagents` required refs 绑定，
   永不自动召回；确需语义召回的 provider 可覆盖。
3. **AgentCapability 增加 `version: str = ""`**：信息性字段（listing 展示）。spawn
   与 root 实例化一律 `version=None` 取最新，保留 `TemplateDirResolver` 每次读盘的
   热更新语义；未来要 pin 时协议已备好参数，不属本次范围。

## 核心组件：TemplateLookup

新增 `core/orchestrator/template_lookup.py`，内部组件（**不是协议**），持有
`ProviderRegistry`，消灭隐式单例路径：

```python
class TemplateLookup:
    async def resolve_qualified(self, qualified: str, ctx) -> str:
        """agent__planner → template_name。遍历 AgentCapabilityProvider 的 list()
        按 qualify(cap.id) 精确匹配（原 runtime._resolve_subagent_template 逻辑平移）；
        未命中原样返回（字面 template_id 透传语义保留）。"""

    async def get_template(self, template_id: str, version, ctx) -> AgentTemplate:
        """按注册序扫描各 AgentCapabilityProvider.get_template()，首个非 None 胜出；
        单 provider 异常 → logger.exception + 跳过（与 list() 现有吞异常口径一致）；
        全 miss → raise TemplateNotFoundError（携带 template_id 与已尝试的 provider
        名单，替代现在从 resolver 泄漏的裸 KeyError）。"""
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

## 边界语义

- **字面 id 透传**：`resolve_qualified` 未命中原样返回，随后 `get_template` 按注册序
  扫描——与现状行为一致。`assemble()` 里 `or self._template_id` 的回落（subagent_template
  为空时用会话根模板）不变。
- **派发失败口径**：`TemplateNotFoundError` 在派发路径传播、该任务失败，不影响其他
  任务——与今天 `KeyError` 的传播行为相同，只是错误可读。
- **describe()**：不为 `get_template` 增加 describe 字段（YAGNI）。

## 测试

ctx-weft：

1. 协议契约（`test_agent_capability_provider.py`）：`get_template` 委托 resolver、
   `KeyError`→`None`、version 透传；基类 `retrieve()` 默认 `[]`；`list()` 填 version。
2. `TemplateLookup` 新单测：qualified 反查命中/未命中透传；多 provider 注册序首中；
   单 provider 抛异常被跳过且不影响后续；全 miss 抛 `TemplateNotFoundError`
   （含 provider 名单）。
3. 构造期：registry 无 `AgentCapabilityProvider` 时构造 runtime 抛 `ValueError`。
4. 回归：`test_subagent_scoping.py` 语义不变（allowlist 仍靠 retrieve=[]），只改
   测试装配方式（显式注册适配器）；runtime 级冒烟——start_session + use_subagent
   派发全程不触碰已删除的 `_template_resolver`。

IpMasterCoworkPy：

5. `build_runtime()` 接线测试：registry 含适配器 provider、deps 拿到 resolver。
   `test_template_resolver_merge.py` 等 resolver 自身测试不动。
