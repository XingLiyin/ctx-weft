# 05 · 工具鉴权与 HITL

> 真相源：`protocols/capability.py`（契约）+ `providers/authorizer/`（实现）、`core/hitl/`、`core/loop/capability_gateway.py`、`core/loop/hitl_waiter.py`
> 行为黄金：`tests/unit/test_authorizer.py`、`test_gateway_authz_hitl.py`、`test_hitl_*.py`（三份实现须复现同一断言）
> HITL 机制本身见 `docs/spec/07-hitl-suspend-resume.md` 与权威设计
> `docs/superpowers/specs/2026-09-01-hitl-redesign-design.md`；升级须知见
> `docs/upgrade/2026-09-01-hitl-redesign.md`。

这是行为契约（非 reducer 投影），但同样要求三份实现语义一致。下面的"必须"项即跨语言一致性点。

---

## 1. Authorizer 协议

主方法对**一次调用**作授权决定：

```
authorize(capability, ctx, arguments?, *, tool_call_id="") -> AuthorizationDecision
AuthorizationDecision { allowed: bool, message: str | list[ContentPart] = "",
                        modified_arguments: dict | None = None,
                        needs_human: HitlAsk | None = None }
```

- `message`：反馈 / 拒绝指导，回灌给 LLM（allow / deny 都可带）。
- `modified_arguments`：allow 时的有效参数（`None` = 用原参）。
- `needs_human`：**声明**「这次调用要一个人来定」，并把要问什么（`HitlAsk`）一起说出来。
  authorizer **不自己等**——登记、等待、被驱逐时抛 `HitlPark` 全部归 gateway。
  非 `None` 时 `allowed` 必须为 `False`；gateway 先判 `allowed`，安全不变式不依赖本字段。
  取代了只能说「挂起」、说不出问什么的旧 `defer`（已删除）。
- 会问人的 authorizer 另外实现**可选**接口 `HumanGatedAuthorizer.on_decision(...)`：
  gateway 拿到人的决定后喂回给它去解释。声明了 `needs_human` 却没实现它 = 契约违例，
  gateway 出一条错误 tool result（**绝不**放行）。
- `ctx` 是 `ProviderContext`，携带 session/task/agent 标识与 `agent_template_id`；
  授权契约不依赖 core 的 Agent/Task 状态对象。

### 内置实现

| Authorizer | `authorize` 返回 |
|------------|------|
| `AllowAll` | `allowed=True`（默认） |
| `AllowList` | 按 `ctx.agent_template_id` 查 `allow_map`/`deny_map`；拦截时带 `deny_message` |
| `HumanConfirmation` | 返回 `needs_human=HitlAsk(form="approval", ...)`；`on_decision` 把 `message`/`modified_arguments` 透传进决定 |

**AllowList 规则（必须按此顺序）**：
- `cap.id ∈ deny_map[ctx.agent_template_id]` → `allowed=False, message=deny_message`。
- `allowed_set = allow_map.get(ctx.agent_template_id)`：
  - `None`（模板未登记）→ 放行。
  - 集合（含空集）→ 仅 `cap.id ∈ allowed_set` 放行；空集 = 全拦（拦截带 `deny_message`）。
- deny 优先于 allow。

**HumanConfirmation 规则**：`authorize()` 只返回
`AuthorizationDecision(allowed=False, needs_human=HitlAsk(form="approval", ...))` 就结束——
它不认识 registry、不认识协程、不等任何东西。gateway 登记 + 等待，拿到决定后调
`on_decision(...)`，由它翻译成最终决定：
`accepted` → `allowed=True` 且透传 `message` / `modified_arguments`；
`rejected` → `allowed=False` 且透传 `message`（拒绝指导）。
热窗口被驱逐 → gateway 抛 `HitlPark`（不是失败，见 spec/07 §2）。

**冷路径短路在 gateway，不在 authorizer**：`invoke` 顶上先查决定缓存
（`(session_id, tool_call_id, stage, invocation_key)` 四维，见 spec/07 §4），命中就连
`authorize()` 都不调，直接走 `on_decision`。authorizer 因此对热/冷一无所知。

---

## 2. CapabilityGateway 鉴权集成

`invoke(tool_name, arguments, state, ctx)` 的鉴权相关步骤（必须）：

1. 按名查 cap；未找到或 `kind != "tool"` → 返回 `is_error` 结果，文案含 `unknown tool`。
2. **鉴权**：`authorizer = _get_authorizer(cap.id)`；`decision = authorizer.authorize(cap, ctx.provider_ctx, arguments, tool_call_id=…)`。
   传入的必须是 `ProviderContext`，不是 loop 的 `LoopContext`。
   - `decision.allowed == False` → 返回 `is_error` 结果，**且绝不调用 provider.invoke**（关键安全不变式）。
     文案：有 `message` → `[Blocked by human: {message}]`（指导回灌）；否则 `[Error: capability 'X' not authorized]`。
     `message` 可以是 `list[ContentPart]`（人类审批时贴的图），拼前后缀走 `content_with_prefix/suffix`，对 `str` 逐字节原样。
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

