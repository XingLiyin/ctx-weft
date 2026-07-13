# HITL 数据结构重构（hitl_id 统一 + form 显式化 + 实体合并）Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把 HITL 请求 ID 三套叫法（`request_id`/`approval_id`）统一为 `hitl_id`，三种等待形态显式化为 `form: approval|question|wait` 字段，合并 `HitlRequest`/`HitlRequestView` 为单一实体，存量事件经 m004 一次性迁移。

**Architecture:** core（ctx-weft）彻底切到新命名、不留兼容读代码；host 冻结对前端的 REST/SSE 契约（`kind` 在边界从 `form` 派生）；存量事件 payload 由 host 启动期迁移 m004 改写。行为等价重构——既有测试群是回归网。

**Tech Stack:** Python 3.12 dataclass/Literal、FastAPI/pydantic、SQLAlchemy Core（迁移）、pytest（`uv run pytest`，Windows 下勿用裸 pytest）。

**Spec:** `docs/superpowers/specs/2026-07-05-hitl-id-unify-design.md`

## Global Constraints

- 分支：`refactor/hitl-id-unify`，从 master 拉出；core/host 同分支（事件 key 是共享契约）。
- 行为等价：不改热/冷分流、park/恢复语义；不改事件类型名（`"HitlRequired"` 等字符串不变）。
- 前端零改动：SSE `waiting_input` 字段名与取值、`GET /hitl/pending` 既有字段（`id/kind/status/...`）与取值逐字节不变；`form` 仅作为**新增**字段出现。
- core 全库不留 `approval_id`、不留 `kind`（HITL 语义的）、不留 `WAIT_FOR_USER_CAPABILITY_ID` 分支判断 / `endswith(":wait_for_user")`——唯一例外是 m004 迁移函数体内。
- `observe.py` 的 LLM 流式 `request_id` 是另一概念，**不要动**。
- 测试命令一律 `uv run pytest ...`（pyproject 已配 `pythonpath=["."]`）。
- 每个 Task 结束 commit；Task 2 提交后 host 侧暂时红是预期（Task 4 修复），core 测试红到 Task 3 修复——commit message 里注明。

---

### Task 1: 分支 + 新实体 `HitlRequest` 落 `state/models.py`

**Files:**
- Modify: `src/ctx_weft/core/state/models.py`（文件末尾追加）
- Test: `tests/unit/test_hitl_request_model.py`（新建）

**Interfaces:**
- Produces: `ctx_weft.core.state.models.HitlRequest`（字段见下）、`HitlForm = Literal["approval","question","wait"]`、`HitlStatus = Literal["pending","accepted","rejected","cancelled"]`。后续所有 Task 从这里 import。
- 本 Task 纯新增，不删旧代码——commit 后全库仍绿。

- [ ] **Step 1: 拉分支**

```bash
git checkout master
git checkout -b refactor/hitl-id-unify
```

- [ ] **Step 2: 写失败测试**

新建 `tests/unit/test_hitl_request_model.py`：

```python
"""HitlRequest 统一实体（spec 2026-07-05）：form 三形态 + 默认值。"""
from ctx_weft.core.state.models import HitlForm, HitlRequest, HitlStatus  # noqa: F401


def test_hitl_request_defaults():
    req = HitlRequest(id="hit_1", form="question", session_id="s1", task_id="t1")
    assert req.status == "pending"
    assert req.accepted is False
    assert req.arguments == {} and req.questions == []
    assert req.resolved_at is None
    assert req.resume_llm_account is None and req.resume_llm_model is None
    assert req.created_at is not None


def test_hitl_request_accepted_property():
    req = HitlRequest(id="hit_2", form="approval", session_id="s1", task_id="t1")
    req.status = "accepted"
    assert req.accepted is True


def test_hitl_request_three_forms():
    for form in ("approval", "question", "wait"):
        assert HitlRequest(id="x", form=form, session_id="s", task_id="t").form == form
```

- [ ] **Step 3: 跑测试确认失败**

Run: `uv run pytest tests/unit/test_hitl_request_model.py -v`
Expected: FAIL，`ImportError: cannot import name 'HitlRequest'`

- [ ] **Step 4: 实现**

`src/ctx_weft/core/state/models.py`：顶部 import 区（`from ctx_weft.protocols import ...` 之后）加一行：

```python
from ctx_weft.core.utils import now_utc
```

文件末尾追加：

