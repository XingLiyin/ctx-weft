# 08 · 移植同步说明：HITL 冷应答闭环 + 文件系统 Provider 拆分

> 本文记录本轮在 **Python 参考实现**（`ctx-weft/` + `src/ipmastercowork/`）落地的两组改动，供
> **Java 版（`loomej-core/`）** 与 **TS 版（`ctx_weft-ts/`）** 对齐移植。
> 行为契约以本文为准；代码细节以 Python 源 + [spec/07](./07-hitl-suspend-resume.md) 为真相。
> 每条改动给出「为什么 / 契约 / 待移植清单 / 必须复刻的测试不变式」。

两组改动彼此独立，可分别移植：

- **A. HITL 热/冷分流在 core 内闭环 + 事件驱动的崩溃恢复**（对应 spec/07 §6/§9）。
- **B. 文件系统能力从 builtin provider 拆出，带 per-session workspace**。

---

## A. HITL 冷应答闭环 + 崩溃恢复

### 背景术语

- **热应答（hot）**：被 park 的工具协程仍活着（`Future` 未驱逐），应答直接 `future.set_result` 就地唤醒。
- **冷应答（cold）**：协程已被超时驱逐，或进程重启后 `Future` 不存在 → 无法就地唤醒，必须**触发该
  session 重新恢复执行**才能续跑。
- 此前冷分流逻辑散在 **host**：host 调 `resolve_*` 拿到 `was_hot`，`was_hot=False` 时 host 自己去调
  `runtime.recover_session`。本轮把它**收敛进 core**——host 不再感知 `was_hot`。

### A1. 冷分流收敛进 HitlManager（`on_cold_resolve` 回调）

**为什么**：分流是 core 的职责（host 不该知道 was_hot、也不该决定怎么续跑）。act 层 input/approval 与
observe 层 ask_human 的续跑方式不同（见 A2），该决策属于 core。

**契约**：
- `HitlManager` 新增可选回调 `on_cold_resolve: (HitlRequest) -> Awaitable<void>`，构造可传、亦可
  `set_cold_resolve_handler(...)` 晚绑定（Runtime 需先构造好自己才能绑 `recover_session`）。
- `_resolve(...)` 新增 `resume_on_cold: bool` 形参。仅 **answer / approve / reject** 三个解决路径传
  `resume_on_cold=True`；**cancel 不传**（cancel 是终态，不 requeue）。
- `_resolve` 末尾：`if resume_on_cold and not was_hot and on_cold_resolve != null: await on_cold_resolve(req)`。
  即：唯有「冷 + 非 cancel + 已绑回调」才触发 resume。传入的是**已解决的 `HitlRequest`**（含 kind /
  capability_id / message / status），由回调侧据内容决定续跑方式。
- `Runtime` 构造后调 `hitl_manager.set_cold_resolve_handler(self._resume_after_cold_hitl)` 绑定。

**待移植清单**：
- `HitlManager`：加 `onColdResolve` 字段 + setter；`resolve()` 内部加 `resumeOnCold` 参数与上述触发分支。
- `Runtime`：构造后绑定 `_resumeAfterColdHitl`。

**测试不变式**：纯单测（无回调）冷应答不报错、不 resume；绑定后冷应答恰好触发一次回调，热应答不触发。

### A2. 冷应答后续跑的两条路（判别键 = `capability_id`）

冷应答到达 → `Runtime._resume_after_cold_hitl(req)` 据 `req.capability_id` 二选一：

1. **act 层 input（`request_human_input`）/ approval** → 走 **reconcile**（`recover_session` 内的
   reconcile step 把 dangling tool_call 经 `gateway.invoke` 补完，HITL 记录按 `tool_call_id` 作权威决定
   缓存短路门控）。**不**注入。
2. **observe 层 ask_human（`submit_task_assessment`）** → 走 **注入**（reconcile 覆盖不到，见 spec/07 §6.1）：
   observe 的 ReAct 是纯内存、不往 task 层写 `LLM_RESPONSE`，且该工具是 `_SILENT_TOOLS`（不写
   TOOL_INVOCATION/TOOL_RESULT），故无 dangling 锚点。其冷路径**镜像 finalize 的 ask_human 分支**。

