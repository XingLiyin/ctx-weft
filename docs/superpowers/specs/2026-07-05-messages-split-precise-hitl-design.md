# /messages 职责拆分 + 前端改走精确 HITL 接口

日期：2026-07-05
状态：设计已获批准
分支：`refactor/hitl-id-unify`（与 hitl_id+form 重构同分支，算同一特性的一部分；依赖其 form 字段、`HitlPendingItem`、hitl_id 命名）

## 背景与问题

1. **`/messages` 职责过载**（`api/sessions.py:455-537`）：一个端点按会话状态做四件事——RUNNING→409、INTERRUPTED→409、PAUSED*→HITL 应答（`_submit_hitl_response`）、终态→新 run 续聊。
2. **前端零流量走精确端点**：frontend-desktop（打包在用）与老 frontend/ 的 HITL 应答全部走 `/messages`——连 Approve/Reject 按钮都是发字面量文本 `"approved"`/`"rejected"` 靠词表解析（`frontend-desktop/src/components/ChatPanel.tsx:612-613`、`frontend/src/components/chat/ChatPanel.tsx:891`）；`/hitl/*` 三端点前端 0 引用。
3. **`/hitl/*` 端点是"裸 resolve"**：缺 `/messages` HITL 分支才有的三样会话副作用——重启 SSE consumer、追加 user 消息进 transcript、置 RUNNING 发 session_update——前端若直接调用会表现为"卡住"。
4. **多 pending 歧义**：`/messages` 固定解决 `pending[0]`（`sessions.py:177`），并行任务多条 pending 时用户回复被灌给最老一条。
5. **SSE `waiting_input` 不带 hitl_id**：前端拿不到精确应答所需的 id（`ChatWaitingInput` 无此字段）。

## 目标

1. HITL 应答的会话副作用 + resolve 下沉为**共享服务**，`/hitl/*` 端点成为完整可用的第一公民入口。
2. `/messages` 的 PAUSED* 分支瘦身为薄委托（deprecated，逻辑零重复）。
3. SSE `waiting_input` 增发 `hitl_id` + `form`（additive）。
4. 前端（frontend-desktop + frontend/，**不含 v2**）应答改走 `/hitl/{hitl_id}/answer|approve|reject`，带兜底链。

## 非目标

- 不动 core（ctx-weft）任何代码。
- 不改 `/messages` 其余分支（RUNNING/INTERRUPTED 409、终态续聊）。
- 不做软待命（wait）的多 pending 选择 UI——多条 wait pending 时保持"第一条"语义（与现状 `pending[0]` 等价）。
- 不修"重启后 recover 对 wait-form 会话统一发 PAUSED_HITL"的既有状态失真（`runtime.py:1201`，记为后续小修：按 form 分流发 PAUSED/PAUSED_HITL）。
- 不改 frontend-desktop-v2（重构中，按本 spec 契约自行实现）。
- LLM 选择行为保持现状等价：resolve 服务从 entry 读当前值（与今天 `/messages` 路径一致），`/hitl` 端点 body 不加 llm 参数。

## 设计

### 1. 后端：共享服务 `api/hitl_service.py`

```python
async def resolve_hitl(
    hitl_id: str,
    action: str,                # "answer" | "approve" | "reject"
    *,
    text: str = "",             # answer 的答复原文
    message: str = "",          # approve/reject 的可选备注
    modify: dict | None = None, # approve 的改参
) -> HitlRequest               # KeyError → 调用方转 404
```

序列（从 `_submit_hitl_response` 提炼，顺序不变）：

1. **定位**：`hitl.get(hitl_id)`；miss → `runtime.rebuild_all_pending_hitl()` 自愈再取（吸收现 `_heal_and_retry` 语义）；仍 miss → KeyError。
2. **会话副作用**（按 `req.session_id` 找 entry；entry 在时）：
   a. 追加 user 消息进 transcript：answer=答复原文；approve/reject=`"approved"`/`"rejected"`（+空格+message 若有）——与现前端按钮字面量一致，`pendingHitlRef` 气泡配对不破；
   b. 置 entry RUNNING + `session_update` SSE；
   c. `_consumer_token++` 重启 session_consumer（**resolve 前**，先订阅 bus 再产生事件）；
   d. `_ensure_workspace_registered`；
   e. `llm_kw` 取 entry 当前 `llm_account/llm_model`。
   entry 不在（纯 API 调用、无前端会话）：跳过 a-e，log warning，resolve 照走（HitlManager 冷恢复闭环仍生效）。
3. **resolve**：按 action 调 `hitl.answer/approve/reject(hitl_id, ..., **llm_kw)`。

依赖方向：`hitl_service` 依赖 deps + sessions 的 entry 设施；`api/hitl.py` 与 `api/sessions.py` 都只依赖 `hitl_service`（消除现 `hitl.py` late-import `sessions._ensure_workspace_registered` 的别扭）。`_ensure_workspace_registered` 若因此产生循环 import，将其一并移入 `hitl_service`（或独立 util），`sessions.py` 其余三个调用点改 import 路径。

### 2. 后端：`/hitl/{hitl_id}/answer|approve|reject`

改为调 `resolve_hitl`。请求/响应 schema 不变（`AnswerRequest/ApproveRequest/RejectRequest` → `{id, status}`）。`_heal_and_retry`、`_reregister_workspace_for_hitl` 被服务吸收后删除。`GET /hitl/pending` 不变（已带 form）。

### 3. 后端：`/messages` 薄委托（deprecated）

