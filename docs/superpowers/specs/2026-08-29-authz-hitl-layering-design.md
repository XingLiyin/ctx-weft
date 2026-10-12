# 授权与 HITL 契约分层 · 设计

> 状态：设计已定，待实施
> 实施计划：`docs/superpowers/plans/2026-08-29-authz-hitl-layering.md`
> 受影响的权威 spec：`docs/spec/05-authz-and-hitl.md`（本设计**会改**它的 §1 / §5 跨语言清单）

## 1. 问题

`core/auth/` 里住着两类不同性质的东西，被当成一类对待：

| 符号 | 性质 | 现址 | 应在 |
|------|------|------|------|
| `Authorizer` ABC / `AuthorizationDecision` | 契约（host 实现的扩展点） | `core/auth/authorizer.py` | `protocols/` |
| `AllowAllAuthorizer` / `AllowListAuthorizer` / `HumanConfirmationAuthorizer` | 可替换的参考实现 | 同上 | `providers/` |
| `HitlForm` / `HitlStatus` / `HitlRequest` | host-facing 契约（host 读 `form` 决定 UI） | `core/state/models.py` | `protocols/` |
| `HitlManager` | 机制（与 resume/reconcile 语义耦合） | `core/orchestrator/` | **不动** |

三条具体病灶：

**P1 · 契约住在 core，且签名反向依赖 core。**
`protocols/__init__.py` 自述「零运行时依赖」，`protocols/context.py:63` 为了不反向依赖
`core.content` 宁可写惰性模块绑定并贴性能实测。而 `Authorizer.authorize()` 收 `Agent` / `Task`
两个 core 状态 dataclass —— `protocols/*.py` 全层没有任何地方引用它们。protocols 里对应的东西是
`ProviderContext`：它刻意只带 `session_id` / `task_id` / `agent_id` 这些**标识**，不带状态对象。

**P2 · 该签名今天就是错的（latent bug）。**
`core/loop/capability_gateway.py:179-181` 把 `ctx`（一个 **`LoopContext`**）传给形参
`ctx: ProviderContext`。三个内置实现都没读过 `ctx`，所以至今没爆。但任何 host 自写的
Authorizer 只要信了标注去读 `ctx.session_id` 就会 `AttributeError` —— 扩展点是坏的。

**P3 · HITL 的「形式」不可扩展。**
`HitlForm = Literal["approval", "question", "wait"]`（`core/state/models.py:292`）是闭集。
host 想加一种形式（如「diff 逐条审阅」），得改 core 的 models + `control/reducers.py` 三处
`HitlRequest(...)` 重建。而 `HitlRequest` 的 docstring 明写「host 据 form 决定 UI」——
它已经是 host-facing 类型，却住在一个叫「Core state dataclasses」的内部模块里。

### 非问题（已澄清，不改）

- **呈现与渠道**（CLI / Web / Slack）今天就是开的：`HitlRequired` 事件出去，
  `hitl_manager.approve/reject/answer` 回来，core 不碰 UI。
- **触发策略**今天也是开的：`Authorizer` 接口 + per-provider/per-capability 挂载。
- **`HitlManager` 不该可替换**：future 表、幂等复用、热→冷驱逐、`HitlPark`、决定缓存、
  事件发射与 `spec/07` §6/§7 的挂起-恢复模型是一体的。换掉它 ≠「换个 HITL 形式」。

## 2. 决策

### D1 · `Authorizer.authorize` 签名收口