判别实现：`is_ask_human = req.kind == "input" and req.capability_id.endswith(":submit_task_assessment")`。

**注入逻辑**（`Runtime._inject_ask_human_reply`，在 `recover_session` 的 **drain 之前**执行）：
- 取回复文本：`status == "rejected"` → `"Human declined: {message}"`（无 message 用兜底句）；否则
  `message`（空则 `"(no response)"`）。
- 以 `MemoryEventType.USER_PROMPT`、`role="user"`、`metadata={source: "hitl_reply"}` ingest 到 task 层
  memory（scope/ctx 用该 task）。
- 清旧进展：`task.outputs = null`、`task.process_report = null`；若 task 非终态则置 `PENDING`。

`recover_session` 因此新增可选参数 `ask_human_reply: HitlRequest | null`，仅在 ask_human 冷路径传入。

**待移植清单**：
- `Runtime.recoverSession(sessionId, askHumanReply=null)` 增形参。
- `Runtime._resumeAfterColdHitl(req)`：据 capability_id 判别，调 `recoverSession`。
- `Runtime._injectAskHumanReply(req, session, taskManager)`：按上述注入。

**测试不变式**：
- act input/approval 冷应答 → 经 reconcile 写出唯一 TOOL_RESULT（形状与热路径一致、exactly-once）。
- observe ask_human 冷应答 → task 层多出一条 `USER_PROMPT`、task 回到 PENDING、续跑能读到回复；
  reject 时文本为 `Human declined: …`。

### A3. reconcile 修复：先绑 capability，且 `next_step = "reason"`（不是 `"act"`）

**为什么（两个端到端缺陷）**：
1. `ReconcileStep` 原 `next_step="act"`，但 `ActStep` 要求 `ReasonStep` 先装配 `assembled_prompt` →
   崩溃恢复任务直接报错。
2. capability 原**仅**在 `ReasonStep` 绑入 per-agent cache，而 reconcile 作为 resume 的 initial_step 跑在
   ReasonStep **之前** → 空 cache，`gateway.invoke` 找不到 dangling 工具。

**契约**：
- 抽出共享函数 `resolve_and_bind(state, ctx)`（= 解析 capability + 写入 `capability_cache`），供
  `ReasonStep` 与 `ReconcileStep` 复用（原 `ReasonStep._resolve_capabilities` 整段移到这里）。
- `ReconcileStep.execute`：① 有 dangling 时先 `await resolve_and_bind(state, ctx)` 再 invoke；
  ② **无论有无 dangling，`next_step` 一律 `"reason"`**（让 reason 用补齐的 memory 重装 prompt + 再次绑定，
  再由 act 调 LLM）。

**待移植清单**：
- 新建共享模块（Py: `core/loop/steps/_capabilities.py`）：`resolveCapabilities` + `resolveAndBind`。
- `ReasonStep` 改用共享函数；`ReconcileStep` 两处改动如上。

**测试不变式**（务必端到端、跑真 ActStep，别只单测 `ReconcileStep.execute`——这正是历史漏测点）：
- 崩溃中途批次恢复后能续跑到底、不报缺 `assembled_prompt`；
- reconcile 后 `next_step == "reason"`（有/无 dangling 都是）。

### A4. 事件驱动的崩溃恢复 `recover()`（无回调、启动不 drain）

**为什么**：旧 `recover(on_session_interrupted=...)` 靠 host 回调标 INTERRUPTED、且按 host 投影状态决策；
问题：① 重启后内存 HitlManager 为空 → 应答入口 404；② core 反向调 host 与事件溯源不一致；③ 多 HITL 部分
解决时投影会误回 RUNNING（事件折叠才准）。

