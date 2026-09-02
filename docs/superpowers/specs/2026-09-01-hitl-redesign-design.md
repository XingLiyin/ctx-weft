# HITL 机制重新设计（权威设计 · 语言中立）

> 状态：设计稿，待评审。**设计本身是从零写的**——不让现有事件日志、旧测试、host 端点的形状反过来约束目标结构。
> 兼容性不进设计、只进迁移：§12.3 已定为**双读适配**，在途未决请求跨版本存活。
>
> 取代：`docs/spec/05-authz-and-hitl.md` 的 HITL 部分、`docs/spec/07-hitl-suspend-resume.md` 的结构部分。
> **保留** 07 的核心洞见：热/冷两层、超时=降级而非失败、请求即持久化、精确重入。本设计不推翻机制，只重划边界。
>
> 语言中立：下文用伪类型描述契约，`asyncio` 只作实现举例，§10 的不变式清单不依赖任何语言特性。
> 注：`docs/loomej` 那份 Java 实现已不再维护，本设计不为它承担同步义务。

---

## 0. 为什么重做

现有实现的**机制**是想清楚了的，脏的是**边界**。四处病灶：

1. **provider 反向依赖 core 的具体类。** `providers/authorizer/human.py:12` 是整个 `providers/` 里唯一 import core 编排类的文件（其余 12 处只 import `core.utils` / `core.content` 这类纯函数）。它只用到 `HitlManager` 的 3 个方法，却绑死在一个 505 行的具体类上，且没有任何声明的契约。

2. **工具 provider 层根本没有接缝。** `Authorizer` 有干净的 `defer` 接缝，但 `ToolCapabilityProvider` 够不着它（`hitl_manager.py:170` 的 docstring 自承此事），只能调 `wait()`——那会抛 `HitlPark`，一个 core 内部的 `BaseException`。于是任何 host 自写工具想问人，必须 (a) import core 具体类，(b) 让 core 的异常穿过自己的栈。

3. **编排层与执行栈混住，导致循环依赖。** `HitlManager` 既管账（登记、幂等、决定缓存、GC）又管栈（future、驱逐、park），因此必须在函数体内延迟 import `core.loop.park`（`hitl_manager.py:199`）来躲循环依赖。这个延迟 import 是边界画错的自白。

4. **Runtime 与 HitlManager 双向依赖 + 半成品窗口。** Runtime 构造 manager 后挂三个 setter（`set_cold_resolve_handler` / `set_cold_decision_lookup` / `set_content_normalizer`，`runtime.py:494-502`），manager 再回调 Runtime。构造完到接线完之间对象是半成品；实现自己承认「裸 HitlManager 从来不是生产路径」「未注入时是恒等变换」——单测跑的和生产跑的不是同一个东西。

外加两处数据建模问题：契约类型 `HitlRequest` 上挂着 core 的临时调度态（`resume_llm_account/model`，注释明写「不入事件、不持久化」）；续跑路由靠跨三个模块的魔法字符串（`form == "wait"` + `WAIT_FOR_USER_CAPABILITY_ID` sentinel）。

**根因只有一个**：「等一个人」这件事，等待权被下放给了每一个需要它的人。

---

## 1. 硬需求（已确认，不得牺牲）

| 能力 | 含义 |
|------|------|
| **热路径就地续跑** | 人类秒级~分钟级回复时，原调用栈原地续跑，不从持久上下文重建。零重建、零保真风险 |
| **form / outcome 开放值域** | host 可定义内建三种之外的等待形态与结局，core 原样透传、不校验 |
| **人工改写参数后放行** | approval 时人类可改写 arguments 再放行，改后参数真正生效并进审计 |
| **应答可带多模态内容** | 答复 / 备注 / 拒绝理由可带图片，需校验 + 外部化 |
| **单一 HITL 机制** | 「等人的决定」与「会话让位给用户」仍是同一个机制，但续跑策略由显式字段决定，不由 form 字符串推断 |

同时保留 07 已定的：请求即持久化（走事件、不加表）、超时是热→冷降级而非失败、精确重入（reconcile 只补 dangling 的 tool_call）、exactly-once 工具执行。

---

## 2. 核心决策：挂起是结局，不是调用

> **没有任何人 await 一个人类。需要人的一方返回一个结局；等待权收归 gateway。**

这一条决定其余全部结构。它的价值不在「少写几行」，而在**让解耦成为结构性事实**：provider 层拿不到等待句柄，所以不可能耦合——不需要靠纪律维持。

provider 侧契约变成纯数据进、纯数据出。**基础契约的签名一个字不改**——不问人的实现完全不受 HITL 影响：

```
Authorizer.authorize(cap, ctx, args, tool_call_id) -> AuthzDecision

AuthzDecision =
  | Allow      { message?, modified_arguments? }
  | Deny       { message? }
  | NeedsHuman { ask: HitlAsk }

ToolProvider.invoke(name, args, ctx) -> AsyncIterator<CapabilityEvent>

CapabilityEvent.kind ∈ { progress, stdout, stderr, result, error, needs_human }
                                                                 ^^^^^^^^^^^^ 新增
```

**工具侧是流式的，所以「返回一个结局」落成「产出一个事件」。** 现有契约
（`protocols/capability.py`）里 `invoke` 是 `AsyncIterator[CapabilityEvent]`，`kind` 是封闭
`Literal`。因此 provider 表达「我需要一个人」的方式是 **yield 一个 `kind="needs_human"` 的
事件**，payload 携带 `HitlAsk`；gateway 见到它即**停止消费该流**并接管等待。

两条随之而来的性质：

- 该事件**必须是流的最后一个**——gateway 收到即停止消费。provider 在它之后 yield 的任何东西
  都不会被看到，这一点要写进契约。
