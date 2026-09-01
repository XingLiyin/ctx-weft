# HITL 机制重新设计（权威设计 · 语言中立）

> 状态：设计稿，待评审。**这是一份从零设计**——不背现有事件日志、旧测试、host 端点的兼容包袱。
> 落地与迁移另议（见 §9）。
>
> 取代：`docs/spec/05-authz-and-hitl.md` 的 HITL 部分、`docs/spec/07-hitl-suspend-resume.md` 的结构部分。
> **保留**：07 的核心洞见（热/冷两层、超时=降级而非失败、请求即持久化、精确重入）——本设计不推翻机制，只重划边界。
>
> 语言中立：下文用伪类型描述契约，`asyncio` 只作实现举例。三份实现（Python / TS / Java）须复现 §8 的不变式清单。

---

## 0. 为什么重做

现有实现的**机制**是想清楚了的，脏的是**边界**。四处具体病灶：

1. **provider 反向依赖 core 的具体类。** `providers/authorizer/human.py` 是整个 `providers/` 里唯一 import core 编排类的文件（其余 12 处只 import `core.utils` / `core.content` 这类纯函数）。它只用了 `HitlManager` 的 3 个方法，却绑死在一个 505 行的具体类上，且没有任何声明的契约——host 想换实现无从下手。
2. **工具 provider 层根本没有接缝。** `Authorizer` 有干净的 `defer` 接缝，但 `ToolCapabilityProvider` 够不着它，只能调 `HitlManager.wait()`——那会抛 `HitlPark`，一个 core 内部的 `BaseException`。于是任何 host 自写工具想问人，必须 (a) import core 具体类，(b) 让 core 的异常穿过自己的栈。
3. **编排层与执行栈混住，导致循环依赖。** `HitlManager` 既管账（登记、幂等、决定缓存、GC、持久化）又管栈（future、驱逐、抛 park），因此必须在函数体内延迟 import `core.loop.park` 来躲循环依赖。这个延迟 import 是边界画错的自白。
4. **Runtime 与 HitlManager 双向依赖 + 半成品窗口。** Runtime 构造 manager 后挂三个 setter（`set_cold_resolve_handler` / `set_cold_decision_lookup` / `set_content_normalizer`），manager 再回调 Runtime。构造完到接线完之间对象是半成品；实现自己承认「裸 HitlManager 从来不是生产路径」「未注入时是恒等变换」——单测跑的和生产跑的不是同一个东西。

外加两处数据建模问题：契约类型 `HitlRequest` 上挂着 core 的临时调度态（`resume_llm_account/model`，注释明写「不入事件、不持久化」）；续跑路由靠跨三个模块的魔法字符串（`form == "wait"` + `WAIT_FOR_USER_CAPABILITY_ID` sentinel）。

**根因只有一个**：「等一个人」这件事，等待权被下放给了每一个需要它的人。

---

## 1. 设计约束（已确认）

硬需求，不得牺牲：

| 能力 | 含义 |
|------|------|
| **热路径就地续跑** | 人类秒级~分钟级回复时，原调用栈原地续跑，不从持久上下文重建。零重建、零保真风险 |
| **form / outcome 开放值域** | host 可定义内建三种之外的等待形态与结局，core 原样透传、不校验 |
| **人工改写参数后放行** | approval 时人类可改写 arguments 再放行，改后参数真正生效并进审计 |
| **应答可带多模态内容** | 答复 / 备注 / 拒绝理由可带图片，需校验 + 外部化 |
| **单一 HITL 机制** | 「等人的决定」与「会话让位给用户」仍是同一个机制，但续跑策略由显式字段决定，不由 form 字符串推断 |

同时保留 07 已定的：请求即持久化（走事件，不加表）、超时是热→冷降级而非失败、精确重入（reconcile 只补 dangling 的 tool_call）、exactly-once 工具执行。

---

## 2. 核心决策：挂起是结局，不是调用

> **没有任何人 await 一个人类。需要人的一方返回一个结局；等待权收归 gateway。**

这一条决定了其余全部结构。它的价值不在于「少写几行」，而在于**让解耦成为结构性事实**：provider 层拿不到等待句柄，所以不可能耦合——不需要靠纪律维持。