```python
# ── HITL ──────────────────────────────────────────────────────────────────────

# 三种等待形态（spec 2026-07-05，替代旧 kind + capability_id sentinel 拼判）：
#   approval — 审批门控：放行/拒绝一次工具调用（HumanConfirmationAuthorizer 触发）
#   question — ask_user 结构化提问，答复回灌 LLM
#   wait     — act 纯文本暂停 / 软打断（wait_for_user 冷 park）
HitlForm = Literal["approval", "question", "wait"]
HitlStatus = Literal["pending", "accepted", "rejected", "cancelled"]


@dataclass
class HitlRequest:
    """一次 HITL 请求（含其解析结果）。内存态与事件回放投影共用的单一实体。

    form 决定语义与应答形态：approval 用 approve/reject；question/wait 用 answer/reject。
    host 据 form 决定 UI（批准/拒绝按钮 vs 答题输入框 vs 普通输入框）。
    """

    id: str                                       # 全局唯一，即 hitl_id
    form: HitlForm
    session_id: str
    task_id: str
    agent_id: str = ""
    capability_id: str = ""                       # approval: 被门控的工具；question: 触发提问的工具；wait: 保留 sentinel 值仅作信息
    tool_call_id: str = ""                        # 发起本次调用的 LLM tool_call id（短路门控的键）
    arguments: dict[str, Any] = field(default_factory=dict)
    question: str = ""                            # 展示给人类的问题（approval / wait 用）
    context: str = ""                             # wait 形态的来源（plain_text / interrupt / interrupt:edit）
    questions: list[dict[str, Any]] = field(default_factory=list)  # ask_user 的结构化批量问题（含 options/multi_select）
    status: HitlStatus = "pending"
    # 解析载荷
    message: str = ""                             # 人类附带的自由文本：答复 / 拒绝理由 / 备注
    modified_arguments: dict[str, Any] | None = None  # approval form：改写后的工具参数（暂仅记录，不生效）
    created_at: datetime = field(default_factory=now_utc)
    resolved_at: datetime | None = None
    # resume-time LLM 覆盖：冷应答触发 session resume 时用的当前所选模型（host 据 entry 传入），
    # 仅供本次 cold-resolve 转发给 recover_session，不入事件、不持久化。
    resume_llm_account: str | None = None
    resume_llm_model: str | None = None

    @property
    def accepted(self) -> bool:
        return self.status == "accepted"
```

- [ ] **Step 5: 跑测试确认通过**

Run: `uv run pytest tests/unit/test_hitl_request_model.py -v`
Expected: 3 PASS

- [ ] **Step 6: Commit**

```bash
git add src/ctx_weft/core/state/models.py tests/unit/test_hitl_request_model.py
git commit -m "feat(core): HitlRequest 统一实体落 state/models.py（form 三形态，纯新增）"
```

---

### Task 2: core 全量切换到 hitl_id + form + 统一实体

**Files:**
- Modify: `src/ctx_weft/core/orchestrator/hitl_manager.py`
- Modify: `src/ctx_weft/core/loop/park.py`
- Modify: `src/ctx_weft/core/loop/steps/act.py:606-617`
- Modify: `src/ctx_weft/core/auth/authorizer.py:109-125`
- Modify: `src/ctx_weft/core/orchestrator/control_capability.py:693-709`
- Modify: `src/ctx_weft/core/control/types.py`（删 `HitlRequestView`）
- Modify: `src/ctx_weft/core/control/reducers.py`
- Modify: `src/ctx_weft/core/runtime.py:1109,1226,1274`

**Interfaces:**
- Consumes: Task 1 的 `HitlRequest/HitlForm/HitlStatus`。
- Produces（后续 Task 依赖的精确签名）:
  - `HitlManager.request(form: HitlForm, session_id, task_id, *, capability_id="", arguments=None, question="", context="", questions=None, agent_id="", tool_call_id="") -> str`
  - `HitlManager.request_parked(form, session_id, task_id, *, capability_id="", arguments=None, question="", context="", agent_id="", tool_call_id="") -> str`
  - `HitlManager.wait/approve/answer/reject/cancel/get/_require(hitl_id: str, ...)`（参数名统一 `hitl_id`）
  - `HitlManager.rebuild_pending(pending: dict[str, HitlRequest]) -> None`
  - `HitlPark(hitl_id: str = "", tool_call_id: str = "")`，属性 `.hitl_id`
  - 事件 payload：`HITL_REQUIRED` = `{"hitl_id", "form", "capability_id", "tool_call_id", "question", "context", "arguments", "questions"}`；`SESSION_PAUSED_HITL` = `{"capability_id", "form"}`；各 resolve = `{"hitl_id"}`。**不再发 `kind`。**
  - `fold_pending_hitl(events) -> dict[str, HitlRequest]`
- 本 Task 提交后 core/host 测试预期红（Task 3/4 修复），commit message 注明。

- [ ] **Step 1: 重写 `hitl_manager.py` 的实体与签名**

按序做以下编辑（行号为重构前）：

a. 模块 docstring（1-21 行）：把「两种 **kind**」段落改写为三形态说明：

```python
"""HitlManager：Human-in-the-Loop。

一个机制（request → wait → resolve），三种 **form**：

  - ``approval``：审批门控——放行/拒绝一次工具调用，可带 ``modified_arguments``。
                  由 HumanConfirmationAuthorizer 在 CapabilityGateway 鉴权步触发。
  - ``question``：向人提问——取回人类文字 ``answer`` 回灌给 LLM。由 ask_user 工具触发。
  - ``wait``    ：act 纯文本暂停 / 软打断（wait_for_user 冷 park），回复经注入续跑。

状态：``pending → accepted | rejected | cancelled``

  - approval accepted（无改参）→ HitlApproved；（带改参）→ HitlModified
  - question/wait accepted     → HitlAnswered
  - rejected → HitlRejected；cancelled → HitlCancelled

超时语义（spec/07 §3/§7）：timeout_sec 到期 → 热→冷驱逐（移除 future，保留 pending）+
抛 HitlPark。答案后到时走冷 resume。HITL_TIMEOUT 事件定义保留但不再发出。

应答方式按 form：approval 用 ``approve``/``reject``；question/wait 用 ``answer``/``reject``。
host 据 ``request.form`` 决定 UI（批准/拒绝按钮 vs 答题输入框 vs 普通输入框）。
"""
```