- 重入是**重新调用**，不是恢复一个挂起的生成器：原生成器已被关闭。`resume` 因此同样是
  `AsyncIterator[CapabilityEvent]`，而 `resume_state`（§2.2）在流式契约下不是优化而是**必需**
  ——生成器里的局部状态在关闭时就没了。

授权侧不是流式的，`NeedsHuman` 就是普通的返回值联合成员。

只有**会问人**的实现，额外实现一个可选能力接口：

```
HumanGatedAuthorizer {                                    # 授权侧
  on_decision(cap, ctx, args, tool_call_id, decision: HitlDecision) -> AuthzDecision
}

HumanResumable {                                          # 工具侧（同样是流式）
  resume(ask_id, decision: HitlDecision, resume_state, ctx) -> AsyncIterator<CapabilityEvent>
}
```

### 2.1 重入走可选接口，不污染基础签名

**加法式，不是分叉式。** 不问人的 provider / authorizer 看不到任何 HITL 相关的参数；会问人的多实现一个接口。

由此，「谁能问人」在契约上是**可见且可判定**的：

| 返回 | 要求 |
|---|---|
| `NeedsHuman(reply_as_result = true)` | 不必实现接口——答复直接作工具结果（`ask_user` 走这条） |
| `NeedsHuman(reply_as_result = false)` | **必须**实现对应接口；未实现 = 契约违例，gateway 当场报错，不静默降级 |

**为什么不分叉基类。** 一个直觉方案是拆成「需要 HITL 的基类 / 不需要的基类」。它的问题是：基类分叉会连带要求把**返回类型联合也分叉**——否则「不需要 HITL 的基类」在类型上仍然允许返回 `NeedsHuman`，非法组合只是换了个地方藏。要做干净就得维护两套返回联合，契约面积翻倍，gateway 还要按类型分派。加法式接口只加一个方法，基础契约零改动。

**热路径与冷路径依然同形**——统一的是 gateway 的调用形状，不是被调用方的方法数：

```
热：gateway 拿到 decision            → 调 on_decision / resume
冷：reconcile 走 gateway.invoke      → gateway 见 registry 已有该 tool_call 的决定 → 调同一个方法
```

被调用方知道自己正在被恢复，这是无害的；把它藏起来不是本设计的目标。

`NeedsHuman` 取代 `AuthorizationDecision.defer`——`defer` 只能说「挂起」，说不出「挂起并问这个问题」，所以今天的实现必须自己先去登记请求。合并成一个结局后，authorizer **退化为无状态判断**：`HumanConfirmationAuthorizer` 连 `hitl_manager` 字段都没有。

### 2.2 `resume_state`：让出前的工作不必重做

`HitlAsk` 带一个**不透明**的 `resume_state`。core 原样保存（随 `HitlOpened` 落盘、随 `HitlSnapshot` 装填回内存），重入时作为 `resume()` 的参数原样回传，**永不解读**。它不出现在 `invoke` 的签名里。

没有它，重入就等于要求 provider 重做让出前的全部工作——对纯判断的 authorizer 无所谓，对已经做过实际工作的工具就是重复副作用。

两条约束写死：`resume_state` 必须**可序列化**（要跨重启存活）；core 对其内容**零假设**。

### 2.3 `reply_as_result`

`HitlAsk.reply_as_result = true` 表示「人给的答复直接作为工具结果回灌」，gateway 据此直接构造工具结果，**重入根本不发生**。`ask_user` 走这条。绝大多数「问完即结果」的场景不需要重入逻辑。

---

## 3. 分层与所有权

依赖箭头全部朝下，无回头边、无 setter 接线、无延迟 import。

```
protocols/hitl                          纯数据契约。零端口、零依赖
  HitlAsk · HitlDecision · HitlRequest(只读视图) · HitlReply · Delivery · outcome 常量
        ▲
core/hitl                               自足子系统：不 import loop、不 import runtime
  HitlRegistry    纯内存状态机：登记 / 幂等 / 决定缓存 / GC / 等待槽。可裸测
  HitlService     唯一漏斗：open(ask) / resolve(reply) / cancel(id)，锁内原子转移，锁外发事实
  ReplyIntake     应答内容校验 + 外部化（复用既有 blob 端口，构造期注入）
        ▲
core/control/reducers                   纯函数：事件 → HitlSnapshot（pending + 所需决定）
        ▲
core/loop                               唯一碰 park 的地方
  HitlWaiter          热等待：创建等待句柄、超时驱逐
  CapabilityGateway   消费 NeedsHuman；把「被驱逐」翻译成 HitlPark
        ▲
runtime                                 组装 · 恢复时装填 registry · 据 resolve 返回值做续跑
  ResumeCoordinator

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

`HitlRegistry` **完备即构造**。装填的完备性从此是恢复路径的责任，且集合天然有界：reconcile 只关心最后一个 assistant turn 里 dangling 的 tool_call，按这个集合折叠即可——比今天「每次未命中就全量 scan HITL 事件」更省。

- **应答内容的校验与外部化不设新端口。** `ReplyIntake` 是 core 内一个普通协作者，复用既有 blob 端口，构造期注入，不是 Runtime 挂上去的回调。

### 3.2 等待槽：为什么它不破坏分层

热投递与冷续跑必须**互斥且单一权威转移**，这要求「状态转移」与「唤醒等待者」在同一把锁内完成。因此等待槽（rendezvous slot）存放在 `HitlRegistry` 里，而不是 loop 里。

但 `core/hitl` 不能因此认识 loop。解法：**槽里放的是一个语言级并发原语的最小抽象**——一个只有单操作 `deliver(decision) -> bool` 的不透明句柄（Python 下即 future 的薄包装）。`HitlWaiter`（loop 层）创建句柄、交给 `open()`、然后等它；`HitlService.resolve()` 在锁内取走并 `deliver`。

于是：`core/hitl` 只接触一个并发原语，不接触任何 loop 类型；箭头仍然朝下。约束写死——**槽里永远只能是这个最小句柄**，一旦塞进更丰富的 loop 对象，反向箭头就回来了。

真正属于 loop 的只有一件事：把「被驱逐 / 无等待槽」翻译成 `HitlPark`。这件事留在 `CapabilityGateway`；`core/hitl` 全程不认识 park，`core.loop.park` 的延迟 import 消失。

---

## 4. 类型：一个类拆成五个

今天一个 `HitlRequest` 同时扮演四个角色：host UI 契约、core 内存态、事件回放投影、core 内部信箱。拆分后每个类型只有一个方向：

| 类型 | 层 | 方向 |
|---|---|---|
| `HitlAsk` | protocols | provider → core |
| `PendingHitl` | core/hitl | core 内部，**不出 core** |
| `HitlRequest` | protocols | core → host（只读视图） |
| `HitlReply` | protocols | host → core（命令） |
| `HitlDecision` | protocols | core → provider |

```
HitlAsk {
  form:            string          # 开放值域；决定 host 怎么渲染
  delivery:        Delivery        # 封闭值域；决定 core 怎么续跑（§5）
  prompt:          string          # 给人看的主问题
  detail:          string          # 展示用补充说明
  fields:          list<Field>     # 结构化提问（options / multi_select）
  proposal:        map?            # 被门控的参数（approval 用）
  subject_id:      string          # 被门控的能力 id（展示与审计用）
  resume_state:    json?           # 不透明，core 永不解读（§2.2）
  reply_as_result: bool            # 答复直接作工具结果（§2.3）
}

