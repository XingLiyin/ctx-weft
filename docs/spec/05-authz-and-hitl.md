# 05 · 工具鉴权与 HITL

> 真相源：`core/auth/authorizer.py`、`core/orchestrator/hitl_manager.py`、`core/loop/capability_gateway.py`
> 行为黄金：`tests/unit/test_authorizer.py`、`test_hitl.py`（三份实现须复现同一断言）

这是行为契约（非 reducer 投影），但同样要求三份实现语义一致。下面的"必须"项即跨语言一致性点。

---

## 1. Authorizer 协议

主方法对**一次调用**作授权决定：

```
authorize(capability, ctx, arguments?, *, tool_call_id="") -> AuthorizationDecision
AuthorizationDecision { allowed: bool, message: str = "", modified_arguments: dict | None = None, defer: bool = False }
```

- `message`：反馈 / 拒绝指导，回灌给 LLM（allow / deny 都可带）。
- `modified_arguments`：allow 时的有效参数（`None` = 用原参）。
- `defer`：挂起本次调用（不放行也不拒绝；gateway 绝不 invoke，上抛 HitlPark）。见 spec/07 §7。
- `ctx` 是 `ProviderContext`，携带 session/task/agent 标识与 `agent_template_id`；
  授权契约不依赖 core 的 Agent/Task 状态对象。

`filter(capabilities, ...) -> capabilities` 是基于 `authorize` 的**批量便捷默认**（可见性过滤），
保留给装配期/外部用；当前内部唯一消费点是 gateway 的单 cap `authorize`。

### 内置实现

| Authorizer | `authorize` 返回 |
|------------|------|
| `AllowAll` | `allowed=True`（默认） |
| `AllowList` | 按 `ctx.agent_template_id` 查 `allow_map`/`deny_map`；拦截时带 `deny_message` |
| `HumanConfirmation` | 发 approval HITL，等应答，把 `message`/`modified_arguments` 透传进决定 |

**AllowList 规则（必须按此顺序）**：
- `cap.id ∈ deny_map[ctx.agent_template_id]` → `allowed=False, message=deny_message`。
- `allowed_set = allow_map.get(ctx.agent_template_id)`：
  - `None`（模板未登记）→ 放行。
  - 集合（含空集）→ 仅 `cap.id ∈ allowed_set` 放行；空集 = 全拦（拦截带 `deny_message`）。
- deny 优先于 allow。

**HumanConfirmation 规则（热路径）**：`hitl.request(kind="approval")` → `wait`；
`accepted` → `allowed=True` 且透传 `approval.message`/`approval.modified_arguments`；
`rejected` → `allowed=False` 且透传 `approval.message`（拒绝指导）。
热窗口超时 → 协程经 HitlPark 信号 unwind 至 `SUSPENDED`（不是失败，见下方热/冷模型）。

**HumanConfirmation 规则（冷路径短路）**：`authorize()` 先按 `tool_call_id` 查已解决的 HITL 缓存；
命中（restart 后 `rebuild_pending` + `resolve_approve` 路径）→ 直接用缓存决定返回，**不调 `wait()`**。
此短路是安全性必要条件：restart 后 `_futures` 为空，调 `wait()` 会 `KeyError`。

---

## 2. CapabilityGateway 鉴权集成

`invoke(tool_name, arguments, state, ctx)` 的鉴权相关步骤（必须）：

1. 按名查 cap；未找到或 `kind != "tool"` → 返回 `is_error` 结果，文案含 `unknown tool`。
2. **鉴权**：`authorizer = _get_authorizer(cap.id)`；`decision = authorizer.authorize(cap, ctx.provider_ctx, arguments, tool_call_id=…)`。
   传入的必须是 `ProviderContext`，不是 loop 的 `LoopContext`。
   - `decision.allowed == False` → 返回 `is_error` 结果，**且绝不调用 provider.invoke**（关键安全不变式）。
     文案：有 `message` → `[Blocked by human: {message}]`（指导回灌）；否则 `[Error: capability 'X' not authorized]`。