b. 删 41-77 行的 `HitlKind`、`HitlStatus`、`@dataclass class HitlRequest`、`HitlApproval = HitlRequest` 别名（全库无引用，直接删），改为：

```python
from ctx_weft.core.state.models import HitlForm, HitlRequest, HitlStatus  # noqa: F401  (HitlStatus re-export 供既有 import)
```

同时删 TYPE_CHECKING 里的 `from ctx_weft.core.control.types import HitlRequestView`（37 行）。

c. `request()`（106-151 行）：参数 `kind: HitlKind` → `form: HitlForm`；构造 `HitlRequest(id=rid, form=form, ...)`；日志 `"HITL requested [%s]"` 的实参 `kind` → `form`；两处 payload 改为：

```python
        await self._emit(EventType.HITL_REQUIRED, req, payload={
            "hitl_id": rid, "form": form, "capability_id": capability_id,
            "tool_call_id": tool_call_id,
            "question": question, "context": context,
            "arguments": dict(arguments or {}),
            "questions": questions or [],
        })
        await self._emit(
            EventType.SESSION_PAUSED_HITL, req,
            payload={"capability_id": capability_id, "form": form},
        )
```

docstring 里「返回 request_id」→「返回 hitl_id」。

d. `request_parked()`（153-178 行）：参数 `kind` → `form`，转发 `form=form`；docstring 同步。

e. 全文件参数改名 `request_id` → `hitl_id`（`wait`/`approve`/`answer`/`reject`/`_stash_resume_llm`/`cancel`/`get`/`resolve_answer`/`resolve_approve`/`resolve_reject`/`_require` 及其函数体内引用）。`wait()` 里的抛出改为 `raise HitlPark(hitl_id=hitl_id, tool_call_id=req.tool_call_id)`。

f. `rebuild_pending()`（293-304 行）改为直存：

```python
    def rebuild_pending(self, pending: dict[str, HitlRequest]) -> None:
        """从 replayed view 的 pending_hitl 重建内存请求（spec/07 §9）。

        不建 future（_futures 空）→ 后续 answer/approve 自动走冷 resume；
        re-park（resume 后 reconcile 再 request 同一 tool_call_id）时由 request() 补 future。
        fold_pending_hitl 每次折叠都构造新对象,直存无别名风险。
        """
        self._requests.update(pending)
```

g. `_resolve()` 的 emit（354 行）：`payload={"approval_id": req.id}` → `payload={"hitl_id": req.id}`。

h. resolve 三方法与 `answer`/`approve` docstring 里的 "input-kind/approval-kind" 措辞改为 form 措辞（如 "question/wait form 应答"）。

- [ ] **Step 2: `park.py` 字段改名**

```python
class HitlPark(BaseException):
    """携带挂起所需的最小信息。"""

    def __init__(self, hitl_id: str = "", tool_call_id: str = "") -> None:
        super().__init__(f"HITL park: hitl={hitl_id} tool_call={tool_call_id}")
        self.hitl_id = hitl_id
        self.tool_call_id = tool_call_id
```

（`capability_gateway.py:157` 的 `HitlPark(tool_call_id=tool_call_id)` 只用关键字参数，无需改。）

- [ ] **Step 3: 三个发起方改传 form**

a. `act.py` `_park_wait_for_user`（606-617 行）：`request_parked(kind="input",` → `request_parked(form="wait",`；`raise HitlPark(request_id=rid)` → `raise HitlPark(hitl_id=rid)`。`capability_id=WAIT_FOR_USER_CAPABILITY_ID` 保留（仅作信息）。

b. `authorizer.py:114`：`kind="approval",` → `form="approval",`；114 行局部变量 `approval_id` 改 `hitl_id`（125 行 `wait(approval_id)` 跟改）。

c. `control_capability.py:699-709`：`kind="input",` → `form="question",`；局部变量 `approval_id` 改 `hitl_id`（709 行 `wait` 跟改）。693 行注释「cold reconcile 再入」保留。

- [ ] **Step 4: reducers/types 切换**

a. `control/types.py`：删除 `HitlRequestView` 类（70-80 行）；`RunStateView.pending_hitl` 改：

```python
    # Pending HITL requests folded from events (only unresolved; spec/07 §9)
    pending_hitl: dict[str, "HitlRequest"] = field(default_factory=dict)
```

文件顶部加 `from ctx_weft.core.state.models import HitlRequest`（state.models 不依赖 control，无环）。

b. `reducers.py:12`：import 行删 `HitlRequestView`；加 `from ctx_weft.core.state.models import HitlRequest`。

c. `fold_pending_hitl`（24-45 行）改为（注意：顺带把 payload 里的 arguments/questions 也带上——旧 View 丢弃它们是缺陷，合并实体后免费修复）：

```python
def fold_pending_hitl(events: list[Event]) -> dict[str, HitlRequest]:
    """折叠 HITL 事件 → 仍未解决的 {hitl_id: HitlRequest}（HitlRequired 减去各终态）。

    只需 HITL_STATUS_EVENT_TYPES 这几类事件即可,无需全量回放——崩溃恢复据此既判某 session 是
    "等人答复 / 崩溃中断",又(等人答复时)直接重建内存 HitlManager（spec/07 §9）。
    """
    pending: dict[str, HitlRequest] = {}
    for ev in events:
        p = ev.payload or {}
        rid = p.get("hitl_id", "")
        if not rid:
            continue
        if ev.type == EventType.HITL_REQUIRED:
            pending[rid] = HitlRequest(
                id=rid, form=p.get("form", "approval"),
                session_id=ev.session_id, task_id=ev.task_id or "",
                capability_id=p.get("capability_id", ""), tool_call_id=p.get("tool_call_id", ""),
                question=p.get("question", ""), context=p.get("context", ""),
                arguments=p.get("arguments") or {}, questions=p.get("questions") or [],
            )
        elif ev.type in _HITL_RESOLVE_TYPES:
            pending.pop(rid, None)
    return pending
```