PAUSED* 分支改为：`list_pending(session_id)`（空则 `rebuild_hitl` 自愈）→ 取 `pending[0]`（保留现语义；注释标注多 pending 歧义 + deprecated + 指路 `/hitl`）→ `route_hitl_reply(req.form, content)` → `resolve_hitl(req.id, action, ...)`。`_submit_hitl_response` 的副作用代码删除（在服务里）。其余分支不动。

### 4. 后端：SSE `waiting_input` 增发字段

`api/models/session.py` 的 `HITL_REQUIRED` 翻译分支增发 `"hitl_id": p.get("hitl_id", "")` 与 `"form": form`（additive；`kind` 等既有字段与取值不动）。重连补发走同一 snapshot 自动带上。重构升级前的旧 snapshot 无 hitl_id → 前端兜底链覆盖。

### 5. 前端（frontend-desktop 与 frontend/ 同构改造，各自一遍）

a. **新 `api/hitl.ts`**：
```ts
hitlApi = {
  pending: (sessionId) => GET `/hitl/pending?session_id=${sessionId}`,
  answer:  (hitlId, answer)   => POST `/hitl/${hitlId}/answer`,
  approve: (hitlId, modify?)  => POST `/hitl/${hitlId}/approve`,
  reject:  (hitlId, message?) => POST `/hitl/${hitlId}/reject`,
}
```

b. **SSE 类型与透传**：`ChatWaitingInput` 增 `hitl_id?: string`、`form?: string`；waiting_input 事件处理透传。

c. **应答分流（ChatPanel）**：
- 面板应答（按钮/答题/文本框）：`waitingInput.hitl_id` 存在 → 对应 `hitlApi.approve/reject/answer`；
- PAUSED 软待命（无面板，含人为打断后的回复）：composer 提交时 `hitlApi.pending(sessionId)` → 取 `form=="wait"` 第一条 → `hitlApi.answer`；
- **兜底链**：无 hitl_id（旧 snapshot）→ `pending()` 按 form 匹配再精确调用 → 仍拿不到 → 退回旧 `answerInput`（`/messages` 薄委托）；`/hitl` 404（id 失效）→ 重新 `pending()` 试一次 → 失败提示刷新；
- 应答成功后 invalidate/气泡配对逻辑不变。

d. 老 `frontend/` 同三步，但**只迁面板路径**（`WaitingInputArea`/`BashExecConfirmArea`）；其 PAUSED 软待命回复走普通 composer → `sendMessage`（`/messages` 薄委托），刻意不迁——遗留 UI 的成本收益取舍。

## 设计修订（规划期发现，2026-07-05b）

现状两点与原设计"端点 body 不加 llm 参数"冲突：

1. `/messages` 的 PAUSED* 分支会按 body 的 `llm_account/llm_model` 更新 entry（`sessions.py:479-484`）——"随回复切模型"依赖 body 传参；前端 PAUSED 软待命回复走 `sendMessage`（带 llm）。
2. 前端面板应答（`answerInput` 不带 llm）今天会把 entry LLM **重置回默认**（`req.llm_account` 为 None → 走 else 重置分支）——隐性 bug。

修订：`/hitl` 三端点 body 增加**可选** `llm_account/llm_model` 字段。语义：字段**出现在请求里**（pydantic `model_fields_set` 判定）才生效——truthy account → 设 entry 两值；显式 null account → 重置两值为 None（与 `/messages` 同）；字段缺席 → **不动 entry**（顺带修复面板应答重置 bug）。前端 composer wait 路径带当前选择；面板按钮/答题不带。

## 行为不变式

- SSE 事件形状只增字段；`/hitl` 响应形状不变；`/messages` 其余分支不变。
- transcript 中应答仍以 user 消息呈现（配对不破）。
- 重复点击安全：`HitlManager._resolve` 幂等（已解决→no-op）。
- 打断（wait form）语义完整保留：`context="interrupt:edit"` 的"上一条取消"说明等在 core 注入时生效，不受影响。
- 打断落地异步性不变：RUNNING 期间发消息仍 409、查不到 wait pending，前端等 `session_update(PAUSED)` 再发（现状即如此）。

## 测试

- **host**：`resolve_hitl` 服务单测（transcript 追加/状态翻转/consumer token 自增/workspace 重登记的顺序、entry-miss 分支、自愈定位；复用 `test_hitl_reply_self_heal` stub 惯用法）；三端点 × 三 form 集成测试（走服务后前端可见副作用齐全）；`/messages` 薄委托等价（现有 `test_hitl_reply_routing`/`test_sessions_paused_routing`/`test_hitl_reply_self_heal` 保持绿）；SSE `waiting_input` 含 `hitl_id`/`form` 断言（扩展 `test_session_entry_paused`）；`test_hitl_pending_contract` 保持。
- **前端**：frontend-desktop 补 waiting_input 透传 hitl_id 的单测（沿既有测试基建）；两前端手动冒烟：三形态应答、人为打断后回复、重启冷应答、兜底链。

## 验收标准

1. 前端正常流量下 HITL 应答全部走 `/hitl/{id}/*`；`/messages` 仅剩兜底流量。
2. `/hitl` 三端点单独 curl 即可完成冷应答，且前端 SSE 正常续流（consumer 重启生效）。
3. 多 pending 场景，面板应答精确命中所属那条（不再是 pending[0]）。
4. `waiting_input` SSE 携带 `hitl_id`/`form`；既有字段与取值逐字节不变。
5. host 侧 `_submit_hitl_response` 与 `/hitl` 端点无重复的副作用逻辑（单一 `resolve_hitl` 源）。
6. 两侧测试全绿（master 既有 5 红除外）；两前端构建通过。