**契约**（`Runtime.recover()`，无参、无回调、返回处理的 session 数）：对每个 active session（有
`SessionCreated`、无 `SessionFinished`），用**轻查询**只取 HITL 类事件并折叠出未解决 pending：
- **有未解决 pending HITL** → **只 `hitl_manager.rebuild_pending(...)`**（仅重建内存 HitlManager，使三个
  应答入口可命中），状态留 `PAUSED_HITL`、**不 drain、不跑任何 task**（task 重建+drain 推迟到应答到达的
  `recover_session`）。
- **否则**（崩溃前在跑）→ **emit `SessionStatusChanged(new_status="INTERRUPTED")`**，由 host 既有事件
  订阅者（投影 / SSE）反映，等用户 `/resume`。

启动期因此对称：PAUSED 等应答、INTERRUPTED 等 `/resume`，**都不在重启时跑机器工作**。

辅助方法：
- `rebuild_hitl(session_id) -> int`：折叠该 session 的 HITL 事件、重建内存 pending，返回条数。**幂等**。
- `rebuild_all_pending_hitl() -> int`：对所有 active session 调 `rebuild_hitl`，供只带 approval_id、无
  session_id 的应答入口自愈用。
- `_pending_hitl(session_id)`：调 EventStore 轻查询 `read_session_events_of_types(HITL_STATUS_EVENT_TYPES)`，
  未实现则降级为「全量读 + 内存过滤」。
- `_emit_session_interrupted(session_id)`：emit `SESSION_STATUS_CHANGED{new_status: INTERRUPTED}`。

**待移植清单**：
- reducer 层：`fold_pending_hitl(events) -> {id: HitlRequestView}`（`HITL_REQUIRED` 累加、各终态
  `HITL_APPROVED/MODIFIED/ANSWERED/REJECTED/CANCELLED` 移除）；常量 `HITL_STATUS_EVENT_TYPES`；
  便捷 `unresolved_hitl_ids`。
- EventStore 协议：新增 `read_session_events_of_types(sessionId, types) -> List<Event>`（按 sequence/id
  排序；默认抛 NotImplemented，内存实现做过滤，Postgres 实现做 `type IN (...)` 查询）。
- `Runtime`：`recover()` 重写 + 上述四个辅助方法。

**测试不变式**：
- 有 pending HITL 的 active session 经 `recover()` 后：内存 HitlManager 有该 pending、状态仍 PAUSED_HITL、
  无 drain；其余 session 收到一条 `SESSION_STATUS_CHANGED=INTERRUPTED` 事件。
- `fold_pending_hitl` 折叠正确（请求后被各终态移除；快照往返不丢）。

### A5. Host 侧适配

- **`sessions._submit_hitl_response`**（`/messages` 回复入口）：
  - 改调 `hitl.reject/approve/answer`（不再读 `was_hot`、不再自己调 `recover_session`）——冷分流已在 core。
  - 回复前若 `list_pending` 为空 → 先 `runtime.rebuild_hitl(session_id)` 自愈再试（重启后内存可能未填）。
  - **在 resolve 之前**重启该 session 的 `session_consumer`（bump `_consumer_token`、`sse_finished=False`、
    `create_task(session_consumer(...))`），把冷恢复的续跑事件桥接到 SSE——否则重启后原 consumer 已随进程消失，
    agent 续跑/再 park 的事件到不了前端，表现为「卡住」。
- **`hitl.py`**（`/hitl/{id}/{approve,answer,reject}` + `/hitl/pending`）：加 `_heal_and_retry` 包装——
  先试；`KeyError`（重启后内存 HitlManager 未填）→ `runtime.rebuild_all_pending_hitl()` 后重试一次；
  仍 `KeyError` → 404。`/hitl/pending` 列空时也据事件重建后再列。
- **`deps.get_runtime_optional()`**：软访问器，未配置返回 None（供上述 best-effort 自愈路径）。
- **`startup.py`**：`_setup_recovery` 去掉回调、只调 `runtime.recover()`；**关键顺序**：先 `recover()`
  （core emit 事件→投影更新），**再** `load_sessions_from_db`（从已更新投影把 INTERRUPTED/PAUSED_HITL
  灌回内存缓存）。原先 `_setup_db` 里的 `load_sessions_from_db` 移到 recover 之后。