`unresolved_hitl_ids` docstring（49 行）：「仍未解决的 hitl_id 集合」。

d. 快照序列化（137-144 行）`"kind": h.kind` → `"form": h.form`；反序列化（206-214 行）：

```python
    pending_hitl: dict[str, HitlRequest] = {}
    for rid, h in data.get("pending_hitl", {}).items():
        pending_hitl[rid] = HitlRequest(
            # 旧快照无 form（inspect/replay 工具数据,非恢复真相源）→ 缺省按 approval 降级读。
            id=h["id"], form=h.get("form", "approval"), session_id=h.get("session_id", ""),
            task_id=h.get("task_id", ""), capability_id=h.get("capability_id", ""),
            tool_call_id=h.get("tool_call_id", ""), question=h.get("question", ""),
            context=h.get("context", ""),
        )
```

（206 行原地的 `from ctx_weft.core.control.types import HitlRequestView` 局部 import 删除。）

e. `_apply` 的 `SESSION_PAUSED_HITL` 分支（362-371 行）：

```python
    elif t == EventType.SESSION_PAUSED_HITL:
        # 纯文本暂停(form=wait)= 软待命 PAUSED；ask_user/审批 = PAUSED_HITL。
        # 与 ProjectionUpdater 同语义（单一真相）。
        status = "PAUSED" if p.get("form") == "wait" else "PAUSED_HITL"
        view.session_status = status
        sess = view.sessions.get(ev.session_id)
        if sess is not None:
            sess.status = status
```

（删除 365 行的 `WAIT_FOR_USER_CAPABILITY_ID` 局部 import。）

f. `_apply` 的 `HITL_REQUIRED` 分支（465-478 行）：

```python
    elif t == EventType.HITL_REQUIRED:
        rid = p.get("hitl_id", "")
        if rid:
            view.pending_hitl[rid] = HitlRequest(
                id=rid,
                form=p.get("form", "approval"),
                session_id=ev.session_id,
                task_id=ev.task_id or "",
                capability_id=p.get("capability_id", ""),
                tool_call_id=p.get("tool_call_id", ""),
                question=p.get("question", ""),
                context=p.get("context", ""),
                arguments=p.get("arguments") or {},
                questions=p.get("questions") or [],
            )
```

（466 行局部 import 删除。）resolve 分支 483 行：`p.get("approval_id", "")` → `p.get("hitl_id", "")`。

- [ ] **Step 5: runtime.py 三处**

a. 1109 行：

```python
        is_inject = req.form == "wait"
```

（1106-1107 行 docstring 措辞顺带改为「form=wait 须把回复注入 task 层」。）

b. 1226 行 docstring：「只带 approval_id 的应答入口」→「只带 hitl_id 的应答入口」。

c. 1274 行 `_pending_hitl` docstring：`{id: HitlRequestView}` → `{id: HitlRequest}`。

- [ ] **Step 6: 全局扫尾确认**

Run: `grep -rn "approval_id\|HitlKind\|HitlRequestView\|HitlApproval\b\|kind=\"approval\"\|kind=\"input\"" ctx-weft/src/`
Expected: 无输出。

Run: `grep -rn "WAIT_FOR_USER_CAPABILITY_ID" ctx-weft/src/`
Expected: 仅 `control_capability.py:46`（定义）与 `act.py`（import + `capability_id=` 赋值），无任何 `==` / `endswith` 分支判断。

Run: `uv run pytest tests/unit/test_hitl_request_model.py -v`
Expected: PASS（新实体不受影响）。

- [ ] **Step 7: Commit**

```bash
git add ctx-weft/src/
git commit -m "refactor(core): HITL 切换 hitl_id+form 统一实体（事件 payload 换 key；tests 红至 Task3/4，host 红至 Task4）"
```

---

### Task 3: core 测试迁移 & 全绿

**Files:**
- Modify: `tests/unit/test_hitl*.py`（test_hitl / test_hitl_park / test_hitl_recovery / test_hitl_cold_resume / test_hitl_ask_human_cold / test_hitl_paused_status / test_hitl_request_parked / test_hitl_reconcile 等）
- Modify: `tests/unit/test_interrupt_*.py`、`test_recover_routing.py`、`test_superseded_task_manager.py`、`tests/integration/test_interactive_task.py`、`test_hitl_cold_input.py` 等含 HITL 引用的文件
- Test: 全部 `tests/`

**Interfaces:**
- Consumes: Task 2 的全部新签名。
- 迁移是机械映射，见 Step 1 的对照表；**断言的行为语义一律不变**。

- [ ] **Step 1: 按对照表机械迁移**

先列受影响文件：

Run: `grep -rln "approval_id\|HitlRequestView\|kind=\"approval\"\|kind=\"input\"\|request_id=" tests/`

对每个文件应用映射（严格按此表，不做其他改动）：

