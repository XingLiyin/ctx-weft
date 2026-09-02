# 07 · HITL 挂起与续跑（2026-09-01 重设计后的现状）

> 真相源：`core/hitl/`（registry / service / reply_intake）、`core/loop/hitl_waiter.py`、
> `core/loop/capability_gateway.py`、`core/loop/steps/reconcile.py`、`core/runtime.py`
> 权威设计：`docs/superpowers/specs/2026-09-01-hitl-redesign-design.md`（下称**设计文档**）
> 升级须知：`docs/upgrade/2026-09-01-hitl-redesign.md`

**本文档已按 2026-09-01 重设计重写。** 早前版本描述的 `HitlManager`
（`request` / `wait` / `approve` / `answer` / `reject`、`_futures` 热冷判别、
`AuthorizationDecision.defer`、`on_cold_resolve` 回调、`pending_hitl` 投影）**已全部删除**，
没有兼容路径。机制的完整推导在设计文档，本文档只写「跑起来是什么样」，不再复述设计。

---

## 1. 机制一句话

**Provider 声明需要人，然后返回；等待权归 gateway；应答只有一个入口。**

- authorizer 返回 `AuthorizationDecision(allowed=False, needs_human=HitlAsk(...))`；
  流式工具 provider `yield CapabilityEvent("needs_human", {"ask": HitlAsk(...)})`——
  **`needs_human` 是流的最后一个事件**，gateway 见之即停止消费并显式 `aclose()` 该流。
- gateway 是**全仓唯一**登记 + 等待 + 抛 park 的地方（`CapabilityGateway._resolve_human`）。
- host 应答走**唯一入口** `CtxWeftRuntime.reply_to_hitl(HitlReply)`；
  未决列表走 `CtxWeftRuntime.list_pending_hitl(session_id=None) -> list[HitlRequestView]`。

设计文档 §2 / §3 / §6。

## 2. 热与冷

`HitlWaiter.wait(hitl_id)` 挂一个等待槽等应答：

- **热**：应答在窗口内到达并被这个槽消费（`claimed=True`）→ `wait()` 返回决定，
  被 park 的协程就地续跑。
- **冷（驱逐）**：窗口超时、或请求在挂上槽之前就已终局 → `wait()` 返回 `None`（**不抛**），
  gateway 把它翻译成 `HitlPark`，任务落 `SUSPENDED`，请求**保持未决**。

**判据是 `claimed`，不是 `resolved`。** `claimed` 由 `HitlService._commit` 在 `registry.resolve()`
取走等待槽的同一原子段里写入，是「热投递赢了这次终局」的唯一权威。`wait()` 的入口守卫与超时
分支都用它——只看 `resolved` 会让一次应答**同时**驱动热续跑与冷续跑。

`reply_to_hitl` 据同一个 `claimed` 分流：True → 已就地续跑，返回；False → 触发冷续跑。
**冷续跑由返回值驱动，不挂总线订阅**（设计文档 §7.3）。

## 3. 续跑路由：按 `delivery`，不按 `form`

`form` 是开放值域（host 可自定义）；`Delivery` 是**封闭**联合，续跑路由只认它：

| delivery | 续跑动作 |
|----------|----------|
| `ToolResultDelivery(tool_call_id)` | 热：就地重入；冷：`recover_session` → reconcile 精确重入 |
| `UserTurnDelivery(task_id, preface)` | 把答复作一条 user 消息注入 task 对话并重排 |
| `NoResumeDelivery` | 纯通知 / 取消，不续跑 |

**面板提示**（前端要不要出一块要人拍板的面板）由**未决请求的 delivery** 推导，同样不看
form：全是 `UserTurnDelivery`（软待命，无面板）→ `"PAUSED"`，其余 → `"PAUSED_HITL"`。
判据只有一份：`core/hitl/status.py::paused_status_for`，唯一消费方是 host 只读入口
`CtxWeftRuntime.session_status_after_recover`。

