# 升级须知 · 会话状态所有权重构（2026-09-02）· **破坏性**

## 先读这一条

**`SessionStatusChanged` 不再被发出。** host 投影 / SSE 若按它更新会话状态，
升级后会话状态**永远停在崩溃前的值**——不会报错，只是再也不动了。改订四条：

| 旧 | 新 | 会话状态 |
|----|----|----|
| `SessionStatusChanged{"new_status":"INTERRUPTED"}` | `SessionInterrupted{reason}` | `INTERRUPTED` |
| `SessionStatusChanged{"new_status":"PAUSED"／"PAUSED_HITL"／"WAITING"}` | `SessionWaiting{count}` | `WAITING`（**三个旧值合并成一个**） |
| `SessionStatusChanged{"new_status":"RUNNING"}` | `SessionRunning{reason}` | `RUNNING` |
| `SessionStatusChanged{<终态>}` | `SessionFinished{final_status}` | `final_status` |

`reason` 的取值：`SessionInterrupted` ∈ `llm_outage` / run 崩溃的 `error_code` /
`process_restart`；`SessionRunning` ∈ `human_replied`（从 `WAITING` 回来）/
`resumed`（从 `INTERRUPTED` 回来）。都是给人看的，**不要拿来做路由**——
分流的判据是**事件类型**。

> **旧分支不要删。** 存量日志的回填仍需要它：core 侧 `reducers._apply` 保留了
> `SessionStatusChanged` 与 `SessionPausedHitl` 两个读分支（L 档 = 只读存量、
> 不得再发射），host 的 `projection_updater` 应当同样保留。删枚举值要等一整个归档周期，
> 见 `docs/events-v2.md` §5。

判断你是否受影响：搜这些字符串，命中即需要改。

```
SessionStatusChanged     new_status        SessionPausedHitl
TaskSuspended            hitl_park         run_crash
"PAUSED"                 "PAUSED_HITL"     "QUEUED"      "TIMEOUT"
```

---

## `TaskSuspended` 从三义收窄到一义

从前一个 `TaskSuspended` 盖住三件完全不同的事，逼得消费方去匹配 payload 里的
`reason` 字面量。现在每件事各有类型：

| 旧 | 新 | task 状态 |
|----|----|----|
| `TaskSuspended{reason:"hitl_park"}` | `TaskAwaitingHuman{hitl_id}` | `AWAITING_HUMAN` |
| `TaskSuspended{reason:"run_crash", …}` | `RunInterrupted{reason, error_code?, error_message?}` | `INTERRUPTED` |
| `TaskSuspended{summary, spawn_titles}` | **不变** | 仍是 `SUSPENDED`（等子任务） |

**host 若按 `reason` 字面量分流，改为按类型分流。** `reason` 现在只是展示文本。

`TaskStatus` 相应多了两个值。完整值域：
`PENDING` / `ACTIVE` / `SUSPENDED` / `AWAITING_HUMAN` / `INTERRUPTED` /
`TO_BE_OBSERVED` / `FINISHED` / `FAILED` / `CANCELED`。
host 若镜像了这份值域或用它做 switch，补上两个分支——**否则「等人」和「被打断」的
task 会掉进 default**。

> **「谁在等人」现在完全由 task 层回答。** 会话级只说「停着且正常」；
> 具体是哪个 task 在等、等的是什么，看 task 状态与未决 HITL 列表。

---

## 会话终态不再有「早报」

从前 `SessionStatusChanged(final_status)` 会**先于** `SessionFinished` 到达，
前端拿它提前把会话标成完成。**这条早报已删除。**

- **前端改为监听 `TaskQueueDrained`**（TM 报「全部任务终态」）或 task 结束事件，
  来提前反映「这一轮跑完了」。
- `SessionFinished` 仍在后台协程收尾之后到达，**仍是关流的信号**，语义未变。

若你的 UI 依赖「终态先到一次、再到一次」的两拍节奏，改成一拍
（或按上面订 `TaskQueueDrained`）。

---

## `PAUSED` 与 `PAUSED_HITL` 合并成 `WAITING`

两者的差别是**前端要不要出审批面板**，而那个信息的源头是 `HitlOpened.delivery`——
host 渲染面板时本来就拿到了。会话状态再复制一份只是让同一份信息有了第二个副本，
而副本会失步：多个未决请求时，「后到的 user_turn 把 `PAUSED_HITL` 降成 `PAUSED`」
和「解掉其中一个就回 `RUNNING`」这两个 bug 就是副本失步的表现。

**host 侧要做的**：会话徽标不再从会话状态区分「有没有面板」，改为从**未决 HITL 的
`delivery`** 判断（渲染面板的地方本来就在做这件事）：

| delivery | 前端 |
|---|---|
| `user_turn` | 输入框，无审批面板 |
| `tool_result` / `no_resume` | 审批面板 |

会话状态只回答三档：在跑 / 正常地停着 / 异常地停着。

### 例外：`session_status_after_recover` 的契约**没有变**

