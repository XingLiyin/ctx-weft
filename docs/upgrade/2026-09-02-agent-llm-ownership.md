# 升级须知 · agent 域重构 + LLM 归属收敛（2026-09-02）· **破坏性**

本文收口「agent-registry-and-llm-ownership」整个计划（10 个任务）对 host 的
破坏性影响。三条，每条都要落地才算完：

1. `projection_updater.py` 补两个事件分支：`AgentLlmChanged`、`TaskHumanResolved`。
2. `reply_to_hitl` 不再接受 `resume_hint`：换模型改发独立的 `set_agent_llm` /
   `set_session_llm`。
3. 会话级「模型选择器」（如果 host 有）改调 `set_session_llm`，不要再直接改
   session 记录上的 `llm_model` 字段。

---

## 1. `projection_updater.py` 加两个分支

两条都是 **S 档**（进 core 的 `TASK_STATUS_BY_EVENT` / agent 折叠表），host 若不
跟着加分支，投影会在这两类事实上过期，且**不会报错**——只是安静地不再更新。

### `AgentLlmChanged`

```
payload: {llm_account, llm_model, reason}
```

纯赋值：`agent.llm_account` / `agent.llm_model` 改成新值，不碰任何 task / session
状态字段（换模型和「让 task 跑起来」是两件事，故意分开成两条独立事件——见
`docs/events-v2.md` 三条命令一节，spec §06）。发射点是 `AgentRegistry`
（`set_agent_llm` / `set_session_llm` 两条命令 + 派生新 agent 时的初始选择）。
`reason` 目前只有 `"user_selected"` 一个值，展示用，不建议按它分流。

### `TaskHumanResolved`

```
payload: {hitl_id}
```

`TaskAwaitingHuman{hitl_id}` 的配对解除事件——「人已经答复/放行，这个 task 不再
等人了」。→ task 状态 `PENDING`，且清旧产出（`outputs = None`，与 `TaskRequeued`
效果相同，但**判据是类型不是 payload**：`TaskRequeued` 已经背着 retry / reopen
两义，不能再塞进第三义）。

发射点两处，功能上互斥（同一次 HITL 解决只会走其中一条）：
- `TaskManager.resume_task`（approval 分支，且仅当当前状态是 `AWAITING_HUMAN` /
  `SUSPENDED` 时才发，避免与下面那条重复）；
- `CtxWeftRuntime._inject_user_reply`（wait_for_user 分支，覆盖「存活 owner 就地
  续跑」与「崩溃后重建 TM」两条路径）。

若 host 的投影靠「task 是否还在等人」来渲染一个气泡/提示，这条事件到达前，
`TaskAwaitingHuman` 之后的投影会一直显示"在等人"——即使 core 内部（内存对象）
早已把状态改回了 `PENDING` 并继续跑。**这正是本次要修的缺陷**：此前
`resume_task` / `_inject_user_reply` 只改内存状态、不发事件，重放（`/resume`
重建投影、host 的事件表回放）看到的永远是卡在 `AWAITING_HUMAN` 的 task。

---

## 2. `reply_to_hitl` 不再接受 `resume_hint`：拆成两条命令

旧签名允许 `reply_to_hitl(reply, resume_hint=...)` 在一次调用里同时「换模型」+
「应答」。现在 `HitlReply` 只有 `hitl_id` / `outcome` / `message` /
`modified_arguments` 四个字段，**没有** `resume_hint`，`ResumeHint` 类型本身也已
从 `ctx_weft.protocols` 删除。

若 host 曾经这样用：

```python
# 旧
await runtime.reply_to_hitl(HitlReply(
    hitl_id=hid, outcome="accepted", resume_hint=ResumeHint(llm_model="claude-x"),
))
```

改成两条独立调用，先换模型、再应答（顺序不能反——应答会驱动续跑，续跑读的是
registry 当时已经生效的选择）：

```python
# 新
await runtime.set_agent_llm(agent_id, llm_model="claude-x")
await runtime.reply_to_hitl(HitlReply(hitl_id=hid, outcome="accepted"))
```

`set_agent_llm` 作用于单个 agent；批量改一个会话下所有 agent 用
`set_session_llm(session_id, llm_model=...)`（返回值是真正改动了的 agent
数，幂等 no-op 不计数）。两者都发 `AgentLlmChanged`——host 必须先完成本文
第 1 节才能看见这些改动反映到投影上。

---

## 3. 会话级模型选择器改调 `set_session_llm`

**LLM 的真相源已经从 session 移到 agent record**（`AgentRegistry` 持有，
`session.llm_model` 不再是权威值，只在没有更精确来源时兜底）。若 host 有一个
「整个会话切换模型」的入口，此前可能是直接改 session 表的 `llm_model` 字段
（或经某个已废弃的同步路径回填）——那条路径已经不是权威写入点，投影可能与
`AgentRegistry` 里的真实选择不一致。