> **这两个字面量不是会话状态。** 会话状态的值域自 2026-09-02 起不含 `PAUSED` /
> `PAUSED_HITL`——「等的是面板还是一句话」是 delivery 的性质，`HitlOpened` 已经载着它到了
> 前端，会话状态再复制一份只会失步。会话级只回答「停着且正常」= `WAITING`。

## 4. 精确重入：dangling tool_call 对账

被 park 时，memory 里是「assistant turn 含 N 个 tool_call + 仅已执行调用的 TOOL_RESULT」。
`ReconcileStep`（resume 后、任何 LLM turn 之前）按原顺序处理最近一个 assistant turn：

```
先 resolve_and_bind(state, ctx)           # reconcile 跑在 prepare 之前，否则命中空 cache
对每个 tool_call：
  已有 TOOL_RESULT → 跳过（复用持久结果，不重跑）
  dangling         → gateway.invoke(同一 tool_name / 同一原始 arguments / 同一 tool_call_id)
                     授权步：命中决定缓存 → 不再问人，直接把决定喂回 authorizer.on_decision
                     工具步：provider 重跑到 yield needs_human 时命中缓存 → 转入 resume()
→ next_step = "prepare"
```

`gateway.invoke` **恒为唯一 TOOL_RESULT 写入点**（热=原 invoke 写，冷=reconcile 经同一 invoke 写）。

**决定缓存的键是四维**：`(session_id, tool_call_id, stage, invocation_key)`。
`stage` 区分授权步与工具步（否则授权步会吃掉工具阶段的答复）；`invocation_key`（工具名 +
原始参数摘要，`capability_gateway.invocation_key()`）区分**同一 tool_call id 下的另一次调用**
——模型复用 `call_1` 这类短 id 是常态，少了这一维，第 3 轮的批准会替第 9 轮的另一次调用开门。
key 随 `HitlOpened` 落盘、随折叠装填回来；旧事件折出来的记录 key 为 `""`，**通配**（迁移期
行为逐条同构）。

### exactly-once

授权步的 park 严格发生在 `provider.invoke` **之前**，故被门控工具要么热执行、要么 reconcile
执行，二者互斥，副作用恰好一次。**工具步不然**：provider 已经跑到 `needs_human` 才让出，冷路径
会把 `invoke` 从头再跑一遍——所以 `needs_human` 之前的工作必须**幂等或便宜**，不可重复的副作用
只能放进 `resume()`（设计文档 §2.2 / §9.3）。

`resume_state` 因此只省掉**热**重入：冷路径上 `resume()` 收到的是 provider 刚刚重新 yield 的
那一份，落盘装填回来的那一份被丢弃。

## 5. 竞态：驱逐 vs. 应答

`HitlRegistry` **全同步、无锁**——单线程 asyncio 下一段没有 `await` 的代码原子执行，
`registry.resolve()` 在同一原子段里完成「状态转移 + 取走等待槽」，热投递与冷续跑天然互斥。
驱逐本身**永不触发续跑**，唯有应答才触发。

## 6. 持久化与跨重启

**core 不持久化、也不查询存储**（设计文档 §3.1）。耐久性来自两个事件：

| 事件 | 载荷要点 |
|------|----------|
| `HitlOpened` | `hitl_id / form / delivery / stage / tool_call_id / invocation_key / resume_state / reply_as_result / prompt / fields / proposal` |
| `HitlResolved` | `hitl_id / outcome / claimed / message? / modified_arguments?` |

恢复是**喂进来**，不是查回去：`fold_hitl_snapshot`（同步、纯函数，双读新旧两套事件）
→ `_hydrate_snapshot_messages`（event blob ref → memory 侧内容，纯文本零 IO）
→ `HitlRegistry.load_snapshot`。此后 registry 的一切查询只读内存。