3. 放行 → 有效参数 = `decision.modified_arguments ?? arguments` → `_sanitize(有效参数)` → 找 provider
   → 发 `CapabilityInvoked` + ingest `tool_invocation`（均用有效参数）→ 流式执行
   → 若 `decision.message` 非空，将 `[Human note: {message}]` 并入结果文本 → 发 `CapabilityFinished` + ingest `tool_result`。

### `_get_authorizer` 解析顺序（必须）

```
1) provider_authorizers[完整 cap.id]
2) provider_authorizers[cap.id 的 provider 前缀]   # 前缀 = cap.id.rsplit(":", 1)[0]
3) default_authorizer（默认 AllowAll）
```

### 参数脱敏 `_sanitize`（必须）

`arguments.headers` 中 key（小写）∈ `{authorization, cookie, x-api-key, x-auth-token}` 的值替换为 `"***"`；
**不可改动原 dict**（返回副本）。脱敏后的参数用于事件与 memory，避免密钥落审计流。

---

## 3. HITL 模型：一个机制 / 两种 kind / 三个触发点

`HitlManager` 是单一机制（request → wait → resolve）。每个请求带显式 **`kind`** 区分语义：

| kind | 语义 | 应答 | 消费方 |
|------|------|------|--------|
| `approval` | 工具门控（放行/拒绝一次调用） | `approve` / `reject` | HumanConfirmationAuthorizer |
| `input` | 向人提问（取回文字答复） | `answer` / `reject` | request_human_input、submit_task_assessment(needs_user_input) |

**决定与消息正交**：任何应答都可附带自由文本 `message`（答复 / 拒绝指导 / 备注）。
`decision`（accept/reject）控制流程，`message` 旨在回灌给 agent。`modified_arguments` 仅 approval、暂仅记录。

**三个触发点**：

| # | 触发点 | purpose | kind | park 在 | task 状态 |
|---|--------|---------|------|---------|-----------|
| A | HumanConfirmationAuthorizer（gateway 鉴权步） | — | approval | authorizer.filter 内 | 不改 |
| B | `request_human_input` 控制工具 | act + observe | input | ControlProvider._handle 内 | 不改 |
| C | `submit_task_assessment(task_status="needs_user_input")` | observe | input | 同 _handle（复用 `HITL_REQUESTED` metadata） | **不改**（观察循环据答复继续，非终态裁决） |

> B/C 共用同一条 park 路径；C 本质是"观察者调了一次 request_human_input"。
> park 期间 `session.status = "PAUSED_HITL"`，应答后回 `RUNNING`；答复作为工具结果回灌当前 ReAct 轮。
>
> **跨重启持久性（Phase C-1 已实现）**：`PAUSED_HITL` 通过 host 投影持久化（HitlRequired →
> PAUSED_HITL，HitlResolved → RUNNING）；进程重启后 `recover_session()` 从 `pending_hitl`
> 投影重建 `HitlManager`，HITL 挂起的 task 保持 `SUSPENDED` 不重新入队，等待冷路径应答触发续跑。
> 这使 pending HITL 在重启后保持 resumable（不再被标为 `INTERRUPTED`）。
>
> **crash-mid-batch 统一路由**：若进程崩溃时 LLM 批量 tool_call 只执行了部分，
> `_task_has_dangling_tool_call(memory, scope, provider_ctx)` 检测 memory 中未完成的 tool_call，
> 路由到 `ReconcileStep` 补全剩余调用，与 HITL 恢复共用同一对账机制（spec/07 §9）。

### 状态机

```
pending ──approve──────────▶ accepted   （HitlApproved；带 modified_arguments → HitlModified）
        ──answer(text)─────▶ accepted   （HitlAnswered）
        ──reject───────────▶ rejected   （HitlRejected）
        ──cancel───────────▶ cancelled  （HitlCancelled；session 关闭 / interrupt / GC）

        ──(热窗口超时)──▶ 仍是 pending  （内部 hot→cold 降级：驱逐 _futures entry + task SUSPENDED；不发终态事件）
```