- **SSE 重发 waiting_input**（`models/session.py` `sse_generator`）：`waiting_input` 是瞬时控制事件、不进
  history，重连/重启后提问框会消失。故当 `status == "PAUSED_HITL"` 时，从快照里倒查最近一条 `waiting_input`
  补发一次，让前端重渲染提问框（仅 PAUSED 时补，不会复活已解决的旧框）。

**待移植清单（TS host 对应 Fastify 实现）**：上述五点逐一对应；Java 若无 host 层可只移植 core 部分。

---

## B. 文件系统能力 Provider 拆分 + per-session workspace

### 背景

原 `BuiltinToolsCapabilityProvider` 同时提供文件系统工具（`bash_exec/read_file/write_file/glob`）与
`http_request`。文件系统工具需要 **per-session 工作目录**（参考此前「workspace 属于 fs provider」约定）：
core 不持有 workspace，由 host 在 session 启动前登记给 fs provider 管理。本轮把 fs 工具单独拆成一个 provider。

### B1. 新协议层 `protocols/filesystem.py`

- 常量 `FS_PROVIDER_NAME = "fs"`；`FsTool` 钉死标准 capability id：`fs:bash_exec / fs:read_file /
  fs:write_file / fs:glob`（模板、授权按此引用）。
- 抽象类 `FilesystemCapabilityProvider(ToolCapabilityProvider)`，在工具 invoke 之外额外约定 workspace
  生命周期：
  - `register_session(session_id, workspace)`：host 在执行前调，**必须绝对路径**（相对 → raise），确保目录存在。
  - `deregister_session(session_id)`：session 结束注销映射（仅传 id）。
  - `workspace_for(ctx) -> str | None`：返回已登记的绝对路径。
  - `spill(content, ctx, name_hint="") -> str`：把超长工具输出落盘到该 session 的 workspace，返回路径；
    未登记 workspace 时 raise（调用方 gateway 据此回退硬截断）。
- 在 `protocols/__init__.py` 导出 `FS_PROVIDER_NAME / FilesystemCapabilityProvider / FsTool`。

### B2. 共享 `@tool` 装饰器工厂 `providers/_tooldecl.py`

原 `builtin_tool` 装饰器内联在 builtin provider 里、写死 `builtin:` 前缀。抽成
`make_tool_registry(provider_name) -> (tool_decorator, tools_dict, impls_dict)`：每个 provider 调一次得到
**独立**三元组（互不串扰），故 fs 与 builtin 各用自己的前缀。`tool(...)` 支持 `description` 覆盖（缺省取
docstring 首行）——fs 的 `bash_exec` 用它生成**平台感知**描述（见 B3）。

### B3. 新 provider `providers/capability_filesystem/`

- `FilesystemToolsProvider(FilesystemCapabilityProvider)`：实现 `bash_exec/read_file/write_file/glob`
  （从旧 builtin 整段搬来）+ workspace 映射 `dict[session_id -> abs_path]`。
- **workspace 锚定**：`invoke()` 把该 session 的 `workspace`（和可选 `allowed_dirs`）注入 `ctx.extra`；
  - `bash_exec` 以 workspace 为 **cwd**；
  - `read_file/write_file/glob` 用 `_resolve()` 把**相对路径锚定到 workspace**，绝对路径原样。
- **平台感知 bash 描述**（`_bash_exec_description()`）：据 `platform.system()` 生成不同描述，告诉 LLM
  Windows 走 cmd.exe（用 dir/type/findstr，ls/cat/grep 不可用）/ Linux/macOS 走 /bin/sh（用 POSIX 命令）。
  ——移植时按各自运行平台等价实现。
- `spill()`：写到 `{workspace}/tool_outputs/{safe_name}_{id}.txt`。
- 沿用安全约束：`_BASH_BLACKLIST`、bash 30s 超时、50KB 输出上限、读文件 500KB 上限、glob 500 条上限。
- `BuiltinToolsCapabilityProvider` 瘦身：仅留 `http_request`，删掉所有 fs 工具与 `allowed_dirs` 注入逻辑，
  改用 `make_tool_registry("builtin")`。