- `CtxWeftRuntime.recover()`：**不再按未决与否分流会话状态**，它代 TM 发一条队列信号，
  由 `SessionManager` 推会话状态——有未决 → `TaskQueueBlocked{count}` → `SessionWaiting` →
  `WAITING`；无未决 → `TaskQueueInterrupted{reason}` → `SessionInterrupted` → `INTERRUPTED`，
  等 `/resume`。两条路都**什么都不跑**（重排推迟到应答）。
- 装填出来的 pending **不带等待槽**：重启后一切皆冷。
- 崩溃窗口兜底：已终局的 `UserTurn` 请求，其答复若还没进过对话，由
  `_inject_resolved_user_turns` 在恢复期补写（幂等键 `MemoryEvent.id = "hitlreply:{hitl_id}"`）。
  它**只写记忆、绝不碰 task 状态**——「SUSPENDED 在活子任务上」的父任务必须保持 SUSPENDED，
  `_try_resume_parent` 以此为门。

**已知代价**：`rebuild_hitl` 的折叠是 O(该会话 HITL 事件数)，而这条路每次冷应答都走一遍。
不截尾是刻意的：未决请求的年龄没有上界，按条数/时间截尾可能漏掉一条久未终局的请求，
从而让人还没回答的任务被重排跑起来。详见 `rebuild_hitl` 的 docstring。

## 7. 投影：分层链路，HITL 事件不写会话状态

从前是一步到位：`HitlOpened` 直接把会话打成 `PAUSED` / `PAUSED_HITL`，`HitlResolved`
再把它掰回 `RUNNING`。那让同一份信息有了第二个副本，而副本会失步——多个未决请求时，
「后到的 user_turn 把 `PAUSED_HITL` 降成 `PAUSED`」和「解掉其中一个就回 `RUNNING`」
两个 bug 就是失步的表现。现在改成**每层只说自己那层的事实**：

| 层 | 事件 | 说了什么 |
|----|------|----------|
| HITL | `HitlOpened` / `HitlResolved` | 有一个请求开了 / 终局了。**不碰任何状态** |
| task | `TaskAwaitingHuman{hitl_id}` | 这个 task 在等人 → task 状态 `AWAITING_HUMAN` |
| TM | `TaskQueueBlocked{count}` | 队列里没有能跑的了，`count` 个在等人 |
| session | `SessionWaiting{count}` | 会话停着，但正常 → 会话状态 `WAITING` |

反向同理：人答复了 → task 重新入队 → `TaskStarted` → SM 发 `SessionRunning{reason}`。

**行为变更：热等待窗口期间会话仍是 `RUNNING`。** 那时 task 真的还在跑（协程挂在等待槽上，
没被 park），队列也没空。只有降级成冷 park、队列真的空了，会话才变 `WAITING`。
**审批面板由 `HitlOpened` 驱动，不受影响**——变的只是会话徽标会晚一点。

其余：

- `SessionPausedHitl` **不再被发出**；reducer 保留该分支只为读存量日志，且把旧的两档
  （`PAUSED` / `PAUSED_HITL`）一律折进 `WAITING`——新值域里没有那两个值。
- pending 列表的真相源是 `HitlRegistry`，**不在 `RunStateView` 里另存一份**。

## 8. park 信号管线

`HitlPark` 是 `BaseException` 子类，穿过 gateway 的 `except Exception`，由 loop 捕获后把 task
置「挂起」（不是 `FAILED`），并发 `TaskAwaitingHuman{hitl_id}`——投影里该 task 落
`AWAITING_HUMAN`。**只有 gateway 抛它**——`HitlWaiter` 连 park 都不认识，
它只返回 `None`；`core/hitl/` 不 import `core.loop` / `core.runtime`。

## 9. 不变式清单

见设计文档 §10。三条在本文档反复承重：

- 被拒绝的调用**绝不**到达 `provider.invoke`（每一条路径都成立）。
- 一次应答**只驱动一条**续跑路径（`claimed` 是唯一权威）。
- `HitlRegistry` 全同步无锁，`fold_hitl_snapshot` 同步且纯。