> 旧的 `approved`/`modified` 合并为 `accepted`；区别落在 kind + 载荷 + 发出的事件。
> **超时不再是终态**（已删除 `timeout` 状态与 `HitlTimeout` 事件）：超时 = hot→cold 驱逐，
> request 持久保持 `pending`，协程经 `HitlPark` 信号 unwind 到 `SUSPENDED`；
> 晚到的应答走冷路径（写结果 + 重新入队 task → reconcile → LLM 续跑）。
> 竞态由单一权威锁保证：驱逐 vs. 应答互斥（要么 Future 先被 set 走热路径、要么 Future 先被驱逐走冷路径）。

### HitlRequest 字段

`id / kind / session_id / task_id / agent_id / capability_id / arguments /
question / context / status / message / modified_arguments / created_at / resolved_at`。
`message` 承载人类自由文本（答复 / 拒绝指导 / 备注），任何 decision 下都可有。
`accepted` 便捷属性 = `status == "accepted"`。

### 接口与规则（必须）

| 方法 | 规则 |
|------|------|
| `request(kind, session_id, task_id, *, capability_id, arguments, question, context, agent_id)` | 登记 pending，返回 id；发 `HitlRequired`(payload 含 `kind`) + `SessionPausedHitl` |
| `wait(id)` | 阻塞至应答（热路径）；热窗口超时 → 抛 `HitlPark`（hot→cold 降级，request 仍 `pending`）；未知 id 抛错 |
| `approve(id, *, message="", modified_arguments=None)` | approval：`status="accepted"`、存 `message`；发 `HitlModified`(有改参)/`HitlApproved` |
| `answer(id, text)` | input：`status="accepted"`、`message=text`；发 `HitlAnswered` |
| `reject(id, *, message="")` | `status="rejected"`、存 `message`（指导反馈）；发 `HitlRejected` |
| `get(id)` / `list_pending(session_id=None)` | 查询；仅 `pending` 出现在 list |

> **resolve 幂等**：已解决（accepted/rejected/cancelled）的请求再调 approve/answer/reject 是 no-op，不二次转移。
> 消费方按 `status` 判定：`accepted` → 放行 / 取 `answer`；`rejected` → 拦截 / 给提示文案。

### host 应答路由（必须）

host 据 `request.kind` 决定动作与 UI：
- `approval` → `/hitl/{id}/approve`（可带 modify）或 `/reject`（渲染批准/拒绝）。
- `input` → `/hitl/{id}/answer`（渲染答题输入框）或 `/reject`。
- 兼容入口（用户直接回话）：REJECT 词 → reject；否则 approval→approve、input→answer。

---

## 4. 端到端语义

**B/C. input（控制工具）—— message 回灌已落地**：`request(kind="input")` 后 `wait`（热路径），按 status 生成工具结果回灌 LLM：
- `accepted` → `message or 默认文案`；
- `rejected` → `Human declined: {message}`（有 message）/ `Human rejected the request.`（无）——**拒绝时的指导反馈也回灌**，agent 可据此改方向。
- 热窗口超时 → `HitlPark` unwind 至 `SUSPENDED`；冷路径应答到达后 reconcile 恢复续跑（工具结果在 reconcile 中写入）。
- C（needs_user_input）**不改 task 状态**——不进 `TASK_STATUS_BY_EVENT`，投影上 task 状态不变。

**A. approval（gateway）—— message/改参回灌已生效**：`HumanConfirmationAuthorizer.authorize` 先查决定缓存（按 `tool_call_id`）；
缓存命中（冷路径）→ 直接返回缓存决定，不调 `wait()`。缓存未命中（热路径）→ `request(kind="approval")` + `wait`，
把决定（含 message / modified_arguments）返给 gateway：
- `accepted` → 放行 → gateway 用 `modified_arguments`（若有）执行 provider；`message` 作 `[Human note: …]` 并入结果。
- `rejected` → 拦截，**provider 不执行**；`message` 作 `[Blocked by human: …]` 回灌（拒绝指导送达）。
- 热窗口超时 → `HitlPark` unwind；冷路径 approve/reject 到达后走 reconcile 经同一 `authorize` 命中缓存执行。