契约变成（provider 侧全部是纯数据进、纯数据出）：

```
Authorizer.authorize(cap, ctx, args, tool_call_id) -> AuthzDecision

AuthzDecision =
  | Allow { message?, modified_arguments? }
  | Deny  { message? }
  | NeedsHuman { ask: HitlAsk }

ToolProvider.invoke(name, args, ctx) -> ToolOutcome

ToolOutcome =
  | Value      { content, is_error }
  | NeedsHuman { ask: HitlAsk, reply_as_result: bool }

ToolProvider.resume(ask_id, decision: HitlDecision, ctx) -> ToolOutcome   # 可选实现
```

- `NeedsHuman` 取代 `AuthorizationDecision.defer`——`defer` 只能说「挂起」，说不出「挂起并问这个问题」，所以今天的实现必须自己先去登记请求。合并成一个结局后，authorizer **退化为无状态判断**：`HumanConfirmationAuthorizer` 连 `hitl_manager` 字段都没有。
- `reply_as_result: true` 表示「人给的答复直接作为工具结果回灌，无需二阶段」。`ask_user` 走这条，因此**不需要实现 `resume`**。`resume` 只留给真正需要拿到决定后继续做事的 provider。
- provider 不感知热/冷，不认识 `HitlPark`，不 import 任何 core 符号。

**代价（如实记录）**：需要拿决定后继续执行的工具 provider 必须写成两段（`invoke` 让出 → `resume` 接回），比线性 `await` 难写。这是本设计唯一的人体工学退步，用它换 provider 层的结构性解耦。`reply_as_result` 覆盖了绝大多数实际场景，二阶段是少数派路径。

---

## 3. 分层与所有权

依赖箭头全部朝下，无回头边、无 setter 接线、无延迟 import。

```
protocols/hitl                          纯数据契约。零端口、零依赖
  HitlAsk · HitlDecision · HitlRequest(只读视图) · HitlReply · outcome 常量
        ▲
core/hitl                               自足子系统：不 import loop、不 import runtime
  HitlRegistry    纯内存状态机：登记 / 幂等 / 决定缓存 / GC。可裸测
  HitlWaiter      热等待的会合点：等待槽、超时驱逐
  HitlService     唯一漏斗：open(ask) / resolve(reply)，原子完成状态转移 + 投递分流，发事实
  ReplyIntake     应答内容校验 + 外部化（复用既有 blob 端口）
  ResumePlan      续跑策略（数据，见 §5）
        ▲
core/control/reducers                   纯函数：事件 → HitlSnapshot（pending + 所需决定）
        ▲
core/loop                               唯一碰 park 的地方
  CapabilityGateway   消费 NeedsHuman；把「被驱逐」翻译成 HitlPark
        ▲
runtime                                 组装 · 恢复时装填 registry · 订阅 HitlResolved 做续跑
        └─ ResumeCoordinator

providers/*         ──▶ 只依赖 protocols。恢复「providers 只面对契约层」的不变式
providers/events/*  ──▶ 唯一落盘的地方，且与 HITL 无关
```

### 3.1 core 不持久化、也不查询存储

区分两件事：core **可以依赖端口做 I/O**（它本来就靠 EventBus / blob / memory 端口活着），但 core **不拥有自己的持久化机制，也不反过来查存储重建自己**。

- **耐久性不是 HITL 的私事。** `HitlService` 只往 EventBus 上**发事实**；落盘是 event store provider 的事，与所有其它 core 组件一视同仁。因此 `protocols/hitl` 里**一个端口都没有**，只有数据类型。
- **恢复是「喂进来」，不是「查回去」。** 今天 `find_resolved_for_tool_call` 是两级的——内存未命中就回落去 scan 事件日志（`set_cold_decision_lookup`）。新模型：

```
事件日志 ──(reducer 纯函数折叠)──▶ HitlSnapshot ──(runtime 装填)──▶ HitlRegistry（纯内存）
                                                                        │
                                                     core 的一切查询只读自己内存，一级
```

`HitlRegistry` **完备即构造**。装填的完备性是恢复路径的责任，且集合天然有界：reconcile 只关心最后一个 assistant turn 里 dangling 的 tool_call，按这个集合折叠即可——比今天「每次未命中就全量 scan HITL 事件」更省。