HitlDecision {
  outcome:            string       # accepted / rejected / cancelled / host 自定义
  message:            Content      # 文本或多模态
  modified_arguments: map?
}

HitlRequest {                      # host 渲染 UI 与 pending 列表用
  id, form, session_id, task_id, agent_id
  subject_id, prompt, detail, fields, proposal
  created_at
  outcome?, resolved_at?           # outcome 缺省 = 未决
}

HitlReply {                        # 一次应答的全部入参
  hitl_id
  outcome:            string
  message:            Content
  modified_arguments: map?
  resume_hint: { llm_account?, llm_model? }    # 仅本次续跑用，不入事件、不入状态
}

core 内部：PendingHitl {
  view:         HitlRequest        # 对外视图的快照
  delivery:     Delivery
  tool_call_id: string             # 幂等键 + 决定缓存键
  resume_state: json?
  slot:         WaitHandle?        # 等待槽，热等待时非空（§3.2）
}
```

要点：

- **`resume_llm_account/model` 找到了正确的家。** 它是「这一次应答」的属性，不是「这个请求」的属性。放进 `HitlReply.resume_hint` 后，契约类型上再没有一个「不持久化」的字段，`_stash_resume_llm` 那种先塞进对象再偷偷读出来的手法消失。

- **`tool_call_id` 从对外视图移除。** host 不需要它；它是 core 的幂等键。

- **`modified_arguments` 只出现在 `HitlReply` 与 `HitlDecision` 上。** 它是决定的载荷，不是请求的属性。

- **宽结构是 form 开放的必然代价，如实承认。** 既然 form 是开放值域，就不可能做穷举的 tagged union。`HitlAsk` 因此保留一组**通用展示槽位**（prompt / detail / fields / proposal），host 自定义 form 复用同一组。真正要修的不是「字段多」，而是**私有语义混进通用槽位**——今天 `context` 一边是能力描述、一边存着 `plain_text` / `interrupt:edit` 这种续跑修饰符。后者移入 `Delivery`（§5），通用槽位就只剩展示语义。

- **`resolved` / `accepted` 仍是推导属性**，不存储（现有实现这一点是对的，保留）。

---

## 5. Delivery：form 开放，delivery 封闭

今天续跑路由的判据散在三处：`control_capability.py:46` 定义 sentinel `WAIT_FOR_USER_CAPABILITY_ID` → `act.py:644` 写进请求 → `runtime.py:1725` 用 `req.form == "wait"` 分流。form 是开放值域，却被拿来做**控制流分派**——host 定义一个语义上也是「纯文本等待」的 form，会静默落进「其它」分支拿不到正确行为（spec 05 已自承此缺陷）。

改成请求自带的显式数据：

```
Delivery =
  | ToolResult { tool_call_id }
      # 决定作为该 tool_call 的结果送达 → 热路径就地重入 / 冷路径 reconcile 精确重入
  | UserTurn   { task_id, preface: normal | after_interrupt | after_interrupt_edit }
      # 决定作为一条 user 消息注入任务对话 → 置 PENDING 重排
  | NoResume
      # 纯通知 / 取消，不续跑
```

映射：

| 起点 | Delivery |
|------|----------|
| gateway 鉴权步（approval） | `ToolResult` |
| `ask_user` 控制工具 | `ToolResult`（配 `reply_as_result`） |
| act 纯文本暂停 / 软打断续接 | `UserTurn`（`preface` 区分 interrupt / interrupt:edit） |
| 熔断 / 会话关闭导致的取消 | `NoResume` |

核心不变式：

> **host 可以定义新的等待形态（form），但不能定义新的回灌方式（delivery）。**

注意不变式的准确形态：**封闭的是值域，不是构造权**。host 自写的 authorizer 确实会构造 `Delivery`（见 §9.2），但它只能从上面三个成员里挑一个——无法发明第四种。因此无论 host 定义多少新 form，续跑路由的每一个取值 core 都认识、都有确定行为，**不可能落进「其它」分支**。开放扩展点与控制流安全从此正交。

**副产品：会话暂停态也不再看 form。** 今天 `PAUSED` vs `PAUSED_HITL` 靠 `all(r.form == "wait")` 判定（`runtime.py:1842`）。新模型由 delivery 推导：`UserTurn` = 会话在等用户说话 → `PAUSED`；`ToolResult` = 等一个面板决定 → `PAUSED_HITL`。语义与今天逐条等价，判据从魔法字符串换成结构。

---

## 6. 执行流：热与冷收敛于 gateway

登记 + 等待 + park 的代码**全仓只有一段**，位于 `CapabilityGateway`：

```
resolve_human(ask) -> HitlDecision | Evicted:
    id = hitl.open(ask, ctx)              # 幂等：同 tool_call_id 复用既有请求
    if 已有终局决定: return 它             # 决定缓存短路（重启后同样命中，因已装填）
    handle = waiter.new_handle(id)        # 注册等待槽
    return waiter.await(handle, timeout)  # 超时 → 驱逐槽 → Evicted