| 旧 | 新 |
|---|---|
| `HitlRequestView(` / `from ctx_weft.core.control.types import HitlRequestView` | `HitlRequest(` / `from ctx_weft.core.state.models import HitlRequest` |
| `request(kind="approval"` / `request_parked(kind="approval"` | `request(form="approval"` / `request_parked(form="approval"` |
| `request(kind="input"` / `request_parked(kind="input"`，且该调用带 `capability_id=WAIT_FOR_USER_CAPABILITY_ID`（或值以 `:wait_for_user` 结尾） | `form="wait"` |
| `request(kind="input"`（其余，即 ask_user 语义） | `form="question"` |
| `HitlRequestView(..., kind="approval"...)` 构造实参 | `form="approval"`（同上规则区分 wait/question） |
| payload 断言 `["approval_id"]` / `p.get("approval_id")` | `["hitl_id"]` |
| payload 断言 `["kind"]`（HITL 事件的） | `["form"]`（值按上表映射） |
| `HitlPark(request_id=` / `.request_id`（HitlPark 属性） | `HitlPark(hitl_id=` / `.hitl_id` |
| `req.kind` | `req.form` |

注意：`observe` 相关测试里的 `request_id`（LLM 流式）**不改**；测试辅助里作为 HitlManager 方法**位置实参**传的 id 无需改名。

- [ ] **Step 2: 跑 core 全量测试**

Run: `uv run pytest tests/ -x -q`
Expected: 全 PASS。若有失败，逐个核对是映射遗漏（改测试）还是 Task 2 的 src 缺口（改 src），修到全绿。

- [ ] **Step 3: 新增 reducer form 分流回归测试**

在 `tests/unit/test_hitl_paused_status.py` 追加（自包含，走纯函数 `reduce_events`）：

```python
import pytest

from ctx_weft.core.control.reducers import reduce_events
from ctx_weft.core.events.types import Event, EventType
from ctx_weft.core.utils import generate_id, now_utc


def _paused_event(session_id: str, form: str) -> Event:
    return Event(
        id=generate_id("evt"), run_id=None, sequence=0, session_id=session_id,
        type=EventType.SESSION_PAUSED_HITL, timestamp=now_utc(), task_id=None,
        payload={"capability_id": "whatever", "form": form},
    )


@pytest.mark.parametrize("form,expected", [
    ("wait", "PAUSED"),
    ("question", "PAUSED_HITL"),
    ("approval", "PAUSED_HITL"),
])
def test_session_paused_hitl_routes_by_form(form, expected):
    """SESSION_PAUSED_HITL 按显式 form 分流,不再看 capability sentinel。"""
    view = reduce_events([_paused_event("s1", form)], run_id="r1")
    assert view.session_status == expected
```

（若该文件既有测试对 Event 构造另有本地 helper，沿用其惯用法，断言不变：三个 form 分别得 PAUSED / PAUSED_HITL / PAUSED_HITL。）

再补 `rebuild_pending` 直存回归（追加到 `tests/unit/test_hitl_recovery.py`）：

```python
def test_rebuild_pending_stores_hitl_request_directly():
    """合并实体后 rebuild_pending 直存 HitlRequest,不再做字段搬运。"""
    from ctx_weft.core.orchestrator.hitl_manager import HitlManager
    from ctx_weft.core.state.models import HitlRequest

    mgr = HitlManager()
    req = HitlRequest(id="hit_1", form="question", session_id="s1", task_id="t1",
                      questions=[{"question": "q?"}], arguments={"a": 1})
    mgr.rebuild_pending({"hit_1": req})
    got = mgr.get("hit_1")
    assert got is req                      # 直存同一对象
    assert got.status == "pending"
    assert got.questions == [{"question": "q?"}] and got.arguments == {"a": 1}  # 不再丢字段
    assert mgr.list_pending(session_id="s1") == [req]
```

- [ ] **Step 4: 跑新测试 + 全量**

Run: `uv run pytest tests/unit/test_hitl_paused_status.py tests/ -q`
Expected: 全 PASS

- [ ] **Step 5: Commit**

```bash
git add tests/
git commit -m "test(core): HITL 测试迁移到 hitl_id+form；补 form 分流回归"
```

---

### Task 4: host 切换（API/SSE/投影/路由）

**Files:**
- Modify: `src/ipmastercowork/api/schemas/hitl.py`
- Modify: `src/ipmastercowork/api/schemas/__init__.py`（若 re-export `HitlApprovalResponse`）
- Modify: `src/ipmastercowork/api/hitl.py`
- Modify: `src/ipmastercowork/api/schemas/sessions.py:37-52`
- Modify: `src/ipmastercowork/api/sessions.py:177-187`
- Modify: `src/ipmastercowork/api/models/session.py:12,335-357,376-380`
- Modify: `src/ipmastercowork/persistence/postgres/projection_updater.py:24,65-69`

**Interfaces:**
- Consumes: core 新签名（`hitl.approve/answer/reject(hitl_id, ...)`、`req.form`、payload `form`/`hitl_id`）。
- Produces: `HitlPendingItem`（pydantic：`id/kind/status/capability_id/question/task_id/session_id/agent_id/form`）；`route_hitl_reply(form: str, content: str) -> tuple[str, str]`。
- **对前端不变式**：`GET /hitl/pending` 既有字段与取值不变（`kind` 由 form 派生：`"approval" if form=="approval" else "input"`）；SSE `waiting_input` 的 `kind/prompt/arguments/questions/task_title` 字段名与取值不变。