- **应答内容的校验与外部化不设新端口。** `ReplyIntake` 是 core 内一个普通协作者，复用既有 blob 端口，构造期注入，不是 Runtime 挂上去的回调。

### 3.2 一处刻意的例外：热投递的会合槽

`HitlWaiter` 与 `HitlRegistry` **必须同层**（都在 `core/hitl`）。原因是原子性：热投递与冷续跑必须互斥且单一权威转移，而「状态转移」和「投递」分处两层就只能靠跨层回调或反向箭头来缝，两者都比同层更差。

因此 `HitlService.resolve()` 在一把锁内完成：状态转移 → 取走等待槽（若有）→ 判定热/冷。锁外再执行投递或发冷事实。

真正属于 loop 的只有**一件事**：把「被驱逐 / 无等待槽」翻译成 `HitlPark` 信号。这件事留在 `CapabilityGateway`。`core/hitl` 全程不认识 park、不认识协程栈语义，`core.loop.park` 的延迟 import 消失。

> 这里用了一个回调（等待槽），与「所有回调改成事实 + 订阅者」的原则不符，是**刻意例外**：它的生命周期与一次等待同寿（等待时注册、投递即取走），不是构造期接线；而且它必须在锁内被**原子地取走**——事件总线是异步、至少一次投递，做不到与状态转移原子。

---

## 4. 类型：契约态 / 内部态 / 应答态三分

今天一个 `HitlRequest` 同时扮演四个角色：host UI 契约、core 内存态、事件回放投影、core 内部信箱。于是它上面既有 host 要看的字段，也有「不入事件、不持久化」的调度临时态。拆成三个：

```
# ── provider → core（纯意图，provider 唯一需要构造的类型）
HitlAsk {
  form:      string              # 开放值域；决定 host 怎么渲染
  question:  string
  context:   string              # 展示用补充说明
  questions: list<Question>      # 结构化提问（options / multi_select）
  subject:   Subject?            # 被门控的对象：{ capability_id, arguments }，approval 用
}

# ── core → provider（人给了什么）
HitlDecision {
  outcome:            string     # accepted / rejected / cancelled / host 自定义
  message:            Content    # 文本或多模态
  modified_arguments: map?       # 仅 approval 有意义
}

# ── core → host（只读视图；host 拿它渲染 UI 与 pending 列表）
HitlRequest {
  id, form, session_id, task_id, agent_id
  subject?, question, context, questions
  created_at
  outcome?, resolved_at?         # outcome 缺省 = 未决
}

# ── host → core（一次应答的全部入参）
HitlReply {
  hitl_id
  outcome:            string
  message:            Content
  modified_arguments: map?
  resume_hint: { llm_account?, llm_model? }    # 仅本次续跑用，不入事件、不入状态
}
```

关键点：

- **`resume_llm_account/model` 找到了正确的家。** 它是「这一次应答」的属性，不是「这个请求」的属性。放进 `HitlReply` 后，契约类型上再没有一个「不持久化」的字段，`_stash_resume_llm` 这类先塞进对象再偷偷读出来的手法消失。
- **`tool_call_id` 从契约视图移除。** host 不需要它。它属于 core 内部态：

```
core 内部：PendingHitl {
  view:         HitlRequest      # 对外视图的快照
  tool_call_id: string           # 幂等键 + 决定缓存键
  resume:       ResumePlan       # 显式续跑策略，见 §5
}
```

- **`resolved` / `accepted` 仍是推导属性**，不存储（现有实现这一点是对的，保留）。
- **开放 outcome 的放行语义写死为保守默认**：只有内建 `accepted` 授予放行；host 自定义 outcome 一律**不**放行。理由：放行是安全决定，未知值必须落到拒绝侧。host 想要新的放行语义，应显式映射到 `accepted`。

---

## 5. 续跑策略显式化

今天续跑路由的判据散在三处：`control_capability.py` 定义 sentinel `WAIT_FOR_USER_CAPABILITY_ID` → `act.py` 写进请求 → `runtime.py` 用 `req.form == "wait"` 分流。form 是开放值域，却被拿来做**控制流分派**——host 定义一个语义上也是「纯文本等待」的 form，会静默落进「其它」分支拿不到正确行为。