```

`Evicted` 是 core 内部的哨兵值，不出现在任何契约类型里——provider 永远见不到它，它在 gateway 内当场被翻译成 `HitlPark`。

三个起点共用它：

```
授权步:      authorize(...) -> NeedsHuman(ask)
             → d = resolve_human(ask)
             → Evicted ? raise HitlPark : authorizer.on_decision(..., decision=d)

工具调用:    invoke(...) -> NeedsHuman(ask)
             → d = resolve_human(ask)
             → Evicted ? raise HitlPark
             : ask.reply_as_result ? 直接构造工具结果
                                   : provider.resume(ask_id, d, resume_state, ctx)

act 暂停:    hitl.open(ask with UserTurn) 后不等待，直接抛 HitlPark（冷 park）
```

**热/冷的单一权威转移**在 `HitlService.resolve()` 的一把锁内完成：状态转移 → 取走等待槽 → 判定热/冷。锁外再投递或发事实。

- 应答先到 → 命中槽 → 热投递；随后的驱逐成为 no-op。
- 驱逐先到 → 槽已空 → 应答走冷。
- **驱逐本身永不触发续跑**，唯有应答才触发。

这条与 07 §8 语义一致，只是权威点从散落的锁收拢到唯一漏斗。重启后槽全空 ⟹ 一切皆冷，与今天一致。

超时旋钮语义不变：纯内存/存活旋钮（热窗口多久后驱逐），与 UX 无关。

---

## 7. 事件与恢复

### 7.1 事件从 8 个收敛到 2 个

```
HitlOpened   { hitl_id, form, delivery, subject_id, prompt, fields, proposal,
               tool_call_id, task_id, agent_id, resume_state }
HitlResolved { hitl_id, outcome, message_ref, modified_arguments, claimed }
```

今天是 `HitlRequired` + `SessionPausedHitl` + 5 个 resolve 事件（Approved / Modified / Answered / Rejected / Cancelled）。那 5 个事件映到 3 个内建 outcome，所以代码里得写「outcome 是事件的有损投影」。新模型把它反过来：**outcome 是事实本身**，事件类型不再编码结局——approved vs modified 由 `modified_arguments` 是否存在推出，其余由 outcome 推出。host 自定义 outcome 因此不需要新增事件类型才能表达。

`SessionPausedHitl` 一并删除：会话暂停态是 pending 集合的**推导投影**，不是独立事实（§5 副产品）。「事件与状态两处维护」的不变式随之消失。

`claimed` 记录该次应答是否已被热投递消费，供 `ResumeCoordinator` 判定是否需要冷续跑。

### 7.2 恢复

```
reducer（纯函数）折叠 HitlOpened / HitlResolved
   → HitlSnapshot {
       pending:       [...],
       decisions_for: { tool_call_id → (HitlDecision, resume_state) }
     }
   → runtime 装填 HitlRegistry
```

`decisions_for` 只需覆盖**最后一个 assistant turn 里 dangling 的 tool_call**——有界，且是 reconcile 唯一会问的集合。

**决定必须与 `resume_state` 成对装填**：冷路径重入调的是 `resume(ask_id, decision, resume_state, ctx)`，只带决定而丢掉 `resume_state`，provider 就得重做让出前的工作——正是 §2.2 要消除的重复副作用。`resume_state` 来自同一 `hitl_id` 的 `HitlOpened`，折叠时一并取出。

### 7.3 冷续跑：返回值驱动，**不挂总线订阅**

> **订正（2026-09-01，出段 2 计划时核对实现发现）**：本节原设计为「`ResumeCoordinator` 订阅
> `HitlResolved`」。核对 `providers/events/bus/in_process/bus.py` 后**推翻**：该总线的 handler
> 订阅者是**在 `emit()` 内部同步 drain** 的，且队列满时**丢弃最旧事件**（`QueueFull` 分支）。
> 于是订阅式冷续跑有两个致命性质：①「人答了但事件被丢」⟹ 会话永不续跑，正是本设计要消灭的
> 故障类；② `recover_session` 会在 host 应答的调用栈里内联跑完。事件总线适合**广播事实**，
> 不适合承载**控制流关键信号**。

`HitlService` 仍然不认识 Runtime——但这个目标由**返回值**达成，而不是靠订阅：

```
HitlService.resolve(reply) -> PendingHitl | None      # 已终局 → None
```

冷续跑由**组合根**（runtime 的应答入口）驱动：它调 `resolve`，拿到已终局的请求，按 delivery
决定续跑方式。`HitlService` 依然只发事实、不认识任何人；依赖方向依然单向。区别只在于「谁来
接这个结果」——是调用者自己接，而不是注入一个回调、也不是挂一个可能丢事件的订阅。

这与旧实现的 `set_cold_resolve_handler` 有本质区别：那是**构造期注入的回调**（双向依赖 +
半成品窗口），这是**调用点的返回值**（单向，且 `HitlService` 可以脱离 runtime 单测）。

`claimed` 字段仍然进事实——它是重放时判断「这次应答当时是热是冷」的依据，只是不再由订阅者消费。

续跑分流：

```
resolve() 返回已终局请求 且 claimed=false → 按 delivery 分流
    ToolResult → recover_session(resumed_task_id) → reconcile 精确重入
    UserTurn   → 注入 user 消息 + 置 PENDING + 重排
    NoResume   → 无动作