- [ ] **Step 1: schema 改名**

`api/schemas/hitl.py`：`HitlApprovalResponse` 整类改名 `HitlPendingItem` 并加 `form`：

```python
class HitlPendingItem(BaseModel):
    id: str
    kind: str          # 派生字段(前端契约冻结): approval→按钮 / input→文本框
    status: str
    capability_id: str
    question: str
    task_id: str
    session_id: str
    agent_id: str = ""
    form: str = ""     # 新增: approval | question | wait
```

Run: `grep -rn "HitlApprovalResponse" src/` — 把所有 import/引用改为 `HitlPendingItem`（含 `schemas/__init__.py` 的 re-export）。

- [ ] **Step 2: `api/hitl.py` 切换**

a. 路径参数与局部名 `approval_id` → `hitl_id`（三个端点装饰器 `"/{approval_id}/..."` → `"/{hitl_id}/..."`、函数签名、404 文案 `f"Approval {approval_id} not found"` → `f"HITL request {hitl_id} not found"`）。

b. `_reregister_workspace_for_approval(approval_id, hitl)` 改名 `_reregister_workspace_for_hitl(hitl_id, hitl)`（docstring 里 approval_id 措辞跟改），调用点跟改。

c. `list_pending` 的映射改为：

```python
    return [
        HitlPendingItem(
            id=a.id, kind=("approval" if a.form == "approval" else "input"),
            status=a.status, capability_id=a.capability_id,
            question=a.question, task_id=a.task_id, session_id=a.session_id,
            agent_id=a.agent_id, form=a.form,
        )
        for a in pending
    ]
```

d. `_heal_and_retry` docstring 里「只带 approval_id」→「只带 hitl_id」；`approve/answer/reject` 端点内变量 `approval` 改名 `req_resolved`（避免与 form 语义混淆，可选但推荐）。

- [ ] **Step 3: `route_hitl_reply` 换 form**

`api/schemas/sessions.py:37-52`：

```python
def route_hitl_reply(form: str, content: str) -> tuple[str, str]:
    """把一条自由文本回复映射为 (action, message)，action ∈ {approve, reject, answer}。

    - ``approval``       ：审批语义 = yes/no。按**首词**判定 approve/reject，其余文本作 message。
    - ``question``/``wait``：整段文字就是答复（``"no"`` 是否定**答复**，不是"拒绝问题"）→ 一律 answer。
                             显式拒答 / 中止不走这里（用 reject 端点 / interrupt）。
    """
    text = content.strip()
    if form != "approval":
        return ("answer", text)
    head = _SPLIT.split(text, maxsplit=1)
    first = head[0].lower()
    rest = head[1].strip() if len(head) > 1 else ""
    if first in REJECT_WORDS:
        return ("reject", rest)
    return ("approve", rest if first in APPROVE_WORDS else text)
```

`api/sessions.py:178`：`route_hitl_reply(req.kind, content)` → `route_hitl_reply(req.form, content)`。

- [ ] **Step 4: SSE 翻译换 form**

`api/models/session.py`：

a. 12 行删 `from ctx_weft.core.orchestrator.control_capability import WAIT_FOR_USER_CAPABILITY_ID`。

b. `HITL_REQUIRED` 分支（335-357 行）：

```python
        if t == EventType.HITL_REQUIRED:
            form = p.get("form", "question")
            # 纯文本暂停(form=wait): 软待命,不弹 HITL 面板 → 前端回落普通输入框。
            if form == "wait":
                self.status = "PAUSED"
                self.updated_at = _now()
                return self._session_update_json("PAUSED")
            self.status = "PAUSED_HITL"
            self.updated_at = _now()
            return json.dumps({
                "type": "waiting_input",
                # input_type=user_input → 前端渲染文本输入框
                "input_type": "user_input",
                # kind=approval → 前端渲染 Approve/Reject 按钮;input → 文本框（契约冻结,由 form 派生）
                "kind": "approval" if form == "approval" else "input",
                "prompt": p.get("question", "HITL approval required"),
                # approval 审批门控时把调用参数透传给前端,供人工判断是否放行
                "arguments": p.get("arguments") or {},
                # ask_user 的结构化批量问题(含 options/multi_select),前端据此渲染选项面板
                "questions": p.get("questions") or [],
                "task_title": p.get("capability_id", ""),
                "created_at": ts,
            })
```

c. `SESSION_PAUSED_HITL` 分支（376-380 行）：

```python
        if t == EventType.SESSION_PAUSED_HITL:
            self.status = "PAUSED" if p.get("form") == "wait" else "PAUSED_HITL"
            self.updated_at = _now()
            return self._session_update_json(self.status)
```

- [ ] **Step 5: 投影换 form**

`projection_updater.py`：24 行删 `WAIT_FOR_USER_CAPABILITY_ID` import；65-69 行：

```python
        elif t == EventType.SESSION_PAUSED_HITL:
            # 纯文本暂停(form=wait)= 软待命 PAUSED;ask_user/审批 = PAUSED_HITL。
            status = "PAUSED" if p.get("form") == "wait" else "PAUSED_HITL"
            await self._update_session(event.session_id, status=status)
```

- [ ] **Step 6: 扫尾确认**