改用：

```python
n = await runtime.set_session_llm(session_id, llm_account="...", llm_model="...")
```

它遍历 `AgentRegistry` 里该 session 下的全部 agent record 逐个改，每个真正
改动的都发一条 `AgentLlmChanged`（同样依赖本文第 1 节的投影分支）。若 host 的
「会话模型」UI 只是想展示当前值，读某个 agent（通常是 root）的 `llm_model`，
不要再读 session 记录上的字段。

---

## 4. 已知缺口

### (a) `TaskAwaitingHuman` / `TaskHumanResolved` 目前不是严格 1:1 配对

第 1 节说的是配对关系本身；这里说的是**它现在还没做到**。有两处会漏发解除事件：

- **恢复路径**上（`recover_session` 直接调 `_inject_user_reply`，不像
  `_resume_in_existing_tm` 那样随后还会跑一次 `resume_task`）：若 task 同时
  「`SUSPENDED` 且尚有活子任务」又叠加 HITL，`_inject_user_reply` 命中
  `_suspended_on_live_children` 分支提前返回——状态留给 `_try_resume_parent`
  日后唤醒，`TaskHumanResolved` 这一条**不发**。
  存活 owner 路径不受影响：那条路调用 `_inject_user_reply` 之后总会再跑一次
  `tm.resume_task(...)`，其 `was_blocked` 判据（状态仍是 `AWAITING_HUMAN` /
  `SUSPENDED`）在这种情况下命中，会补发这一条——没有丢。
- 「答复已经落了 `HitlResolved`、进程在发出 `TaskHumanResolved` 之前死亡」这个
  窄窗口：崩溃重建路径目前没有钩子去检测并补发它。

后果：host 若查「有 `TaskAwaitingHuman` 没配到 `TaskHumanResolved`」来判断
「这个 task 是否还卡着」，上面两种情形会**假阳性**——task 实际已经解除等待，
只是少了一条记账事件。**本批次不建议 host 直接拿这个配对做告警**；补齐配对是
后续任务的工作。

### (b) `set_session_llm` 只对 registry **当前持有的** record 生效

第 3 节说它「遍历该 session 下的全部 agent record」，没说清 record 什么时候
才存在。`AgentRegistry` 是内存态：一个 session 跨进程重启后，若还没有
`recover_session`（或任何触发 `load()` / `materialize()` 的调用）把它的
agent record 重新装填进当前进程的 registry，`set_session_llm` 会在一个空
候选集上遍历——**返回 `0`，不发任何 `AgentLlmChanged`**，不报错也不警告。

host 若在这个时间点调用它，看到的是「返回成功（`0` 不是异常）但什么都没变」。
要避免这种假成功，调用前先确认该 session 已经过一次 `recover_session`（或其
等价的装填路径），或者直接检查返回值：`0` 且预期应该 > 0 时，视为「registry
还没见过这些 agent」，而不是「没有 agent 需要改」。

---

## 判断你是否受影响

搜这些字符串，命中即需要检查：

```
resume_hint        ResumeHint          reply_to_hitl
AgentLlmChanged     TaskHumanResolved  set_agent_llm
set_session_llm     llm_model
```

- 命中 `resume_hint` / `ResumeHint` → 见第 2 节，拆成两条调用。
- host 的 session 表 / 某个「切模型」入口直接写 `llm_model` 而不经
  `set_agent_llm`/`set_session_llm` → 见第 3 节。
- `projection_updater.py` 还没有 `AgentLlmChanged` / `TaskHumanResolved` 分支
  → 见第 1 节，两条都要加，缺哪条哪类事实就在投影上过期。

## 不受影响的部分

- `delegate_task` 仍然不能指定子 agent 的模型（有意不做——子 agent 一律继承
  派生它的那个 agent，模型只能由人经 `set_agent_llm` / `set_session_llm` 两条
  命令事后改。结构上支持起来很容易，但那等于让 LLM 自己选模型，是产品决定，
  不是技术限制）。
- `TaskResumed` 的投影映射从 `ACTIVE` 改成了 `PENDING`（`_try_resume_parent`
  解挂子任务全终态的父任务时不再抢跑置 `ACTIVE`，真正的 `ACTIVE` 只由
  `TaskStarted` 写定）——这条**不是 host 契约变化**，`TaskResumed` 本来就只是
  core 内部 `TASK_STATUS_BY_EVENT` 的折叠映射，host 若自己维护一份同名映射表，
  照这条改一下即可，不涉及事件 schema 或新分支。