```

**冷续跑仍必须幂等**，尽管不再有至少一次投递的问题——因为应答入口本身可能被重试（host 超时重发、
用户连点两次），而 `resolve` 的幂等只保证「不二次转移」、不保证调用方不会拿着同一个已终局请求
再续跑一次。幂等由目标动作承担，不由协调器记账（记账要跨重启，就又需要一份持久状态，绕回 §3.1
要消除的东西）：

- `ToolResult`：reconcile 本就按「该 tool_call 是否已有 TOOL_RESULT」判定，天然幂等。
- `UserTurn`：注入时把 `MemoryEvent.id` 设为**由 `hitl_id` 确定性派生**的值（如 `hitlreply:{hitl_id}`），重复投递即 no-op。**这一能力已经具备，无需新增**——见 §12.2 的核实记录。

#### 7.3.1 崩溃窗口：恢复期必须重排「挂在已终局 HITL 上」的任务

返回值驱动挡住的是**丢事件**，挡不住**丢进程**：决定已落盘、进程在续跑之前崩了，那次续跑就没了。
不管它的话，人已经答过的会话永远停在 `SUSPENDED`——症状与被丢事件时一模一样。

因此恢复期的判据不是「这个任务有没有 HITL」，而是**它挂着的 HITL 终局了没有**：

| 任务挂着的 HITL | 恢复时 |
|---|---|
| 未决（还在等人） | **保持 parked、不重排**。人还没答，任务绝不能自己跑起来 |
| 已终局 | **重排**。决定已经在了，续跑没跑成，补跑一次 |

**「宁可重排一次」是安全的**，因为两条续跑路径本身都幂等（上面两条）。所以恢复期**不需要**记
「这次续跑到底跑没跑过」——那笔账要跨重启，就又需要一份持久状态，绕回 §3.1 想消除的东西。

这条对**装填完备性**提出了第二个要求：`resolved_for_session` 只能看见被装填进 registry 的已终局
请求，因此装填集合的口径必须覆盖它。若某个已终局请求没被装进来，兜底就失效。

### 7.4 内容

事件里的 message 恒为 ref 或文本、**不含字节**（今天已经做对的地方，保留）。memory 侧与 event 侧各自外部化到各自的 blob，由 `ReplyIntake` 一处统一产出两份载荷；两个 ref 不必相同。校验失败 → 原样抛给应答方，请求保持未决、不发事实、不写 blob（与其余两个内容入口一致：入口即拒、不落库）。

---

## 8. host API

- 读：`GET /hitl/pending` → `HitlRequest[]`
- 写：**收敛成一个端点** `POST /hitl/{id}/reply`，收 `HitlReply`

今天的 approve / answer / reject 三个端点本质是三个「动作」，但结局其实是 `outcome + 载荷` 的组合。合成一个命令后，host 自定义 outcome 不必为每个新值加端点。

首词关键词路由（`"no, 先列目录"` → `outcome=rejected, message="先列目录"`）留在 host 侧，core 不做——spec 05 已定，保留。注意该路由**仅适用于 approval 语义的 form**；`question` / `wait` 的文字回复一律是答复，`"no"` 是一个否定答复而非拒绝。

`cancel` 不是 host 动作，是 core 的收口（session 关闭 / 熔断取消），走 `HitlService.cancel`，终态、不 requeue、已解决则幂等 no-op。

host 侧净简化：不再需要知道 `was_hot`，也不需要知道 delivery。

---

## 9. 扩展指南（host 侧）

### 9.1 装配

```
# 今天：host 必须把 core 的实例递给 provider
HumanConfirmationAuthorizer(hitl_manager=runtime.hitl_manager)   # cli.py:95 / main.py:28

# 新设计：authorizer 零依赖
registry.set_authorizer("shell:*", HumanConfirmationAuthorizer())
```

**host 再也拿不到 HITL 的把手**——这是解耦成为结构性事实的直接体现。

### 9.2 自定义 Authorizer

```
class SpendLimitAuthorizer(Authorizer, HumanGatedAuthorizer):
    async def authorize(self, cap, ctx, args=None, *, tool_call_id=""):
        amount = (args or {}).get("amount", 0)
        if amount <= self.limit:
            return Allow()
        return NeedsHuman(HitlAsk(                  # 让出
            form="spend_approval",                  # host 自定义 form
            delivery=ToolResult(tool_call_id),
            prompt=f"批准 ${amount} 的支出？",
            proposal=args,
        ))

    async def on_decision(self, cap, ctx, args, tool_call_id, decision):
        # 由**我**来解释这个决定
        if decision.outcome == "accepted":
            return Allow(message=decision.message,
                         modified_arguments=decision.modified_arguments)
        return Deny(message=decision.message)       # 未知 outcome 落此分支 = 不放行