> Authorizer 协议已升级为 `authorize(cap, …) -> AuthorizationDecision`（见 §1）：approval 的拒绝指导与
> 同意改参现在都对 agent 生效。改写参数同样过 `_sanitize`，审计/memory 记录用实际执行的有效参数。

### host 文本回话判定（必须）

**关键词路由只用于 `approval`**——因为审批的语义就是 yes/no = approve/reject：按**首词**判定，其余文本作 `message`。
例：`"no, 先列目录"` → `reject(message="先列目录")`；纯首词（如 `"approve"`）→ message 为空。

**`input` 的文字回复一律是答复**（`answer(text)`）——`"no"` 是一个否定**答复**，不是"拒绝问题"。
绝不可从答复文本用关键词推断 reject；input 的显式拒答 / 中止只能走 `POST /hitl/{id}/reject` 或 interrupt。

---

## 5. 跨语言一致性清单

TS / Java 实现必须复现：

- [ ] `authorize(cap, ctx: ProviderContext, arguments?, *, tool_call_id) -> AuthorizationDecision{allowed, message, modified_arguments, defer}`；`filter` 为基于它的默认。
- [ ] AllowList 的 deny 优先、未登记模板放行、空集全拦三条规则（拦截带 `deny_message`）。
- [ ] `_get_authorizer` 三级解析顺序。
- [ ] **拦截时 provider.invoke 绝不被调用**（安全不变式）；deny 的 `message` 作 `[Blocked by human: …]` 回灌。
- [ ] allow 时 `modified_arguments` 生效（过 `_sanitize`，审计/memory 用有效参数）；`message` 作 `[Human note: …]` 并入结果。
- [ ] `_sanitize` 的敏感 header 集合 + 不改原对象。
- [ ] HITL **kind 判别**（approval / input）+ 状态机 `pending→accepted/rejected/cancelled`；**超时 = hot→cold 降级，不改持久状态**。
- [ ] 各 resolution 事件：`HitlApproved`/`HitlModified`/`HitlAnswered`/`HitlRejected`/`HitlCancelled`；request 发 `HitlRequired`(含 kind)+`SessionPausedHitl`。
- [ ] 三触发点：authorizer（approval）、request_human_input（input）、submit_task_assessment needs_user_input（input，不改 task 状态）。
- [ ] park 期间 PAUSED_HITL ↔ RUNNING；答复回灌为工具结果。
- [ ] **决定与 message 正交**：input 路径 accept→`message or 默认`、reject→`Human declined: {message}` 回灌。
- [ ] **关键词路由仅限 approval**；input 文字回复一律为答复（`"no"` 是答复，非拒绝），显式拒答只走 reject 端点 / interrupt。
- [ ] resolve 幂等、`wait` 热窗口超时抛 `HitlPark`（hot→cold 降级）、未知 id 报错、host 按 kind 路由应答。
- [ ] **park 信号管线**：`HitlPark` 为 `BaseException` 子类，穿透 gateway `except Exception`，loop 捕获后 task 置 `SUSPENDED`（非 `FAILED`）。
- [ ] **热/冷判别 = `_futures` 命中**：应答时命中 Future → 热（set Future 就地续跑）；缺失 → 冷（写结果 + requeue + reconcile）；重启后全冷。
- [ ] **approval 冷路径短路**：`authorize()` 按 `tool_call_id` 键查已解决 HITL；命中则直接返回决定，**不调 `wait()`**（重启后无 future 时不 KeyError）。
- [ ] **竞态单一权威**：驱逐（超时）vs. 应答在锁内互斥；驱逐本身不 requeue，唯有应答才触发 resume。

> 与 reducer 的 golden 用例不同，鉴权/HITL 是带并发与超时的行为测试，宜在各语言以等价单测复现，
> 以 `test_authorizer.py` / `test_hitl.py` 的断言为准绳。