改成请求自带的显式数据：

```
ResumePlan =
  | ReenterToolCall { tool_call_id }
      # 决定作为该 tool_call 的结果补齐 → 走 reconcile 精确重入
  | InjectUserTurn  { task_id, continuation: normal | after_interrupt | after_interrupt_edit }
      # 决定作为一条 user 消息注入任务对话 → 置 PENDING 重排
  | NoResume
      # 纯通知 / 取消，不续跑
```

映射：

| 起点 | ResumePlan |
|------|-----------|
| gateway 鉴权步（approval） | `ReenterToolCall` |
| `ask_user` 控制工具 | `ReenterToolCall`（`reply_as_result`） |
| act 纯文本暂停 / 软打断续接 | `InjectUserTurn`（`continuation` 区分 interrupt / interrupt:edit） |
| 熔断 / 会话关闭导致的取消 | `NoResume` |

两条性质：

1. **策略由发起点决定，而发起点永远在 core。** host 可以自由发明 form（那是 UI 语义），但拿不到构造 `ResumePlan` 的笔，因此**不可能破坏续跑路由**。开放扩展点与控制流安全从此正交。
2. **`context` 字符串的 sniffing 消失。** `"interrupt:edit"` 这类靠字符串比较驱动行为的地方，变成 `continuation` 枚举。

**副产品：会话暂停态也不再看 form。** 今天 `PAUSED` vs `PAUSED_HITL` 靠 `form == "wait"` 判定。新模型由 `ResumePlan` 推导：`InjectUserTurn` = 会话在等用户说话 → `PAUSED`；`ReenterToolCall` = 等一个面板决定 → `PAUSED_HITL`。语义与今天逐条等价，判据从魔法字符串换成结构。

---

## 6. 一条路径：热与冷的收敛

登记 + 等待 + park 的代码**全仓只有一段**，位于 `CapabilityGateway`：

```
resolve_human(ask, resume_plan) -> HitlDecision:        # 冷则抛 HitlPark
    id = hitl.open(ask, resume_plan, tool_call_id)      # 幂等：同 tool_call_id 复用
    if 已有终局决定: return 它                           # 决定缓存短路（跨重启同样命中）
    outcome = hitl.wait(id)                             # 热等待
    if outcome is Evicted: raise HitlPark(id)           # 唯一的 park 抛出点
    return outcome
```

三个起点共用它：

```
授权步:   authorize(...) -> NeedsHuman(ask)
          d = resolve_human(ask, ReenterToolCall(tcid))
          d 翻译成 Allow / Deny → 原流程继续（改参生效、message 回灌）

工具调用: invoke(...) -> NeedsHuman(ask, reply_as_result)
          d = resolve_human(ask, ReenterToolCall(tcid))
          reply_as_result ? 直接构造工具结果 : provider.resume(id, d, ctx)

会话让位: act 判定需让位
          resolve_human(ask, InjectUserTurn(task_id, ...))  —— 以 hot=false 开，立即走冷
```

`request_parked` 这个「登记后马上把自己的等待槽扔掉」的专用方法消失——它变成 `open` 的一个参数（`hot: false`）。同理 `wait` / `wait_for_decision` 两个方法合成一个：**驱逐用返回值表达（`Evicted`），不用异常**；异常只在 gateway 那一处由返回值翻译产生。

**竞态（驱逐 vs 应答）**：单一权威转移，在 `HitlService.resolve()` 的锁内完成——状态转移与「取走等待槽」原子。要么应答先到（命中槽 → 热投递，驱逐随后成为 no-op），要么驱逐先到（槽已空 → 应答走冷）。**驱逐本身永不触发续跑**，唯有应答才触发。这条与 07 §8 一致，只是权威点从散落的锁收拢到唯一漏斗。

---

## 7. 事件与恢复

### 7.1 两个事件，不是六个