```

不需要问人的 authorizer（`AllowAll` / `AllowList`）只实现 `Authorizer`，签名里看不到任何 HITL 概念。

三处性质：

1. **authorizer 不再自己查决定缓存。** 今天 `human.py:32` 第一件事是 `find_resolved_for_tool_call(...)`——provider 在替 core 做记账查询。新设计里决定是喂进来的，authorizer 完全无状态、无查询、无 I/O，可纯函数式单测。
2. **热/冷同形。** 热路径由 gateway 调 `on_decision`，冷路径由 reconcile 经 gateway 调同一个方法。
3. **安全不变式不依赖 outcome。** gateway 是否调 `provider.invoke`，只取决于 `on_decision` 返回的 `Allow/Deny`。

### 9.3 自定义工具 provider 要问人

工具侧是**流式**的（`invoke` 是 `AsyncIterator[CapabilityEvent]`），所以让出是 yield 一个事件，
而不是 return 一个值：

```
class DeployTool(ToolCapabilityProvider, HumanResumable):
    async def invoke(self, cap, args, ctx):
        plan = await self.compute_plan(args)              # 有代价的工作
        yield CapabilityEvent("progress", {"text": "plan computed"})
        yield CapabilityEvent("needs_human", {"ask": HitlAsk(
            form="question",
            delivery=ToolResult(ctx.tool_call_id),
            prompt=f"确认部署 {plan.summary}？",
            resume_state=plan.to_dict(),                  # 让出前的工作存这里
        )})
        # 到此为止：gateway 见 needs_human 即停止消费，其后 yield 的东西不会被看到。

    async def resume(self, ask_id, decision, resume_state, ctx):
        plan = Plan.from_dict(resume_state)               # 不必重算
        yield CapabilityEvent("result", {"text": await self.apply(plan, decision)})