```python
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

去掉 `agent` / `task` 两个位置参数。实现真正用到的字段只有四个，其中三个 `ctx` 已有：

| 实现用到 | 旧来源 | 新来源 |
|---|---|---|
| `agent.template_id` | `Agent` | `ctx.agent_template_id`（**新增字段**） |
| `agent.session_id` | `Agent` | `ctx.session_id` |
| `agent.id` | `Agent` | `ctx.agent_id or ""` |
| `task.id` | `Task \| None` | `ctx.task_id or ""` |

**理由**：这是让契约能进 protocols 的唯一干净路径。把 `Agent`/`Task` 塞进 `TYPE_CHECKING`
再搬走是不可接受的折中——运行时是不导入了，但 protocols 的公开签名里仍写着 core 类型，
属于把反向依赖藏起来而非消除，比留在 core 更糟。

顺带修掉 P2：gateway 改传 `ctx.provider_ctx`。

### D2 · `ProviderContext` 新增 `agent_template_id: str = ""`

放在 `agent_id` 之后。带默认值 → 所有既有构造点不受影响。
生产侧唯一需要填的地方是 `core/runtime.py:1933` 的 `_build_provider_ctx`（单一工厂，3 个调用点）。

### D3 · HITL 契约提层 → `protocols/hitl.py`（新建）

搬 `HitlForm` / `HitlStatus` / `HitlRequest` 三个符号。

- `HitlForm` 由 `Literal[...]` **放宽为 `str`**，well-known 值以模块常量给出
  （`HITL_FORM_APPROVAL` / `HITL_FORM_QUESTION` / `HITL_FORM_WAIT`）。
  core 对未知 form 原样透传、不 assert —— 这就是 P3 要的扩展点。
- `HitlStatus` 保持 `Literal["pending","accepted","rejected","cancelled"]`（**闭集**：
  状态机是 core 的不变式，host 加状态会破坏 reducer 投影，不能开）。
- `created_at` 的 `default_factory` 不能用 `core.utils.now_utc`（反向依赖）。
  在 `protocols/hitl.py` 内写一个两行的等价私有函数 `_now_utc()`（`datetime.now(UTC)`）。
  这是刻意的一行重复，代价小于把 `now_utc` 下沉带来的连锁改动。
- `core/state/models.py` **不留 re-export shim**（对齐 256d3b9「删除 events / event_store
  两个 re-export shim」的既定做法），直接改所有 import 点。

### D4 · `Authorizer` + `AuthorizationDecision` → `protocols/capability.py`

不新开 `protocols/auth.py`：授权是 capability 调用语义的一部分，与 `Capability` /
`CapabilityProvider` 同文件内聚。经 D1 收口后该契约只依赖 `Capability` + `ProviderContext`，
零 core 依赖，可安全落层。

### D5 · 三个实现 → `providers/authorizer/`（新建包）

```
providers/authorizer/__init__.py    re-export 三个类
providers/authorizer/allow.py       AllowAllAuthorizer / AllowListAuthorizer
providers/authorizer/human.py       HumanConfirmationAuthorizer
```

**理由**：`providers/__init__.py` 自述 "reference provider implementations" ——
可替换的参考实现，三个 authorizer 恰好符合。此前留在 core 没有原则性理由：

- 「core 不能依赖 providers」不成立 —— `runtime.py:89` 模块级 import `InProcessEventBus`，
  `runtime.py:498` 惰性 import `InMemoryEventStore`，`state/__init__.py:4` 亦然。
- 「AllowAll 是 gateway 的 fallback 默认」不成立 —— `InProcessEventBus` / `InMemoryEventStore`
  同样是 core 的默认，它们就在 providers 里。同为默认，待遇不该不同。
- 「HumanConfirmation 依赖 HitlManager」不成立 —— `authorizer.py:19` 里它是
  `TYPE_CHECKING` import，运行时只持有一个鸭子类型实例；唯一的运行时 core 依赖是
  `content_to_text`（纯函数）。经 D3 后 `HitlRequest` 也在 protocols，依赖进一步收窄。

`core/auth/` 整个目录删除。

### D6 · `capability_gateway` 的默认解析

`AllowAllAuthorizer` 作为 `default_authorizer` 的兜底保留，但改为**只在 host 没给时才 import**：

```python
if default_authorizer is None:
    from ctx_weft.providers.authorizer import AllowAllAuthorizer
    default_authorizer = AllowAllAuthorizer()
self._default_authorizer = default_authorizer
```

而非现有的 `default_authorizer or AllowAllAuthorizer()` —— 后者无条件求值，
让「默认」从运行期选择退化成 import 期耦合。

### D7 · `runtime` 的 `event_bus` 可注入

`core/runtime.py:481` 的 `self._event_bus = InProcessEventBus()` 是全仓唯一一处
**压根不可注入**的实现硬编码（`providers` / `llm` / `hitl_manager` / `event_store` / `config`
都是 `X or Default()`，`AgentCapabilityProvider` 更是强制注入无默认）。
`_event_bus` 被引用 15+ 处，全链路绑死在进程内实现上；想把事件发到 Redis / NATS / 跨进程今天没有缝。
同一个 `providers/events` 包里 `event_store` 有参数而 `event_bus` 没有，无理由。

改动：
1. `__init__` 增参 `event_bus: EventBus | None = None`，`self._event_bus = event_bus or InProcessEventBus()`
2. `runtime.py:89` 的模块级 import 下沉为按需 import（与 `runtime.py:498` 一致）
3. 兜底 import 统一写成「只在 `is None` 时才 import」

`attach_persistence` 的硬接线**不动** —— `runtime.py:502-510` 的注释说明它编排的是
订阅顺序契约（persister 必须先于 snapshot writer），不是选实现。

三个内置 provider（`ControlCapabilityProvider` / `SkillExecutorCapabilityProvider` /
`MediaCapabilityProvider`）的 auto-register **不动** —— 它们是 core 自身语义的一部分，
不是可替换实现。

## 3. 跨语言影响（必须先确认）

`docs/spec/05-authz-and-hitl.md` 顶部声明「三份实现须复现同一断言」，§5 是给 TS / Java 的
跨语言一致性清单，其第 1 条正是 `authorize(cap, …)` 的签名。**D1 是一次破坏性契约变更**，
TS / Java 移植（见 `docs/ctx-weft_TS移植参考.md`）需同步。

同时该 spec 已存在三处漂移，本次一并订正：

| spec 写的 | 代码实际 |
|---|---|
| `AuthorizationDecision{allowed, message, modified_arguments}` | 还有第四个字段 `defer`（spec/07 §7） |
| HITL `kind`（approval / input） | 代码是 `form`（approval / question / wait） |
| `HitlRequest` 字段表含 `kind` | 实际含 `form` / `tool_call_id` / `questions` / `resume_llm_*` |

## 4. 不做

- 不给 `HitlManager` 抽协议。
- 不动 `attach_persistence` 的订阅顺序接线。
- 不动三个内置 capability provider 的 auto-register。
- 不保留任何 re-export shim。
- 不改 `Authorizer` 挂在 `ProviderRegistry` 上的旁挂 `dict[str, Authorizer]` 设计
  （`runtime.py:214`）——已记录为待议，本次不动。