Run: `grep -rn "approval_id\|HitlApprovalResponse\|WAIT_FOR_USER_CAPABILITY_ID\|\.kind\b" src/ipmastercowork/`
Expected: 无 HITL 语义残留（`.kind` 若命中其他领域用法逐个人工确认放行；`waiting_input` 里的 `"kind"` 字符串 key 是对外契约、保留）。

- [ ] **Step 7: Commit**

```bash
git add src/ipmastercowork/
git commit -m "refactor(host): HITL API/SSE/投影切 form+hitl_id,前端契约冻结(kind 边界派生)"
```

---

### Task 5: host 测试迁移 & 全绿

**Files:**
- Modify: `tests/test_hitl_*.py`、`tests/test_sessions_paused_routing.py`、`tests/test_sse_paused_hitl_resend.py`、`tests/test_session_entry_paused.py`、`tests/test_recovery_paused_hitl.py`、`tests/test_pause_during_resume_regression.py`、`tests/test_fs_write_authorizer.py`、`tests/test_selective_bash_authorizer.py` 等
- Test: 全部 `tests/`

**Interfaces:**
- Consumes: Task 4 的 host 新形态 + Task 3 的映射表（同一张表适用）。

- [ ] **Step 1: 列受影响文件并迁移**

Run: `grep -rln "approval_id\|HitlApprovalResponse\|HitlRequestView\|kind=\"approval\"\|kind=\"input\"\|route_hitl_reply\|WAIT_FOR_USER" tests/`

应用 Task 3 Step 1 的同一张映射表，外加 host 专属两条：

| 旧 | 新 |
|---|---|
| `HitlApprovalResponse` | `HitlPendingItem` |
| `route_hitl_reply("approval"/"input", ...)` 实参 | `"approval"` 不变；`"input"` → `"question"`（或 `"wait"`，按测试场景语义——wait_for_user 回复场景用 `"wait"`；两者行为同为 answer，断言不变） |

事件构造处（测试里手工拼 payload 的）：`"approval_id"` → `"hitl_id"`，HITL_REQUIRED/SESSION_PAUSED_HITL payload 补 `"form"` 字段（按场景取值）。

**断言注意**：对外契约断言（SSE `waiting_input` 的 `"kind"` 值、`/hitl/pending` 响应的 `kind` 字段）**保持断言旧值不变**——它们正是"前端契约冻结"的回归证明，只需让构造侧改用 form。

- [ ] **Step 2: 跑 host 全量测试**

Run: `uv run pytest tests/ -x -q`
Expected: 全 PASS（`test_migrations.py` 尚无 m004 用例，不受影响）。

- [ ] **Step 3: Commit**

```bash
git add tests/
git commit -m "test(host): HITL 测试迁移到 form+hitl_id;契约断言保持旧值以证冻结"
```

---

### Task 6: m004 存量事件迁移

**Files:**
- Modify: `src/ipmastercowork/persistence/postgres/migrations.py`
- Test: `tests/test_migrations.py`（追加）

**Interfaces:**
- Consumes: `EventModel`（`persistence/postgres/models.py:64-83`，`payload_json: Text`）；`run_pending` 既有骨架。
- Produces: `_m004_hitl_id_and_form(db) -> int`，注册 id `"m004_hitl_id_and_form"`。

- [ ] **Step 1: 写失败测试**

`tests/test_migrations.py` 追加（参照该文件既有 m001-m003 测试的 fixture/DB 构造惯用法——先读文件再落笔，以下为核心逻辑）：

```python
async def test_m004_rewrites_hitl_payloads(db_factory):
    """approval_id→hitl_id 换 key；HitlRequired/SessionPausedHitl 补 form；幂等。"""
    import json
    from ipmastercowork.persistence.postgres.migrations import run_pending
    from ipmastercowork.persistence.postgres.models import EventModel
    from sqlalchemy import select

    async with db_factory() as db:
        db.add_all([
            EventModel(id="e1", session_id="s1", type="HitlRequired", sequence=1,
                       payload_json=json.dumps({"approval_id": "h1", "kind": "input",
                                                "capability_id": "control:wait_for_user"})),
            EventModel(id="e2", session_id="s1", type="HitlRequired", sequence=2,
                       payload_json=json.dumps({"approval_id": "h2", "kind": "approval",
                                                "capability_id": "fs:bash_exec"})),
            EventModel(id="e3", session_id="s1", type="SessionPausedHitl", sequence=3,
                       payload_json=json.dumps({"kind": "input", "capability_id": "control:ask_user"})),
            EventModel(id="e4", session_id="s1", type="HitlAnswered", sequence=4,
                       payload_json=json.dumps({"approval_id": "h1"})),
            EventModel(id="e5", session_id="s1", type="TaskStarted", sequence=5,
                       payload_json=json.dumps({"approval_id": "unrelated"})),  # 非 HITL 类型:不碰
        ])
        await db.commit()

    await run_pending(db_factory)

    async with db_factory() as db:
        rows = {r.id: json.loads(r.payload_json) for r in
                (await db.execute(select(EventModel))).scalars()}
    assert rows["e1"] == {"hitl_id": "h1", "kind": "input",
                          "capability_id": "control:wait_for_user", "form": "wait"}
    assert rows["e2"]["hitl_id"] == "h2" and rows["e2"]["form"] == "approval"
    assert rows["e3"]["form"] == "question" and "hitl_id" not in rows["e3"]
    assert rows["e4"] == {"hitl_id": "h1"}
    assert rows["e5"] == {"approval_id": "unrelated"}  # 非 HITL 事件不动

    # 幂等:重跑 0 行(applied 标记直接跳过;绕开标记单独调函数也应 0 变更)
    from ipmastercowork.persistence.postgres.migrations import _m004_hitl_id_and_form
    async with db_factory() as db:
        assert await _m004_hitl_id_and_form(db) == 0
```