今天有 `HitlRequired` / `HitlApproved` / `HitlModified` / `HitlAnswered` / `HitlRejected` / `HitlCancelled` / `SessionPausedHitl`。5 个 resolve 事件映到 3 个内建 outcome，是**有损投影**，且事件类型与 outcome 形成双轨——host 自定义 outcome 时无事件类型可用。收成：

```
HitlOpened   { hitl_id, form, session_id, task_id, agent_id,
               subject?, question, context, questions,
               tool_call_id, resume_plan }
HitlResolved { hitl_id, outcome, message?, modified_arguments? }
```

- **outcome 成为唯一维度**，开放值域天然被支持，不需要为自定义结局新增事件类型。
- `HitlCancelled` 并入 `HitlResolved{outcome: cancelled}`。
- **`SessionPausedHitl` 删除**：会话暂停态由 reducer 从「该 session 有无未决 HITL + 它们的 ResumePlan」推导（§5 副产品）。少一个可漂移的第二真相源。
- **`HitlOpened` 携带续跑所需的一切**（含 `agent_id` 与 `resume_plan`）。今天 `HITL_REQUIRED` 的投影没持久化 `agent_id`，导致冷重启后回复被写进空 agent scope、对装配不可见——只能在 runtime 里回退猜测（`req.agent_id or target.assigned_agent_id or target.creator_agent_id`）。事实写全，猜测消失。

### 7.2 恢复：折叠 → 装填

```
fold_hitl(events, needed_tool_calls) -> HitlSnapshot {
    pending:   list<PendingHitl>                 # 未决请求（含 resume_plan）
    decisions: map<tool_call_id, HitlDecision>   # 仅 needed_tool_calls 内的已决定
}
```

纯函数，无 I/O。runtime 在恢复时 `registry.load(snapshot)`，此后 core 只读自己内存。

### 7.3 冷续跑：事实 + 订阅者，幂等由目标动作承担

`HitlService` 发 `HitlResolved` 事实，**不认识任何续跑者**。runtime 的 `ResumeCoordinator` 订阅它，按 `resume_plan` 执行。这消掉了今天 Runtime ↔ manager 的双向依赖与半成品窗口。

代价：总线是异步、至少一次投递，因此续跑必须幂等。**幂等由目标动作承担，不由协调器记账**——记账要跨重启，就又需要一份持久状态，绕回 §3.1 想消除的东西：

- `ReenterToolCall` → reconcile 只对 **dangling** 的 tool_call 执行，已写 `TOOL_RESULT` 的不重跑。这是既有的 exactly-once 不变式，天然幂等。
- `InjectUserTurn` → 注入时以 `hitl_id` 作为该条 user turn 的幂等键，memory 层按键去重。
- `NoResume` → 空操作，幂等。

> **待确认（§9.4）**：memory 层是否已支持写入幂等键。若不支持，`InjectUserTurn` 的幂等需要补一处能力，这是本设计对外部组件的唯一新增要求。

---

## 8. 不变式清单（跨语言实现须复现）

**结构性**

- [ ] `providers/*` 不出现任何 `core` 符号的 import（`core.utils` / `core.content` 类纯函数除外）。
- [ ] `core/hitl` 不 import `core/loop` 与 `runtime`；无函数体内延迟 import 用于躲循环依赖。
- [ ] `protocols/hitl` 只含数据类型，无端口、无行为。
- [ ] `HitlRegistry` 的任何查询只读自身内存；core 中不存在从事件存储反查 HITL 的代码路径。
- [ ] 所有协作者构造期注入；无构造后 setter 接线，无「未注入时行为不同」的分支。

**行为性**