```

注意 `resume_state` 在流式契约下**不是优化而是必需**：让出时生成器被关闭，它的局部变量
（这里的 `plan`）随之消失，重入是重新调用而非恢复挂起的生成器。

若某 provider 返回了 `NeedsHuman(reply_as_result=false)` 却没实现 `HumanResumable`，gateway **当场报错**——这是契约违例，不静默降级成「把答复当结果」，否则一次未完成的部署会被伪装成已完成。

### 9.4 自定义 form 与自定义 outcome

host 定义 `form="spend_approval"` 之后要做的**只有 UI 一件**：据 form 渲染自己的审批卡片，回复时给出自己的 outcome（如 `escalated`）。

core 对这两个新值的处理是**完全不处理**：`form` 只用于透传给 host 渲染；`outcome` 只用于判定「是否已终局」（非空即终局），然后原样喂给发起方去解释。

> **解释 outcome 的权力归发起方；core 只判「是否终局」。**

安全默认因此是「未知 outcome 不放行」，而这个默认由发起方显式写出（9.2 的 `else → Deny`），不是 core 偷偷替它决定的。

### 9.5 唯一一处 core 必须碰 outcome：注入文案

`UserTurn` 冷续跑时要把回复注入对话，今天会给拒绝加 `Human declined: ` 前缀。发起方（`act`）此时早已不在栈上，无法由它渲染。规则定死：

> **core 对 outcome 的分支只允许出现在「呈现」上，永不出现在「控制流」上。**

即：内建 `rejected` 注入时加默认前缀，其余（含全部 host 自定义 outcome）一律**原样注入**。控制流——放不放行、续不续跑、怎么续跑——只看 `resolved` 与 `delivery`，永不看 outcome。

### 9.6 边界：host 不能凭空发起 HITL

**不支持，且这是正确的边界。** HITL 的意义是「暂停某段正在执行的东西，等人，再续跑它」。host 自己的业务流程没有被 core 暂停，也就没有续跑目标，`delivery` 无从填写。允许凭空 `open()` 一个 `NoResume` 的请求，这个机制就退化成一张待办事项表——那是 host 自己该有的东西。

> **HITL 不是审批中心，是执行的暂停点。**

相邻但不同的需求确实存在：host 想**打断**一个在跑的 session 并插问一句。那是 `UserTurn` delivery 的 ask，由 interrupt 路径发起——机制现成，但入口属于 interrupt，不属于 HITL 的扩展面。本版不做（§12.1）。

---

## 10. 不变式清单（任何实现须复现）

- [ ] **provider 层零 core 依赖**：`providers/` 不 import 任何 core 编排类；provider 不感知热/冷、不认识 park。
- [ ] **park 只有一个抛出点**：`CapabilityGateway`（含 act 的显式冷 park）。`core/hitl` 不认识 park。
- [ ] **core 不查存储**：HITL 的一切查询只读内存 registry；恢复靠装填，不靠回落扫日志。
- [ ] **单一权威转移**：状态转移与取走等待槽在同一把锁内；热投递与冷续跑互斥、不双投；驱逐本身永不触发续跑。
- [ ] **重启后一切皆冷**：等待槽全空。
- [ ] **恢复期按 HITL 是否终局分流任务**：挂在未决 HITL 上的保持 parked、不重排；挂在**已终局** HITL 上的**必须重排**（§7.3.1 的崩溃窗口兜底）。前者错了 = 人没答任务就跑；后者错了 = 人答了会话永不醒。
- [ ] **幂等 open**：同 `tool_call_id` 再次 open 复用既有请求，不新建；已终局则直接返回决定，不建槽。
- [ ] **幂等 resolve**：已终局的请求再次 resolve 是 no-op，不二次转移、不重发事实。
- [ ] **幂等冷续跑**：重复投递同一 `HitlResolved` 不产生重复副作用（`ToolResult` 靠 reconcile 天然幂等；`UserTurn` 靠 `hitl_id` 幂等键）。
- [ ] **安全不变式**：`Deny` 时 `provider.invoke` 绝不被调用；`Allow` 时 `modified_arguments` 生效且过脱敏，审计与 memory 记录用实际执行的有效参数。
- [ ] **控制流不看 outcome**：core 仅判「非空 = 终局」；outcome 的语义解释归发起方；core 对 outcome 的分支只出现在呈现层。
- [ ] **delivery 封闭、form 开放**：host 不能定义新的 delivery；form / outcome 原样透传不校验。
- [ ] **`resume_state` 不透明**：core 永不解读，且可序列化、跨重启存活；冷路径装填时必须与决定成对取回。
- [ ] **基础契约不含 HITL 参数**：`authorize` / `invoke` 的签名不因 HITL 而变；重入只经可选接口。声明 `NeedsHuman(reply_as_result=false)` 却未实现对应接口 = 契约违例，**当场报错**，不静默降级。
- [ ] **`needs_human` 事件是流的终点**：gateway 收到即停止消费该流，其后的 yield 一律不可见；`resume` 是重新调用而非恢复生成器，故 `resume_state` 承载让出前的全部状态。
- [ ] **内容**：事件载荷恒不含字节；memory 侧与 event 侧各自外部化；校验失败则不改状态、不发事实、不写 blob。
- [ ] **事件**：仅 `HitlOpened` / `HitlResolved`；会话暂停态由 pending 集合推导而非独立事件。

---

## 11. 对照今天，被删除的机制

| 今天 | 新设计 |
|---|---|
| provider → `core.orchestrator.hitl_manager` 的 import | 0 |
| `core/hitl` → `core.loop.park` 的函数体内延迟 import | 0（park 只在 gateway 抛） |
| 三个 setter 注入（cold_resolve / cold_lookup / content_normalizer） | 0，全部构造参数或返回值 |
| `find_resolved_for_tool_call` 两级回落（内存 → scan 事件日志） | 一次内存查询 |
| `request` / `request_parked` 双入口 | 一个 `open`，是否热等由是否注册等待槽决定 |
| `wait` / `wait_for_decision` 双出口（一抛一返 None） | 一个 `await`，park 只在 gateway 抛 |
| `AuthorizationDecision.defer` + 先登记后 defer 的两步 | 一个 `NeedsHuman(ask)` 结局 |
| `form == "wait"` 字面量判定 ×2 + `WAIT_FOR_USER_CAPABILITY_ID` sentinel | `Delivery`，封闭值域 |
| `context` 存 `interrupt:edit` 等续跑修饰符 | `UserTurn.preface` 枚举 |
| `HitlRequest` 一个类四种角色 | 五个单向类型 |
| `resume_llm_account/model` 挂在契约类型上 | `HitlReply.resume_hint` |
| 8 个事件类型 | 2 个 |
| approve / answer / reject 三个 host 端点 | 一个 `reply` |
| 505 行的 `HitlManager` | 4 个各自可裸测的小件 |

---

## 12. 代价与开放问题

### 12.1 已知代价

| 代价 | 说明 |
|---|---|
| **重入式 provider** | 需要拿决定后继续做事的工具 provider，要把逻辑拆成 `invoke` / `resume` 两段并自带 `resume_state`，比线性 `await` 难写。代价**只落在会问人的实现身上**——基础契约不变，`reply_as_result` 又覆盖了多数场景，所以这是少数派路径的少数派 |
| **冷续跑由应答入口驱动** | 组合根拿 `resolve` 的返回值分流（§7.3 订正：不挂总线订阅——该总线内联投递且背压下丢事件）。仍须幂等以防应答重试：`ToolResult` 靠 reconcile 天然幂等；`UserTurn` 靠 `MemoryEvent.id` 派生键，该能力已具备（§12.2） |
| **装填完备性成为恢复路径的责任** | core 不再兜底扫日志，恢复时漏装 = 重问一遍已答过的问题。需要针对性回归测试 |
| **delivery 封闭是明确取舍** | 若将来出现真正的第三种回灌方式（如「决定只改配置、不回灌给任何对话」），要改 core。现在封闭是对的，但这是一个会回来找我们的决定 |
| **host 不能凭空发起 HITL** | 见 §9.6。若确有需求，应走 interrupt 入口而非扩展 HITL |

### 12.2 已核实：memory 幂等键无需新增（2026-09-01）

`UserTurn` 冷续跑的幂等依赖「按调用方给定的 id 去重写入」。**该能力已存在，本设计对外部组件零新增要求。**

- 契约：`MemoryEvent.id`（`protocols/memory.py:175`）——「给定 → provider 必须采用并按 id 幂等（重复 ingest = no-op）；None → provider 生成」。
- 实现：SQL provider `providers/memory/sql/provider.py:272`（已存在即 no-op，且明确「不比对内容、不推进计数器」）；in-memory provider `providers/memory/in_memory/provider.py:91-94` 同语义。

两条使用约束：

1. **id 命名空间是全局的**，不按 tenant 隔离（`_id_exists` 的注释：「契约第 5 条：id 命名空间仍是全局的」）。派生 id 因此必须全局唯一——`hitl_id` 本身即全局唯一，加前缀后仍然唯一，满足。
2. **排序不依赖 id**：读取按 `timestamp, seq_no` 排序（`provider.py:427`），所以派生 id 不必保持 ULID 的可排序形状，可以用可读前缀。

### 12.3 迁移：双读适配（已定）

两条已确认的前提决定了迁移形态：

- **在途未决请求必须跨版本存活**——不能靠「升级前 drain 干净」回避。因此旧事件的双读**是硬要求**，不是可选的保险。
- **loomej（Java 实现）已不维护**——迁移只覆盖 ctx-weft，不承担跨语言同步。

#### 12.3.1 形态：三段，中间态不允许两套 HITL 共存

| 段 | 内容 | 性质 |
|---|---|---|
| **1 · 纯新增** | 新契约类型 + reducer 双读（旧 8 类事件 → `HitlSnapshot`） | 不碰任何执行路径，可独立发布、可单测 |
| **2 · 原子替换** | `core/hitl` + gateway 接管；provider 契约切换；旧 `HitlManager` 删除 | **不留开关**，一次切完 |
| **3 · 收口** | host 端点合一；双读退役（见 12.3.6） | 依赖闸门条件 |

段 2 不留开关是刻意的：两套 HITL 并存会引入一批**只在过渡期存在**的不变式（pending 归属哪一套、host 该问谁、事件写哪种格式），测了也是白测，而它们出错的方式是**静默丢掉用户已给的回复**。宁可要一次短的原子替换。

#### 12.3.2 旧事件 → 新模型的折叠规则

`HitlRequired` → `HitlOpened`：

| 新字段 | 来源 |
|---|---|
| `form` | `form`（缺省 `"approval"`，与现 reducer 一致） |
| `subject_id` | `capability_id` |
| `prompt` / `detail` | `question` / `context` |
| `proposal` / `fields` | `arguments` / `questions` |
| `tool_call_id`、`task_id`、`agent_id` | 同名字段（`agent_id` 在现 reducer 中确实保留，见 `reducers.py:67`） |
| `resume_state` | `null`（旧模型无此概念） |
| `delivery` | **反推，见下** |

**`Delivery` 的反推必须复刻旧的「判据」，而不是旧的「意图」**：

```
form == "wait"             → UserTurn { task_id, preface: plain_text     → normal
                                                        interrupt      → after_interrupt
                                                        interrupt:edit → after_interrupt_edit }