- [ ] **Step 2: 跑测试确认失败**

Run: `uv run pytest tests/test_migrations.py -v -k m004`
Expected: FAIL，`ImportError: cannot import name '_m004_hitl_id_and_form'`

- [ ] **Step 3: 实现 m004**

`migrations.py` 顶部 import 补 `import json`、`EventModel`。在 m003 之后追加：

```python
# HITL 事件 payload 键（spec 2026-07-05）：approval_id → hitl_id；HitlRequired/SessionPausedHitl
# 补显式 form。事件**类型名**不变。本函数是全库唯一允许出现旧 key 与 wait_for_user sentinel
# 判断的地方（core/host 代码已只认 hitl_id/form）。
_HITL_PAYLOAD_EVENT_TYPES = (
    "HitlRequired", "HitlApproved", "HitlAnswered", "HitlRejected",
    "HitlModified", "HitlTimeout", "HitlCancelled", "SessionPausedHitl",
)
_FORM_BACKFILL_TYPES = ("HitlRequired", "SessionPausedHitl")


async def _m004_hitl_id_and_form(db: AsyncSession) -> int:
    """HITL 事件 payload 迁移：approval_id 换 key 为 hitl_id；补 form 字段。

    form 推导 = 旧 kind + capability_id sentinel：kind=approval → approval；
    kind=input 且 capability_id 以 ':wait_for_user' 结尾 → wait；否则 → question。
    旧 kind 键保留在 payload 里（无害,新代码不读）。

    幂等：无 approval_id 且 form 已存在 → payload 不变、不计数。
    降级注意：迁移后旧版二进制读不懂 hitl_id,回滚需还原 DB 备份（与 m001-m003 约定一致）。
    返回改写行数。
    """
    rows = (await db.execute(
        select(EventModel.id, EventModel.type, EventModel.payload_json)
        .where(EventModel.type.in_(_HITL_PAYLOAD_EVENT_TYPES))
    )).all()
    changed = 0
    for eid, etype, payload_json in rows:
        try:
            p = json.loads(payload_json or "{}")
        except ValueError:
            continue  # 坏行不炸迁移
        orig = dict(p)
        if "approval_id" in p:
            p["hitl_id"] = p.pop("approval_id")
        if etype in _FORM_BACKFILL_TYPES and "form" not in p:
            if p.get("kind", "approval") == "approval":
                p["form"] = "approval"
            elif str(p.get("capability_id", "")).endswith(":wait_for_user"):
                p["form"] = "wait"
            else:
                p["form"] = "question"
        if p != orig:
            await db.execute(
                update(EventModel).where(EventModel.id == eid)
                .values(payload_json=json.dumps(p, ensure_ascii=False))
            )
            changed += 1
    return changed
```

注册（追加到 `MIGRATIONS` 列表末尾，勿动既有条目）：

```python
    ("m004_hitl_id_and_form", _m004_hitl_id_and_form),
```

- [ ] **Step 4: 跑测试确认通过**

Run: `uv run pytest tests/test_migrations.py -v`
Expected: 全 PASS（含既有 m001-m003 用例）

- [ ] **Step 5: Commit**

```bash
git add src/ipmastercowork/persistence/postgres/migrations.py tests/test_migrations.py
git commit -m "feat(host): m004 存量 HITL 事件迁移 approval_id→hitl_id + 补 form"
```

---

### Task 7: 验收扫描 + 全量验证 + 收尾

**Files:**
- 无新改动（只验证；发现缺口则回相应 Task 修）

- [ ] **Step 1: 验收 grep（对照 spec 验收标准 1-3）**

```bash
grep -rn "approval_id" ctx-weft/src/ src/ipmastercowork/ | grep -v migrations.py
grep -rn "request_id" ctx-weft/src/ | grep -v "loop/steps/observe.py" | grep -v background_observe
grep -rn "HitlRequestView" ctx-weft/ src/ tests/
grep -rnE "== *WAIT_FOR_USER_CAPABILITY_ID|endswith\(\":wait_for_user\"\)|endswith\(':wait_for_user'\)" ctx-weft/src/ src/ipmastercowork/ | grep -v migrations.py
```

Expected: 四条全部无输出。有输出 → 回对应 Task 补漏。

- [ ] **Step 2: 两侧全量测试（对照验收标准 4-6）**

Run: `uv run pytest tests/ tests/ -q`
Expected: 全 PASS

- [ ] **Step 3: 冷恢复端到端冒烟**

用 dev 库真实验证「带存量 HITL 事件的库升级后冷恢复正常」：先备份 `data/ipmc-dev.db`，若库里有历史 HITL 事件（`SELECT count(*) FROM events WHERE type LIKE 'Hitl%'` > 0），启动一次 host（跑 m004）后调 `GET /api/v1/hitl/pending` 确认能列出/为空不报错、日志无 rebuild 异常。库里没有 HITL 存量则以 Task 6 的迁移测试为准，跳过本步。

- [ ] **Step 4: 收尾**

调用 superpowers:finishing-a-development-branch 技能，走合并回 master 流程（按用户既定工作流：验证后合回 master，master 是唯一长期分支）。
