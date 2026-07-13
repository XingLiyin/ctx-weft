# HITL 数据结构重构：hitl_id 统一 + form 显式化 + 实体合并

日期：2026-07-05
状态：设计已获批准
分支：`refactor/hitl-id-unify`（core/host 同分支同 PR，事件 key 是两层共享契约）

## 背景与问题

同一个 HITL 请求 ID 目前有三套叫法：

| 层 | 叫法 | 位置 |
|---|---|---|
| core 内存 API | `request_id`（实体 `HitlRequest.id`） | `hitl_manager.py` 全部方法签名、`HitlPark(request_id=...)` |
| 事件 payload（持久化） | `"approval_id"` | `hitl_manager.py:141,354` 发射、`reducers.py` 5 处读取 |
| host API/schema | `approval_id` / `HitlApprovalResponse` | `/hitl/{approval_id}` 路径、`api/hitl.py`、SSE 翻译 |

加重混乱的因素：

1. **"approval" 同时是 kind 的取值之一**（`kind: approval | input`）——一条 `kind="input"` 的提问请求，其 ID 却叫 `approval_id`，语义错误。
2. **`request_id` 在 core 内撞名**——`loop/steps/observe.py` 用 `request_id` 指 LLM 流式请求 ID，与 HITL 无关。
3. **三种等待形态靠拼凑判定**——approval（审批门控）/ ask_user（结构化提问）/ wait_for_user（纯文本暂停）由 `kind` + `capability_id.endswith(":wait_for_user")` sentinel 组合推断（`runtime.py` `_resume_after_cold_hitl` 的 endswith 判定、`reducers.py` `SESSION_PAUSED_HITL` 的 sentinel 分流、host `projection_updater.py` 与 `api/models/session.py` 同款判断）。
4. **同一实体两套 dataclass**——`HitlRequest`（`orchestrator/hitl_manager.py`，内存态）与 `HitlRequestView`（`control/types.py`，回放投影态），`rebuild_pending` 手写字段搬运。

## 目标

1. 三层 ID 统一为 `hitl_id`。
2. 等待形态显式化为 `form: approval | question | wait` 字段，消灭全部 sentinel 分支。
3. 合并 `HitlRequest`/`HitlRequestView` 为单一 dataclass。
4. host schema 改名（`HitlApprovalResponse` → `HitlPendingItem`）。
5. 存量事件经 m004 一次性迁移，core 不留任何兼容读代码。

## 非目标

- 不动热/冷分流、park/恢复语义——纯数据结构与命名重构，**行为等价**。
- 不改事件类型名（`HITL_REQUIRED` 等 EventType 字符串不变，避免迁移面扩大到事件类型列）。
- 不做事件 payload 全量快照化（resolve 类事件仍只带 id；已评估，收益仅调试便利，不值契约变更成本）。
- 前端零改动（契约由 host 冻结，见"host 前向兼容"）。

## 设计

### 1. 核心实体：合并后的 `HitlRequest`

`HitlRequestView` 删除。`HitlRequest` 下沉到中立层 `core/state/models.py`（与 Session/Task 同居），orchestrator 与 control/reducers 都从这里 import——解决 reducers 反向依赖 orchestrator 的分层问题。

```python
HitlForm = Literal["approval", "question", "wait"]

@dataclass
class HitlRequest:
    id: str                    # 全局唯一，即 hitl_id
    form: HitlForm             # 替代 kind + capability_id sentinel
    session_id: str
    task_id: str
    capability_id: str = ""    # approval=被门控工具；question=control:ask_user；wait=保留 sentinel 值，仅作信息
    tool_call_id: str = ""
    question: str = ""
    questions: list = field(default_factory=list)
    context: str = ""          # wait 形态的来源（如 interrupt:edit）
    status: HitlStatus = "pending"
    modified_arguments: dict | None = None
    message: str = ""
    resolved_at: datetime | None = None
    resume_llm_account: str | None = None   # 冷续跑暂存，不入事件
    resume_llm_model: str | None = None
```

**form 与旧概念映射**：

- `kind="approval"` → `form="approval"`
- `kind="input"` + 普通 capability → `form="question"`
- `kind="input"` + `capability_id.endswith(":wait_for_user")` → `form="wait"`

`kind` 在 core 彻底删除；host 边界向前端派生：`kind = "approval" if form == "approval" else "input"`。

### 2. 命名统一规则

| 位置 | 现在 | 统一后 |
|---|---|---|
| core 方法签名/局部变量 | `request_id` / `approval_id` 混用 | `hitl_id` |
| `HitlPark` 字段 | `request_id` | `hitl_id` |
| 事件 payload key | `"approval_id"` | `"hitl_id"` |
| host 路径参数 | `/hitl/{approval_id}` | `/hitl/{hitl_id}`（按位置匹配，客户端无感） |
| host schema | `HitlApprovalResponse` | `HitlPendingItem` |

`observe.py` 的 LLM 流式 `request_id` 不动。

### 3. core 改动清单（按文件）

