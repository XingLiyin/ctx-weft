# 方案 B：删除 TemplateResolver，模板 provider 归一

日期：2026-07-22
状态：已批准（用户确认设计后实施）
前置：spec 2026-07-22-agent-capability-template-protocol（方案 A）已落地——
`get_template` 协议、`TemplateLookup` 前缀路由、m010 存量数据迁移均在
feat/agent-capability-template-protocol 分支（ctx-weft befcbaf / app 5d70912）。

## 问题

方案 A 保留了 `TemplateResolver` 作为「内置适配器的 SPI」，代价是双身份残留：

1. **协议双轨**：`AgentCapabilityProvider`（正门）与 `TemplateResolver`（SPI 后门）
   并存，`AgentTemplateSummary` 与 `AgentCapability` 字段重叠（id/name/version/
   description），`TemplateAgentCapabilityProvider` 适配器纯粹是两者之间的搬运工。
2. **模板格式解析器在 host**：`TemplateLoader`（SOUL.md/ROLE.md/frontmatter 解析）
   住在 IpMasterCoworkPy，而 skills 的同类解析（SKILL.md）早已是 core 资产
   （`ctx_weft.providers.capability_skill_local._parser`）。core 一旦要 ship 目录版
   provider，就会出现同一格式两个解析器的漂移风险。

方案 A 定案时已把 B 标注为长期方向；本次执行 B。

## 决策

1. **删协议**：ctx-weft 删除 `TemplateResolver` 协议 + `AgentTemplateSummary` +
   `TemplateAgentCapabilityProvider` 适配器。模板进入 core 的唯一形态就是
   `AgentCapabilityProvider` 实现，无二级 SPI。
2. **core ship 单根目录扫描版 provider**：新包
   `ctx_weft/providers/agent_template_local/`（对标 `capability_skill_local`）：
   - `_loader.py`：`TemplateLoader` + `merge_default_facets` 自 host **整体上移**
     （单一解析器；模板目录格式从此是 core 定义的资产）；
   - `provider.py`：`LocalAgentTemplateProvider(AgentCapabilityProvider)`，
     `__init__(templates_root: Path, default_template_id="default")`，
     `name = PROVIDER_NAME`（`"agent"` 常量随包回 core，单一真相恢复）；
     `list()` / `get_template()` 每次扫盘（根目录下含 SOUL.md 的子目录即模板），
     热更新友好；get_template miss → `None`；默认 facet 合并
     （compact/recognize_intent/observe 缺项从 default 模板补）与 host 现行为一致。
3. **host 保留自己的管理面与 provider**：`TemplateDirResolver` 按方案 A 评审时批准
   的「彻底合一」改造成 `DirAgentCapabilityProvider`（TemplateStore + core loader），
   host 的 DB store / syncer / 任意路径注册能力**全部保留**——注册走 host 现有机制。
   host **直接引用** core 的 loader 与 `PROVIDER_NAME`，host 侧 `loader.py` 删除。
4. **core src 不 ship in-memory provider**：ctx-weft 测试用 `test_minimal_loop.py`
   内的**测试私有桩**（约 20 行，`.register(template)` 形状，name="agent"）——
   32 个测试文件的 fixture 是内联 `AgentTemplate` 对象（含 LoopConfig 全字段），
   SOUL.md frontmatter 表达不了全部字段，磁盘 fixture 化不可行。
5. 破坏性变更两仓 lockstep，不留兼容垫片（同方案 A 口径）。

## 不变量（硬约束）

- **`PROVIDER_NAME = "agent"` 是数据契约**：m010 已把存量事件/快照写成 `agent:`
  前缀、host 边界 canonical 持续产出它——本次**不得改名**，且**无新 DB 迁移**。
- **运行时零可观测差异**：`TemplateLookup` 路由、构造校验（registry 须含 ≥1
  AgentCapabilityProvider 否则 ValueError）、`TemplateNotFoundError` 语义、
  get_template 的 None/异常契约全部不动。本次纯属删身份、搬实现。
- 加载一律 `version=None`；前缀拆分 `rsplit(":", 1)` 口径不动。