- [ ] 需要人工的一方返回 `NeedsHuman`，绝不阻塞、绝不抛 park；`HitlPark` 全仓唯一抛出点在 gateway。
- [ ] `Deny` 或未放行的 outcome ⇒ **provider.invoke 绝不被调用**（安全不变式）。
- [ ] 放行时 `modified_arguments` 生效并进审计；`message` 回灌 LLM（放行与拒绝都可带）。
- [ ] 只有内建 `accepted` 授予放行；host 自定义 outcome 一律不放行。
- [ ] 按 `tool_call_id` 幂等：同一调用重入不新建请求；已有终局则短路，不重新问人。
- [ ] 热/冷判别 = 等待槽是否存活；重启后全冷。驱逐不触发续跑，唯有应答触发。
- [ ] 驱逐与应答互斥：状态转移与取走等待槽在同一把锁内原子完成。
- [ ] resolve 幂等：已有终局的请求再应答是 no-op。
- [ ] 续跑策略只读 `ResumePlan`，不读 `form`、不读 `capability_id`、不读 `context` 字符串。
- [ ] 应答内容（含多模态）先过校验与外部化，**校验失败则原样抛给应答方**：请求保持未决、不发事件、不写 blob。
- [ ] 事件仅 `HitlOpened` / `HitlResolved`；会话暂停态由 reducer 推导，不单独发事件。
- [ ] 冷续跑幂等：重复投递同一 `HitlResolved` 不产生重复副作用。

---

## 9. 被删除的机制 · host API · 风险 · 未决

### 9.1 删除清单

| 删除 | 因为 |
|------|------|
| `HitlManager`（505 行 god object） | 拆为 Registry / Waiter / Service / ReplyIntake 四个单一职责件 |
| 三个 setter 接线 | 改构造期注入 + 事实订阅 |
| `set_cold_decision_lookup` 两级回落 | 恢复改为装填，查询塌缩成一级 |
| `request_parked` | 变成 `open(hot=false)` 的一个参数 |
| `wait` / `wait_for_decision` 双方法 | 合一；驱逐用返回值而非异常表达 |
| `AuthorizationDecision.defer` | 被 `NeedsHuman(ask)` 取代（`defer` 说不出「问什么」） |
| `WAIT_FOR_USER_CAPABILITY_ID` sentinel | 被 `ResumePlan` 取代 |
| `form == "wait"` 分派、`context` 字符串 sniffing | 同上 |
| `HitlRequest.resume_llm_account/model` | 迁到 `HitlReply` |
| 4 个 resolve 事件 + `SessionPausedHitl` | 收成 `HitlResolved` 一个 + reducer 推导 |
| host 的 approve / answer / reject 三端点 | 收成一个 `reply`（见 9.2） |

### 9.2 host API 形状

```
GET  /hitl/pending?session_id=   -> list<HitlRequest>
POST /hitl/{id}/reply             body = HitlReply
```

三端点收成一个：`outcome` 是开放值域，用三个 URL 表达等于把值域焊死在路由上，host 自定义 outcome 无处可去。

「用户直接回话」的关键词路由（首词 REJECT → 拒绝等）**降级为 host 侧适配器，不是 core 语义**。今天 spec/05 把「关键词路由仅限 approval」写成跨语言一致性条款——那是 UI 策略，不该进机制清单。host 永远不需要知道热/冷。

### 9.3 风险

| 风险 | 评估 |
|------|------|
| 二阶段 provider（`invoke` 让出 / `resume` 接回）心智负担 | 本设计唯一的人体工学退步。`reply_as_result` 覆盖多数场景，二阶段是少数派 |
| 冷续跑从同步回调改成异步订阅 | 需至少一次投递 + 幂等。已由目标动作承担（§7.3），但 `InjectUserTurn` 依赖 memory 幂等键 |
| `HitlOpened` 事件变大 | 携带 `resume_plan` 与完整 subject。换来恢复时零猜测，值得 |
| 与现有事件日志不兼容 | 本设计是从零设计，旧日志无法直接回放。若要落地须单独的迁移方案 |

### 9.4 未决问题

1. **memory 层幂等键**：`InjectUserTurn` 的幂等依赖它。需先确认现有 memory 端口是否支持；不支持则这是唯一的外部新增要求。
2. **超时阈值的归属**：热窗口时长现在是 `HitlWaiter` 的纯内存旋钮。是全局配置、按 session、还是随 `HitlAsk` 携带？倾向全局 + 可按 ask 覆盖，但未定。
3. **`NoResume` 的触发面**：仅会话关闭 / 熔断，还是也含「pending HITL 所属 task 被 reopen/replan 作废」？（07 §12 遗留的同一问题，本设计未解决）
4. **落地路径**：本文是从零设计。整体替换、还是分阶段（先切 provider 接缝、再拆 manager、最后换事件模型）？分阶段需要事件双写或兼容读，成本另算。