- **`state/models.py`**：新增 `HitlRequest` + `HitlForm`/`HitlStatus`。
- **`orchestrator/hitl_manager.py`**：删本地 `HitlRequest` 定义改 import；`request(form=...)` / `request_parked(form=...)`（删 `kind` 参数）；全部 `request_id` 参数改 `hitl_id`；`_emit` payload 改 `{"hitl_id": req.id}`，`HITL_REQUIRED`/`SESSION_PAUSED_HITL` payload 增发 `form`；`rebuild_pending(pending: dict[str, HitlRequest])` 直接存储，删字段搬运。
- **`control/types.py`**：删 `HitlRequestView`；`ControlView.pending_hitl: dict[str, HitlRequest]`。
- **`control/reducers.py`**：`HITL_REQUIRED` 直接构造 `HitlRequest`（读 `p["hitl_id"]`、`p["form"]`）；`SESSION_PAUSED_HITL` 分流改 `p.get("form") == "wait"` → PAUSED、否则 PAUSED_HITL（删 sentinel 比对）；resolve 类读 `hitl_id`；`fold_pending_hitl` 同步。
- **`loop/park.py`**：`HitlPark(hitl_id=..., tool_call_id=...)`。
- **`loop/steps/act.py`**：`_park_wait_for_user` 调 `request_parked(form="wait", ...)`；`HitlPark` 字段名跟改。
- **`auth/authorizer.py`**：`request(form="approval")`；局部变量 `approval_id` → `hitl_id`。
- **`orchestrator/control_capability.py`**：`request(form="question")`；`WAIT_FOR_USER_CAPABILITY_ID` 常量保留（仍作 wait 请求的 capability_id 值），但全库不再有代码以它做分支判断。
- **`runtime.py`**：`_resume_after_cold_hitl` 的 `is_inject` 改 `req.form == "wait"`（删 endswith）；`rebuild_hitl`/`parked_task_ids` 处跟改字段名。

### 4. host 改动清单 + 前向兼容

兼容原则：**REST/SSE 对外契约冻结，`form` → 旧 `kind` 在边界派生，前端零改动**。

- **`api/hitl.py`**：路径参数改 `{hitl_id}`；`HitlApprovalResponse` → `HitlPendingItem`，响应字段保持 `id/kind/status/...` 不变（`kind` 派生），新增 `form` 字段（additive，前端可渐进采用）。
- **`api/schemas/sessions.py`**：`route_hitl_reply(form, text)`——`approval` 走 approve/reject 词表，`question`/`wait` 一律 answer（行为等价）。
- **`api/models/session.py`**：`translate_event` HITL 分支改读 payload `form`（`wait` → PAUSED 不弹面板；其余发 `waiting_input`）；SSE payload 字段名（`kind/prompt/arguments/questions`）不变，`kind` 值派生。
- **`persistence/postgres/projection_updater.py`**：`SESSION_PAUSED_HITL` 分流改读 `form`。
- **`api/sessions.py`**：`_submit_hitl_response` 里 `req.kind` → `req.form` 传给 `route_hitl_reply`。

### 5. m004 迁移

追加到 `persistence/postgres/migrations.py` 的 `MIGRATIONS`（幂等、`applied_migrations` 门控，与 m001-m003 同模式）。扫描 8 种事件类型（7 个 `HITL_*` + `SessionPausedHitl`）的 payload：

1. `approval_id` → 改 key 为 `hitl_id`；
2. `HITL_REQUIRED` / `SessionPausedHitl`：按旧 `kind` + `capability_id` sentinel 规则补写 `form` 字段（迁移是唯一允许 sentinel 判断存在的地方）；
3. 已有 `hitl_id` 的行跳过（幂等）。

**降级注意**：m004 跑过后旧版二进制读不懂 `hitl_id` key，回滚需还原 DB 备份——与 m001-m003 约定一致，在迁移 docstring 注明。

### 6. 测试策略

- core 既有 HITL 测试群（`test_hitl*.py`、`test_interrupt*.py`、`test_recover_routing.py` 等）跟随改名，断言行为不变——作为行为等价的回归网。
- 新增：`form` 三形态各自的 reducer 分流测试（替代原 sentinel 测试）；`rebuild_pending` 直存测试。
- host：`test_migrations.py` 加 m004 用例（幂等重跑、老 payload 补 form、已迁移跳过）；`test_hitl_projection.py`/`test_sse_paused_hitl_resend.py` 验证 SSE 对外 `kind` 值不变。
- 全量 `uv run pytest`（两侧 tests）。

## 验收标准

1. 全库（core src + host src）`approval_id` 零出现（m004 迁移函数内除外——它必须引用旧 key 才能改写）；`request_id` 仅剩 LLM 流式用法（observe.py / act.py / background_observe.py / protocols/context.py），与 HITL 无关。
2. 全库不存在 `WAIT_FOR_USER_CAPABILITY_ID` 分支判断与 `endswith(":wait_for_user")`（m004 迁移函数内除外）。
3. `HitlRequestView` 不存在；`pending_hitl` 值类型为 `HitlRequest`。
4. SSE `waiting_input` 与 `GET /hitl/pending` 对外字段与取值和重构前逐字节一致（`form` 为新增字段除外）。
5. m004 对旧 payload 迁移正确且幂等；带存量 HITL 事件的库升级后冷恢复（rebuild_hitl / recover_session）工作正常。
6. 两侧测试全绿。