### B4. Host / 接线改动

- **CLI**（`cli.py`）：注册 `FilesystemToolsProvider()`（在 builtin 之前）；`bash_exec` 的人审授权器从
  `builtin:bash_exec` 改钉 `FsTool.BASH_EXEC`；新增 `run --workspace <绝对路径>`：执行前 `generate_id("ses")`
  + 找到 fs provider `register_session(session_id, workspace)`，并把 `session_id` 传给 `run_single_task`。
- **模板**（`providers/templates/loader.py` 示例 + 实际 SOUL.md frontmatter）：`builtin:bash_exec` →
  `fs:bash_exec`。
- **持久化**：`SessionModel` 加 `workspace: str | null` 列（host 自有数据、非事件派生）；
  `CreateSessionRequest` 加 `workspace: str | null` 字段。

**待移植清单（B 整组）**：
- TS：`protocols/filesystem.ts`、`providers/_tooldecl.ts`、`providers/capability_filesystem/`，builtin 瘦身，
  CLI/host 接线 + SSE/REST 的 workspace 字段、`fs:bash_exec` 模板/授权 id、DB 列。
- Java：同结构落到 `loomej-core`（fs provider + 协议 + tool registry 工厂）；host 接线视 Java host 现状。
- **id 字符串必须逐字一致**（`fs:bash_exec` 等）——模板、授权、event payload 跨语言共享同一份字符串协议。

**测试不变式**：
- `register_session` 拒绝相对路径（raise）、绝对路径建目录并登记；
- `bash_exec` 在 workspace 下执行（cwd 正确）；相对路径 read/write/glob 锚定到 workspace；
- `spill` 未登记 workspace 时 raise；登记后落盘到 `tool_outputs/`。

---

## 移植对照速查（按文件）

| Python（参考实现） | 主题 | Java / TS 对应物 |
|---|---|---|
| `core/orchestrator/hitl_manager.py` | A1 | `onColdResolve` + `resolve(resumeOnCold)` |
| `core/runtime.py` | A2/A4 | `recoverSession(askHumanReply)`、`_resumeAfterColdHitl`、`_injectAskHumanReply`、`recover()`、`rebuildHitl`、`rebuildAllPendingHitl`、`_pendingHitl`、`_emitSessionInterrupted` |
| `core/control/reducers.py` | A4 | `foldPendingHitl`、`HITL_STATUS_EVENT_TYPES`、`unresolvedHitlIds` |
| `core/state/event_store.py`（+ postgres） | A4 | `readSessionEventsOfTypes` |
| `core/loop/steps/_capabilities.py` | A3 | `resolveCapabilities` / `resolveAndBind`（共享） |
| `core/loop/steps/reason.py` / `reconcile.py` | A3 | reason 改用共享；reconcile 先绑定 + `next="reason"` |
| `src/ipmastercowork/api/{hitl,sessions,startup,deps,models/session}.py` | A5 | host 自愈 + consumer 重启 + recover 顺序 + SSE 重发 |
| `protocols/filesystem.py` | B1 | fs 协议 + `FsTool` 常量 |
| `providers/_tooldecl.py` | B2 | `makeToolRegistry` |
| `providers/capability_filesystem/provider.py` | B3 | fs provider + workspace |
| `providers/capability_builtin/provider.py` | B3 | builtin 瘦身至 http_request |
| `cli.py` / `templates/loader.py` / `postgres/models.py` / `schemas/sessions.py` | B4 | 接线 + `fs:` id + workspace 列/字段 |

> 跨语言**字符串协议**（务必逐字一致）：capability id（`fs:bash_exec`…、`control:submit_task_assessment`、
> `control:request_human_input`）、event type（`SESSION_STATUS_CHANGED`、`HITL_*`）、event payload 键
> （`new_status`、`approval_id`、`kind`、`capability_id`、`tool_call_id`）、memory metadata（`source: "hitl_reply"`）。
