# 08 · 移植同步说明：HITL 冷应答闭环 + 文件系统 Provider 拆分

> 本文记录本轮在 **Python 参考实现**（`ctx-weft/` + `src/ipmastercowork/`）落地的两组改动，供
> **Java 版（`loomej-core/`）** 与 **TS 版（`ctx_weft-ts/`）** 对齐移植。
> 行为契约以本文为准；代码细节以 Python 源 + [spec/07](./07-hitl-suspend-resume.md) 为真相。
> 每条改动给出「为什么 / 契约 / 待移植清单 / 必须复刻的测试不变式」。

两组改动彼此独立，可分别移植：

- **A. HITL 热/冷分流在 core 内闭环 + 事件驱动的崩溃恢复**（对应 spec/07 §6/§9）。
- **B. 文件系统能力从 builtin provider 拆出，带 per-session workspace**。

---

## A. HITL 冷应答闭环 + 崩溃恢复 —— **本节描述的机制已被替换**

> 本节原先记录的是 `HitlManager` 时代的移植说明：`on_cold_resolve` 回调、
> `resolve(resume_on_cold=...)`、按 `capability_id` 判别两条续跑路、`pending_hitl` 投影、
> `_resume_after_cold_hitl` / `_inject_ask_human_reply`。**2026-09-01 的 HITL 重设计把这些
> 全部删除了，没有兼容路径。**

现在的形状（**移植请以此为准**）：

- **声明，不等待**：authorizer 返回 `AuthorizationDecision(allowed=False, needs_human=HitlAsk(...))`；
  流式工具 provider `yield CapabilityEvent("needs_human", {"ask": ...})`。登记 / 等待 / 抛 park
  全部在 `CapabilityGateway`，是全仓唯一一处。
- **一个应答入口**：`CtxWeftRuntime.reply_to_hitl(HitlReply)`。热/冷分流的判据是
  `PendingHitl.claimed`（由 `HitlService._commit` 在取走等待槽的同一原子段里写入），
  **不是** `was_hot`、也不是 future 是否存在。冷续跑由 `reply_to_hitl` 的**返回值**驱动，
  不挂总线订阅。
- **续跑路由按 `delivery`**（`ToolResultDelivery` / `UserTurnDelivery` / `NoResumeDelivery`），
  **不再按 `capability_id` 或 `form` 判别**。
- **一个只读列表入口**：`CtxWeftRuntime.list_pending_hitl(session_id=None) -> list[HitlRequestView]`。
- **事件从 8 个收敛到 2 个**：`HitlOpened` / `HitlResolved`；`SessionPausedHitl` 不再发出，
  会话暂停态由未决请求的 `delivery` 推导。
- **恢复是「喂进来」**：`fold_hitl_snapshot` → hydrate → `HitlRegistry.load_snapshot`；
  此后 core 只读内存，绝不回落 scan 日志。

完整说明见 [spec/07](./07-hitl-suspend-resume.md)，权威设计见
`docs/superpowers/specs/2026-09-01-hitl-redesign-design.md`（§10 是不变式清单，§12.3 是
旧事件的双读折叠规则——跨语言移植时**双读那一节才是本节原内容的替代品**）。
host 侧的破坏性变更清单见 `docs/upgrade/2026-09-01-hitl-redesign.md`。

**A3（reconcile 先绑 capability）仍然成立**，只有一处已变：`next_step` 现在是 `"prepare"`
（`ReasonStep` 已更名/重组为 `PrepareStep`），不是 `"reason"`。

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
| `core/hitl/{registry,service,reply_intake,status}.py` | A（重做后） | `HitlRegistry` / `HitlService` / `pausedStatusFor`——**取代已删除的 `hitl_manager.py`** |
| `core/loop/hitl_waiter.py` | A（重做后） | `HitlWaiter`（热等待；驱逐返回 null，不抛） |
| `core/runtime.py` | A2/A4 | `replyToHitl`、`listPendingHitl`、`recoverSession(userReply)`、`_resumeAfterHitl`、`recover()`、`rebuildHitl`、`_derivePausedStatus`、`_emitSessionInterrupted` |
| `core/control/reducers.py` | A4 | `foldHitlSnapshot`（双读新旧事件）、`HITL_FOLD_EVENT_TYPES` |
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
> （`new_status`、`hitl_id`、`outcome`、`claimed`、`delivery.kind`、`stage`、`invocation_key`、`tool_call_id`）、memory metadata（`source: "hitl_reply"`）。