else if tool_call_id 非空  → ToolResult { tool_call_id }
else                       → NoResume + 告警；host 侧只允许取消
```

直觉上更该用 `capability_id == "control:wait_for_user"` 这个 sentinel 来反推——它是 `act.py:639` 的唯一写入点，语义上更精确。**但那是错的**：今天 runtime 的实际分流判据是 `req.form == "wait"`（`runtime.py:1725`）。若某条旧请求 `form == "wait"` 而 `capability_id` 不是 sentinel，它今天会被注入；改用 sentinel 反推就会把它变成 `ToolResult`——**迁移本身改变了在途请求的行为**。迁移的正确性判据是「与升级前逐条同构」，不是「更符合新设计的意图」。

第三条分支是兜底：既非 `wait`、又无 `tool_call` 可补，续跑无从谈起。让它显式地「只可取消」并告警，好过静默丢掉一条用户正在等的请求。

resolve 类事件 → `HitlResolved`（无损）：

| 旧事件 | `outcome` | 附加 |
|---|---|---|
| `HitlApproved` | `accepted` | — |
| `HitlModified` | `accepted` | `modified_arguments` |
| `HitlAnswered` | `accepted` | `message_ref` |
| `HitlRejected` | `rejected` | `message_ref` |
| `HitlCancelled` | `cancelled` | `message_ref` |

`claimed` 一律折为 `false`：旧事件不记录热/冷，而恢复语境下本就一切皆冷（§6），`false` 是唯一安全值。

`SessionPausedHitl` 折叠时**丢弃**——新模型里会话暂停态由 pending 集合推导（§7.1），旧事件不再是真相。

#### 12.3.3 必须原样继承的保守规则

现 `fold_cold_hitl_decision` 有一条「**可用决定**」规则：`Answered` 必须带 `message`、`Modified` 必须带 `modified_arguments`，否则视为**没有可用决定**，请求按仍未决处理、重新问，**绝不臆造答案**。旧日志里确实存在这类残缺事件。

双读折叠必须逐字继承这条规则。这是整个迁移里最容易被「顺手简化」掉的一处，而代价是丢掉用户已经给过的回复。

同样要继承的还有 event-side ref 的还原路径：旧决定里的 `message` 可能是指向 **event blob store** 的 ref，回放时需转换才能作 memory 侧内容使用；转换失败时降级为文本占位（现 `runtime.py:1960` 的行为），不得抛错卡住恢复。

#### 12.3.4 host 端点兼容层

三个旧端点在 **host 侧**转译成统一的 `HitlReply`，core 不需知情：

```
POST /hitl/{id}/approve {message, modified_arguments}  → Reply{outcome: "accepted", ...}
POST /hitl/{id}/answer  {text}                          → Reply{outcome: "accepted", message: text}
POST /hitl/{id}/reject  {message}                       → Reply{outcome: "rejected", message}
```

成本近似为零，可长期保留，不必与双读同期退役。

#### 12.3.5 provider 契约：没有兼容路径

老式 provider 的形态是 `await hitl_manager.wait(...)`——它要的是一个**等待句柄**，而新契约里这个东西根本不存在，没有适配器能凭空造出来。因此：

- 仓内只有一处（`providers/authorizer/human.py`），随段 2 一起改写。
- host 自写的 authorizer / 工具 provider **必须改**，改不了就不能升级。这一条要在升级须知里写在最前面，不能藏在附录里。

#### 12.3.6 双读的退役闸门

写成明确条件，不是「以后再说」：

> 当且仅当**升级前产生的全部未决 HITL 请求均已终局**，且不再有会话需要回放到升级点之前的日志段时，双读折叠可以删除。

实践上给两个可核对的信号：升级点之前的 `HitlRequired` 全部有对应终态事件；且这些 session 均已归档 / 超出最长存活期。段 3 前先核对，核对不过就继续留着——留着的成本只是一份纯函数。

#### 12.3.7 明确不做：重写事件日志

另一条路是跑一次迁移作业，把旧 8 类事件改写成新 2 类，迁移完零负担。**不采用**，理由：

- 它**改写 append-only 日志**，即变更真相源，失败不可回滚；
- 既有快照（`providers/events/snapshot.py` 的 `serialize_view`）会随之失效，须一并重建，工作量与风险都翻倍。

为省一份纯函数去承担真相源被改坏的风险，不划算。

#### 12.3.8 迁移的验收

双读折叠是纯函数，因此可以被逐条锁死：

- 用**真实旧日志的 golden 夹具**覆盖：approval 已批 / 已改参 / 已拒 / 残缺 Answered / 残缺 Modified / wait 的三种 `context` / 无 `tool_call_id` 的非 wait 请求（兜底分支）。
- 断言口径是**与升级前逐条同构**：同一份旧日志，旧路径与新路径解出的「该不该重问、该怎么续跑、放不放行」必须一致。
- 段 2 上线前，跑一遍全量恢复回归——`restore` 是所有会话恢复都走的路径，HITL 只是它的一个原因（07 §14 已把这里标为最高风险面）。