只读入口 `runtime.session_status_after_recover(session_id)` 仍返回
`""` / `"PAUSED"` / `"PAUSED_HITL"` 三个值中的一个。**这不是自相矛盾**——
它回答的从来不是「会话状态」，而是「等的是一块要人拍板的面板，还是只是一句话」，
即**面板提示**（panel hint）。判据是 `delivery`
（`core/hitl/status.py::paused_status_for`），两个字面量只是沿用了它一直对外用的名字。

**host 这边不用动这个调用。** 只要记住：它的返回值**不能**赋给会话状态字段
——那个值域里已经没有这两个值了。
（名字确实名不副实，改名要动 host 契约，已记为延后项。）

### 时机也变了（行为变更）

**热等待窗口期间会话是 `RUNNING`，而不是「在等人」。** 那时 task 真的还在跑——
协程阻塞在一个 `await` 上，和阻塞在一次 LLM 调用上没有区别，没有 park、没有
`TaskAwaitingHuman`、TM 什么都不知道。只有热窗口耗尽、降级成冷 park 之后，
会话才变 `WAITING`。旧行为是 `HitlOpened` 一到就立刻翻成 `PAUSED_HITL`。

**审批面板由 `HitlOpened` 驱动，不受影响**——面板照样立刻出来，变的只是**会话徽标
会晚一点**。若你的 e2e 断言「开了 HITL 之后会话立即是暂停态」，改成断言面板事件
或 task 状态。

---

## `cancel_all` 现在发终态事件

从前它只发 `SessionStatusChanged(CANCELED)`，**不发 `SessionFinished`**。
现在发 `SessionFinished{final_status:"CANCELED"}`。

若 host 的关流逻辑依赖 `SessionFinished`，那么取消路径的流从前**不会关**（连接泄漏）
——这条一并修了。升级后不需要为取消路径再加特判。

---

## `SessionStatus` 少了两个值、合并了两个值

最终 **6 个**，与会话状态机的可达状态一一对应：

```
RUNNING       有 task 在跑
WAITING       停着，但正常——都在等人 / 等外部输入
INTERRUPTED   停着，异常——系统故障，等 /resume（非终态）
SUCCEEDED  FAILED  CANCELED        三个终态
```

删除的四个：

- `QUEUED` / `TIMEOUT` —— core 从未赋过值，纯死值。
  （若你 grep 到 `"TIMEOUT"`，注意那是 capability provider 的**错误码**，另一回事。）
- `PAUSED` / `PAUSED_HITL` —— 合并成 `WAITING`（见上一节）。

**host 若镜像了这份值域（DB 枚举、TS union、前端 switch），同步改。**
注意 DB 里的存量行仍可能是旧值：**读侧要把 `PAUSED` / `PAUSED_HITL` 折进 `WAITING`**，
core 的 reducer 回放存量日志时就是这么做的（两种 `form` 都折，不再按 form 分档）。

---

## 新增三条 TM 信号（可选订阅）

`TaskQueueBlocked{count}` / `TaskQueueInterrupted{reason}` / `TaskQueueDrained{final_status}`
是 core 内部 `SessionManager` 的输入——TM 报「队列此刻是什么形状」，SM 据此推会话状态。

**host 不必消费**：会话状态已由前面四条 `Session*` 事件承载。

但它们**比会话状态事件更早到达**。host 若想做「这一轮跑完了」的提前提示，
订阅 `TaskQueueDrained` 是最准的信号（见「会话终态不再有早报」一节）。

> TM 只在**手上一个活都没有**时才发（`if self._queue or self._running_tasks: return`），
> 然后按「解开它需要谁」挑一条：`Interrupted`（要运维）> `Blocked`（要用户）>
> `Drained`（不用谁）。

---

## 分层链路速查

一次 HITL 冷 park 的完整链路，四层各说各的事实：

```
HitlOpened{delivery}             ← HITL 层：有一个请求开了。不碰任何状态
  → TaskAwaitingHuman{hitl_id}   ← task 层：这个 task 在等人   → task  AWAITING_HUMAN
  → TaskQueueBlocked{count}      ← TM 层：没有能跑的了
  → SessionWaiting{count}        ← session 层：停着，但正常     → 会话 WAITING
```

反向：人答复 → task 重新入队 → `TaskStarted` → `SessionRunning{reason:"human_replied"}`
→ 会话 `RUNNING`。

崩溃打断同理：`RunInterrupted` → `TaskQueueInterrupted` → `SessionInterrupted`。

---

## 相关文档

- 事件全集与分档：`docs/events-v2.md`（§2.1 / §2.3 / §2.4 / §3.3 / §5.2）
- 冻结清单：`docs/spec/01-events.md`
- HITL 挂起/恢复：`docs/spec/07-hitl-suspend-resume.md` §7（分层链路 + 热等待行为变更）
- reducer 规则：`docs/spec/03-reducer-rules.md`
- 黄金用例：`docs/spec/golden/06`、`07`、`13`（三份实现共用的一致性闸门）