## 改动点

### ctx-weft（删除面）

1. `protocols/template.py`：删 `TemplateResolver`、`AgentTemplateSummary`；
   **保留** `AgentTemplate` / `IdentityFacet` / `CapabilityRef` / `MemoryConfig` /
   `LoopConfig`（core 数据模型）；模块 docstring 去 resolver 表述。
2. `protocols/__init__.py`：删两个 export。
3. `core/orchestrator/agent_capability.py`：**整文件删除**（适配器 + 旧
   PROVIDER_NAME 位置）。
4. 删 `tests/unit/test_agent_capability_provider.py`（适配器亡）；
   `test_subagent_scoping.py` 改用本地 provider 桩（allowlist 语义在协议基类，
   断言不变）。

### ctx-weft（新增面）

5. `providers/agent_template_local/__init__.py`：导出
   `LocalAgentTemplateProvider`、`PROVIDER_NAME`、`TemplateLoader`、
   `merge_default_facets`。
6. `providers/agent_template_local/_loader.py`：host `providers/templates/loader.py`
   与 `merge_default_facets`（resolver.py）逻辑原样搬移（含 SOUL/ROLE frontmatter、
   tools → capability_refs、subagents → required refs、DEFAULT_MERGE_PURPOSES）。
7. `providers/agent_template_local/provider.py`：`LocalAgentTemplateProvider`
   如「决策 2」；`describe()` 报扫描计数。
8. 测试基建：`test_minimal_loop.py` 的 `InMemoryTemplateResolver` 改造成测试私有
   `InlineAgentTemplateProvider(AgentCapabilityProvider)`（保 `.register()`，
   name="agent"）；`make_runtime` 直接注册它（参数改名 `agent_provider=`，随
   32 文件 import + 52 处调用点机械 sed）。
9. 新单测 `tests/unit/test_agent_template_local.py`：tmp_path 真实 SOUL.md 目录——
   扫描发现/AgentCapability 形状、get_template miss→None、默认 facet 合并、
   subagents frontmatter → required refs（用例自 host `test_loader_subagents` 迁移）。

### IpMasterCoworkPy

10. `providers/templates/resolver.py` → `provider.py`：`DirAgentCapabilityProvider
    (AgentCapabilityProvider)`（name = core `PROVIDER_NAME`；store 查 meta，miss →
    None；core `TemplateLoader` 加载 + core `merge_default_facets`）。
    `InMemoryTemplateRegistry` 删除（其测试用途由改打 provider 的用例覆盖）。
11. 删 `providers/templates/loader.py`（上移 core）；`test_loader_subagents.py` 等
    loader 测试改 import core 包（保留 host 侧仍需的用例，与 9 不重复的部分）。
12. `providers/templates/__init__.py`：`AGENT_PROVIDER_NAME` 改为 re-export core
    `PROVIDER_NAME`；`canonical_template_id` 不变。
13. 管理面重定向（方案 A 评审批准的「彻底合一」）：`api/templates.py` 列表直读
    `TemplateStore.list_all()`；详情走 `provider.get_template(id, None, ctx)`，
    None → 404；`api/sessions.py` 默认模板选择改 `store.list_all()` 取首个。
    `deps.set_template_registry` 拆为 `set_template_store(store)` +
    `set_agent_template_provider(provider)`（或合成 host 侧小容器，实施取顺手）。
14. `cli.build_runtime`：构造 `DirAgentCapabilityProvider` 注册；返回值与
    `create_app` 参数按 13 的 deps 形状调整。
15. 照旧 revendor 后 host 才能测。

## 测试

- ctx-weft：全量存量（1365）锤新测试桩；`test_agent_template_local.py` 覆盖目录版
  provider；grep 断言 src/ 零残留 `TemplateResolver|AgentTemplateSummary|
  list_summaries`；基线 3 个环境性失败零新增。
- host：`DirAgentCapabilityProvider` 单测（list 形状、miss→None、merge 行为，自
  `test_template_resolver_merge.py` 迁移）；templates API 列表/详情/404 回归；
  `build_runtime` 接线；全量对照基线（1 个既有失败）零新增。