## 3. HITL：机制已重做，本文档不再复述

2026-09-01 重设计**删除了 `HitlManager`**（`request` / `wait` / `approve` / `answer` /
`reject`、`_futures` 热冷判别、`pending_hitl` 投影、5 个 legacy 终态事件的活路径）。
取而代之：

- provider **声明**需要人（authorizer 返回 `NeedsHuman(ask)`，流式工具 yield `needs_human`），
  **等待权归 gateway**；
- 一个 host 应答入口 `CtxWeftRuntime.reply_to_hitl(HitlReply)`，按显式的 `delivery` 字段分流续跑；
- 一个只读列表入口 `CtxWeftRuntime.list_pending_hitl(session_id=None)`；
- 事件从 8 个收敛到 2 个：`HitlOpened` / `HitlResolved`。

**机制的完整说明在 `docs/spec/07-hitl-suspend-resume.md`，权威设计在
`docs/superpowers/specs/2026-09-01-hitl-redesign-design.md`（§10 是不变式清单）。**
升级须知（host 侧必读）：`docs/upgrade/2026-09-01-hitl-redesign.md`。

本文档只保留**授权侧**与 HITL 交界的那一小块契约，即上面的 §1 / §2。

## 4. 授权侧的端到端语义

`HumanConfirmationAuthorizer` 声明 `needs_human` → gateway 登记 approval 请求并等待 →
`on_decision` 把人的决定翻译成 `AuthorizationDecision`：

- `accepted` → 放行；gateway 用 `modified_arguments`（若有）执行 provider；
  `message` 作 `[Human note: …]` 并入结果（其中的图片 part 与工具结果的 part 一起进最终 content）。
- `rejected` → 拦截，**provider 不执行**；`message` 作 `[Blocked by human: …]` 回灌。
- 热窗口被驱逐 → `HitlPark` unwind → 发 `TaskAwaitingHuman{hitl_id}`，task 落
  `AWAITING_HUMAN`（不是 `SUSPENDED`——那个值现在只表示「等子任务」）；冷应答到达后经
  reconcile 重入 `gateway.invoke`，命中决定缓存执行（spec/07 §4）。
- **会话状态不在这条路上写**：task 落 `AWAITING_HUMAN` 后由 TM 报 `TaskQueueBlocked`。
  **已过期（2026-09-03 agent-centric 改造）**：下一步「`SessionRegistry` 发
  `SessionWaiting` → 会话 `WAITING`」已不成立——会话状态机整体删除，`SessionWaiting`
  已停发（转 L 档），`SessionRegistry`（原 `SessionManager`）不再据此推导任何状态。
  host 若要等价信息，改看该 task 所属 agent 的状态（`waiting_human`）。审批面板仍由
  `HitlOpened` 驱动，不受影响。详见
  `docs/upgrade/2026-09-03-agent-centric-interaction.md` 第 5 节。

改写参数同样过 `_sanitize`，审计 / memory 记录用实际执行的有效参数。

## 5. 跨语言一致性清单

TS / Java 实现必须复现：

- [ ] `authorize(cap, ctx: ProviderContext, arguments?, *, tool_call_id) -> AuthorizationDecision{allowed, message, modified_arguments, needs_human}`。
- [ ] AllowList 的 deny 优先、未登记模板放行、空集全拦三条规则（拦截带 `deny_message`）。
- [ ] `_get_authorizer` 三级解析顺序。
- [ ] **拦截时 provider.invoke 绝不被调用**（安全不变式，每一条路径都成立）；deny 的 `message` 作 `[Blocked by human: …]` 回灌。
- [ ] allow 时 `modified_arguments` 生效（过 `_sanitize`，审计/memory 用有效参数）；`message` 作 `[Human note: …]` 并入结果。
- [ ] `_sanitize` 的敏感 header 集合 + 不改原对象。
- [ ] `needs_human` 是**声明**：authorizer 不登记、不等待、不抛 park；三者全归 gateway。
- [ ] `HumanGatedAuthorizer` 是**可选**接口（加法式，不分叉基础签名）；声明了 `needs_human`
  却没实现它 = 契约违例 → 错误 tool result，不静默放行、也不静默降级。
- [ ] 决定缓存四维键 `(session_id, tool_call_id, stage, invocation_key)`；
  同 id 的**另一次**调用不得复用上一次的决定与 `modified_arguments`。
- [ ] 冷路径短路在 **gateway**（`invoke` 顶上），authorizer 对热/冷一无所知。

> HITL 机制本身的一致性清单见权威设计 §10（`docs/superpowers/specs/2026-09-01-hitl-redesign-design.md`）——
> 不在本文档重复一份，两份清单一旦漂移就没人知道该信哪一份。

> 与 reducer 的 golden 用例不同，鉴权/HITL 是带并发与超时的行为测试，宜在各语言以等价单测复现，
> 以 `test_authorizer.py` / `test_gateway_authz_hitl.py` / `test_hitl_*.py` 的断言为准绳。
