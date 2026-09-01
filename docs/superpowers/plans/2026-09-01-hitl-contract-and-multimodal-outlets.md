# HITL 契约两维化 + 多模态出口贯通 · 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把 `HitlRequest` 的状态从 approval 专用的四值 `status` 换成开放值域的 `outcome`（`resolved` / `accepted` 推导），并把三个 HITL form 的多模态出口拉齐——人贴的图能进 prompt，进不去时留占位而不是静默消失。

**Architecture:** 七个任务，每个独立可测、独立提交。任务 1 是一次原子的契约迁移（`status` 字段消失，漏改处必然 `AttributeError`，不会静默）。任务 3（冷恢复 event→memory ref 转换）**必须先于**任务 5（ask_user 透传 parts）——今天 ask_user 把图展平的 bug 正遮蔽着「冷恢复还原的是 event 侧 ref」这颗雷，先透传就会把 memory 永远打不开的引用写进工具结果。任务 2 的共享助手被任务 5、6 消费。

**Tech Stack:** Python 3.11、pytest / pytest-asyncio、dataclasses、ruff。无新依赖。

**Spec:** `docs/superpowers/specs/2026-09-01-hitl-contract-and-multimodal-outlets-design.md`

## Global Constraints

- **纯文本路径逐字节不变。** 每个改动点都必须保证：`content` 是 `str` 时行为与改造前完全一致（同一对象、同一字节）。每条新增测试都要配一个纯文本对照断言。
- **事件 payload / 快照 / golden 零变更。** 不改事件类型、不改 `payload` 形状、不改 `serialize_view` / `deserialize_view`。旧日志与旧快照必须原样可读。
- **非文本 part 的判据冻结为 `not hasattr(p, "text")`**（spec 2026-08-20 §13）。任何新代码不得另写一份 isinstance 判据。
- **两侧 ref 命名空间独立。** event 侧 ref 与 memory 侧 ref 互不相通，core 从不比较、从不拿一侧的 ref 去另一侧解（spec 2026-08-27 dual-blob-store）。
- **`""` 是 `outcome` 的未决哨兵。** host 自定义 outcome 不得使用空串。
- 运行测试一律用 `.venv/Scripts/python.exe -m pytest`（Windows）。
- 提交信息用中文正文，结尾附 `Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>`。
- **已知的三条既有失败**（`master` 上同样红，与本计划无关，不要试图修）：
  `tests/integration/test_compact_flow_e2e.py::test_multiround_retry_accumulates_then_l3_collapses_e2e`、
  `tests/unit/test_golden_conformance.py::test_golden_dir_present`、
  `tests/unit/test_observe_outcomes.py::test_default_role_prompt_uses_two_fields`。
- 跑全量前先清 `__pycache__`（若曾 checkout 过 master）：`find src -name __pycache__ -type d -exec rm -rf {} +`，否则两条「已删除包不得复活」的守卫测试会假红。

---

## File Structure

| 文件 | 责任 | 任务 |
|------|------|------|
| `src/ctx_weft/protocols/hitl.py` | HITL 请求契约（host-facing）。`outcome` 开放值域 + 推导属性 | 1、7 |
| `src/ctx_weft/protocols/__init__.py` | 导出面：`HitlStatus` 出、`HitlOutcome` + 三常量进 | 1 |
| `src/ctx_weft/core/orchestrator/hitl_manager.py` | HITL 机制。6 处 pending 判据 + 4 处终局写入 | 1、4、7 |
| `src/ctx_weft/core/control/reducers.py` | 事件折叠。`fold_cold_hitl_decision` 的终局赋值 | 1 |
| `src/ctx_weft/core/orchestrator/control_capability.py` | ask_user 的 HITL 分支与出口 | 1、5 |
| `src/ctx_weft/core/runtime.py` | `_inject_user_reply` 的 rejected 判据；`_cold_hitl_decision` 的 ref 转换 | 1、3 |
| `src/ctx_weft/core/content.py` | 内容归一层。新增 `split_for_tool_result` | 2 |
| `src/ctx_weft/protocols/capability.py` | 授权契约。`message` 放宽、删 `filter` | 4、6 |
| `src/ctx_weft/providers/authorizer/human.py` | 内置审批 authorizer。改走 `defer`、透传 parts | 4、6 |
| `src/ctx_weft/core/loop/capability_gateway.py` | 两处 `decision.message` 拼接改走 `content_with_prefix/suffix` | 6 |
| `docs/spec/05-authz-and-hitl.md` | 权威跨语言 spec。状态机词汇 + `filter` 行 | 4、7 |
| `README.md` | 升级须知新增一节 | 7 |

---

## Task 1: `status` → `outcome` 两维化（原子迁移）

**为什么是一个任务而不是三个**：`status` 是一个 dataclass 字段，删掉它的那一刻所有读者同时失效。拆成「先加 outcome / 再迁读者 / 再删 status」会在中间态引入正是本设计要消掉的双字段不变量。一次做完，靠 `AttributeError` 兜底。

**Files:**
- Modify: `src/ctx_weft/protocols/hitl.py:31-78`
- Modify: `src/ctx_weft/protocols/__init__.py:8, 55-63, 146-148`
- Modify: `src/ctx_weft/core/orchestrator/hitl_manager.py:32, 104, 175, 249, 346, 354, 370, 384, 395, 412, 419, 421, 453`
- Modify: `src/ctx_weft/core/control/reducers.py:132-139`
- Modify: `src/ctx_weft/core/orchestrator/control_capability.py:729`
- Modify: `src/ctx_weft/core/runtime.py:1770`
- Test: `tests/unit/test_hitl_request_model.py`（重写）
- Test（迁移）: `test_hitl.py`、`test_hitl_multimodal_validation.py`、`test_hitl_cold_decision.py`、`test_hitl_recovery.py`、`test_hitl_ask_human_cold.py`、`test_hitl_form_extensible.py`、`test_hitl_park.py`、`test_hitl_cold_resume.py`、`test_hitl_request_parked.py`

**Interfaces:**
- Produces: `HitlOutcome = str`；常量 `HITL_OUTCOME_ACCEPTED="accepted"` / `HITL_OUTCOME_REJECTED="rejected"` / `HITL_OUTCOME_CANCELLED="cancelled"`；`HitlRequest.outcome: str` 字段；`HitlRequest.resolved -> bool`（property）；`HitlRequest.accepted -> bool`（property）；`HitlRequest.resolve(outcome: str) -> None`。
- Removes: `HitlStatus`、`HitlRequest.status`。

- [ ] **Step 1: 重写 `tests/unit/test_hitl_request_model.py` 为失败测试**

```python
"""HitlRequest 的状态两维化：outcome 存储、resolved/accepted 推导。"""

from ctx_weft.protocols.hitl import (
    HITL_OUTCOME_ACCEPTED,
    HITL_OUTCOME_CANCELLED,
    HITL_OUTCOME_REJECTED,
    HitlRequest,
)


def _req(**kw) -> HitlRequest:
    return HitlRequest(id="h1", form="approval", session_id="s", task_id="t", **kw)


def test_new_request_is_unresolved():
    req = _req()
    assert req.outcome == ""
    assert req.resolved is False
    assert req.accepted is False


def test_resolve_sets_outcome_and_derives_resolved():
    req = _req()
    req.resolve(HITL_OUTCOME_ACCEPTED)
    assert req.outcome == "accepted"
    assert req.resolved is True
    assert req.accepted is True


def test_rejected_is_resolved_but_not_accepted():
    req = _req()
    req.resolve(HITL_OUTCOME_REJECTED)
    assert req.resolved is True
    assert req.accepted is False


def test_cancelled_is_resolved_but_not_accepted():
    req = _req()
    req.resolve(HITL_OUTCOME_CANCELLED)
    assert req.resolved is True
    assert req.accepted is False


def test_host_defined_outcome_is_resolved_and_not_accepted():
    """form 开放 → outcome 必须同样开放：自定义结局照样算「已决」，但不是 accepted。

    这是本次重整的目的——旧的四值 Literal 让 host 只能用审批语汇描述自定义 form 的结局。
    """
    req = _req(form="form_fill")
    req.resolve("partially_filled")
    assert req.resolved is True
    assert req.accepted is False
    assert req.outcome == "partially_filled"


def test_resolved_is_derived_not_stored():
    """绕过 resolve() 直接写 outcome，resolved 照样为真。

    这条钉的是「resolved 不是存储字段」——存两份就有一条要维护的不变量，而漏维护的后果是
    find_resolved_for_tool_call 返回 None、把已答过的问题重新问一遍。
    """
    req = _req()
    req.outcome = HITL_OUTCOME_CANCELLED
    assert req.resolved is True


def test_hitl_status_symbol_is_gone():
    """旧词汇必须彻底消失——留只读别名会让人继续按审批语汇写代码。"""
    import ctx_weft.protocols as p
    import ctx_weft.protocols.hitl as h
    assert not hasattr(h, "HitlStatus")
    assert not hasattr(p, "HitlStatus")
    assert "status" not in HitlRequest.__dataclass_fields__
```

- [ ] **Step 2: 跑测试确认失败**

Run: `.venv/Scripts/python.exe -m pytest tests/unit/test_hitl_request_model.py -q`
Expected: FAIL —— `ImportError: cannot import name 'HITL_OUTCOME_ACCEPTED'`

- [ ] **Step 3: 改 `src/ctx_weft/protocols/hitl.py`**

把第 41 行的 `HitlStatus = Literal[...]` 换成：

```python
HitlForm = str

#: 终局结果。**开放值域**，与 `HitlForm` 对称——host 定义了自己的 form，就该能定义自己的
#: 结局。core 只认下面三个内建值，其余原样透传、不校验。
#: 空串 `""` 是「未决」哨兵，host 自定义 outcome 不得使用它。
HitlOutcome = str

HITL_OUTCOME_ACCEPTED = "accepted"
HITL_OUTCOME_REJECTED = "rejected"
HITL_OUTCOME_CANCELLED = "cancelled"
```

把 `HitlRequest` 里第 63 行的 `status: HitlStatus = "pending"` 换成：

```python
    outcome: HitlOutcome = ""     # "" = 未决；非空 = 终局（开放值域，见 HitlOutcome）
```

把第 75-77 行的 `accepted` property 换成下面三个成员（放在同一位置）：

```python
    @property
    def resolved(self) -> bool:
        """是否已有终局。**推导而非存储**——存两份就有一条要维护的不变量
        （`resolved is False` ⟺ `outcome == ""`），而漏维护是静默的：
        `HitlManager.find_resolved_for_tool_call` 会误判成「还没答」，把已答过的问题
        重新问一遍、丢掉用户已给的回复。推导掉之后这种失败不可能发生。
        """
        return bool(self.outcome)

    @property
    def accepted(self) -> bool:
        """approval 语义的便利属性。唯一消费者是 `providers/authorizer/human.py`。"""
        return self.outcome == HITL_OUTCOME_ACCEPTED

    def resolve(self, outcome: HitlOutcome) -> None:
        """终局的唯一写入点。`HitlManager._resolve` 与 `reducers.fold_cold_hitl_decision`
        都经由它，不各写各的赋值。"""
        self.outcome = outcome
```

`Literal` 若因此不再被 `hitl.py` 使用，从第 15 行的 import 里删掉。

- [ ] **Step 4: 改 `src/ctx_weft/protocols/__init__.py`**

第 8 行的模块 docstring 条目：

```python
- HitlForm / HitlOutcome / HitlRequest（HITL 请求契约，host-facing）
```

第 55-63 行的 import 块：

```python
from ctx_weft.protocols.hitl import (
    HITL_FORM_APPROVAL,
    HITL_FORM_QUESTION,
    HITL_FORM_WAIT,
    HITL_OUTCOME_ACCEPTED,
    HITL_OUTCOME_CANCELLED,
    HITL_OUTCOME_REJECTED,
    HitlForm,
    HitlOutcome,
    HitlRequest,
)
```

第 146-148 行的 `__all__`：把 `"HitlStatus"` 换成 `"HitlOutcome"`，并加入三个
`"HITL_OUTCOME_*"` 常量（放在既有 `"HITL_FORM_*"` 旁边，保持字母序）。

- [ ] **Step 5: 改 `src/ctx_weft/core/orchestrator/hitl_manager.py`**

第 32 行的 import（去掉 `HitlStatus` 及其 noqa 注释，换成 outcome 词汇）：

```python
from ctx_weft.protocols.hitl import (
    HITL_OUTCOME_ACCEPTED,
    HITL_OUTCOME_CANCELLED,
    HITL_OUTCOME_REJECTED,
    HitlForm,
    HitlOutcome,
    HitlRequest,
)
```

六处 pending 判据（**只改判据，不改逻辑**）：

| 行 | 现 | 改 |
|----|----|----|
| 104 | `if existing.status == "pending":` | `if not existing.resolved:` |
| 175 | `if req.status != "pending":` | `if req.resolved:` |
| 346 | `return mem if mem.status != "pending" else None` | `return mem if mem.resolved else None` |
| 354 | `if r.status == "pending" and (session_id is None or ...)` | `if not r.resolved and (session_id is None or ...)` |
| 419 | `if req.status != "pending":` | `if req.resolved:` |
| 453 | `resolved = [r for r in ... if r.status != "pending"]` | `resolved = [r for r in ... if r.resolved]` |

`_resolve` 的签名与写入（第 409-422 行）：

```python
    async def _resolve(
        self,
        req: HitlRequest,
        outcome: HitlOutcome,
        event_type: EventType,
        *,
        resume_on_cold: bool = False,
        message_event_jsonable: "str | list[dict] | None" = None,
    ) -> tuple[HitlRequest, bool]:
        async with self._lock:
            if req.resolved:
                return req, False                    # 已解决（含驱逐后）→ 幂等
            req.resolve(outcome)
            req.resolved_at = now_utc()
```

四个调用点的字面量换常量：

| 行 | 现 | 改 |
|----|----|----|
| 251 | `req, "cancelled", EventType.HITL_CANCELLED,` | `req, HITL_OUTCOME_CANCELLED, EventType.HITL_CANCELLED,` |
| 372 | `req, "accepted", EventType.HITL_ANSWERED,` | `req, HITL_OUTCOME_ACCEPTED, EventType.HITL_ANSWERED,` |
| 388 | `req, "accepted", evt,` | `req, HITL_OUTCOME_ACCEPTED, evt,` |
| 397 | `req, "rejected", EventType.HITL_REJECTED,` | `req, HITL_OUTCOME_REJECTED, EventType.HITL_REJECTED,` |

- [ ] **Step 6: 改 `src/ctx_weft/core/control/reducers.py:131-139`**

顶部 import 加：

```python
from ctx_weft.protocols.hitl import (
    HITL_OUTCOME_ACCEPTED,
    HITL_OUTCOME_REJECTED,
    HitlRequest,
)
```
（第 19 行原本只 import `HitlRequest`。）

四个分支：

```python
        if ev.type == EventType.HITL_ANSWERED and p.get("message"):
            req.resolve(HITL_OUTCOME_ACCEPTED)
            req.message = content_from_jsonable(p["message"])
        elif ev.type == EventType.HITL_APPROVED:
            req.resolve(HITL_OUTCOME_ACCEPTED)
            req.message = content_from_jsonable(p.get("message", ""))
        elif ev.type == EventType.HITL_MODIFIED and p.get("modified_arguments") is not None:
            req.resolve(HITL_OUTCOME_ACCEPTED)
            req.message = content_from_jsonable(p.get("message", ""))
            req.modified_arguments = p["modified_arguments"]
        elif ev.type == EventType.HITL_REJECTED:
            req.resolve(HITL_OUTCOME_REJECTED)
            req.message = content_from_jsonable(p.get("message", ""))
```

`fold_pending_hitl` **不改**——它构造的就是 `outcome=""` 的未决态。

- [ ] **Step 7: 改两处外部读者**

`src/ctx_weft/core/orchestrator/control_capability.py:729`：

```python
            if approval.outcome == HITL_OUTCOME_REJECTED:
```
并在函数内已有的惰性 import 处加上 `from ctx_weft.protocols.hitl import HITL_OUTCOME_REJECTED`
（与第 727 行的 `from ctx_weft.core.content import content_to_text` 同处）。

`src/ctx_weft/core/runtime.py:1770`：

```python
        if req.outcome == HITL_OUTCOME_REJECTED:
```
并在 `_inject_user_reply` 第 1743 行附近的 import 里加 `HITL_OUTCOME_REJECTED`。

`src/ctx_weft/providers/authorizer/human.py:47` 的 `approval.accepted` **不改**——属性保留。

- [ ] **Step 8: 迁移其余 9 个测试文件**

机械替换，但**逐个文件确认上下文**：只替换 `HitlRequest` 上的 status，**绝不碰**
`task.status` / `session.status`（同名不同物）。

- 读取型 `req.status == "pending"` → `req.resolved is False`
- 读取型 `req.status == "accepted"` → `req.outcome == "accepted"`（或 `req.accepted is True`）
- 读取型 `req.status == "rejected"` → `req.outcome == "rejected"`
- 构造参数 `HitlRequest(..., status="accepted")` → `HitlRequest(..., outcome="accepted")`

具体落点：

| 文件 | 行 |
|------|----|
| `test_hitl.py` | 60, 69, 77, 86, 105 |
| `test_hitl_multimodal_validation.py` | 149, 197, 241, 303, 436, 453, 485, 560, 578, 626 |
| `test_hitl_cold_decision.py` | 58, 100（读）、165（构造） |
| `test_hitl_recovery.py` | 24, 36 |
| `test_hitl_ask_human_cold.py` | 49, 71, 116, 134（全是构造参数；第 49 行的形参 `status: str` 一并改名 `outcome`） |
| `test_hitl_form_extensible.py` | 23, 24 |
| `test_hitl_park.py` | 207 |
| `test_hitl_cold_resume.py` | 37 |
| `test_hitl_request_parked.py` | 44 |

- [ ] **Step 8b: 补一条「事件零迁移」测试（追加到 `tests/unit/test_hitl_cold_decision.py` 末尾）**

本次重整**不改事件 payload 形状**，这条把它钉死——否则日后有人「顺手」把 outcome 写进
payload，旧日志就再也折不出决定了。

```python
def test_old_shape_payload_still_folds_into_an_outcome():
    """旧事件日志（payload 里从来只有 hitl_id / message / modified_arguments）必须原样可折。

    status → outcome 的重整只动 Python API，不动事件。这条用**手写的**旧形状 payload
    喂 fold_cold_hitl_decision，绕开任何「用新代码生成事件再读回来」的自证。
    """
    from ctx_weft.core.control.reducers import fold_cold_hitl_decision
    from ctx_weft.protocols.events import Event, EventType

    def _ev(etype, payload, seq):
        return Event(id=f"evt_{seq}", run_id=None, sequence=seq, session_id="s1",
                     type=etype, timestamp=_TS, task_id="t1", payload=payload)

    events = [
        _ev(EventType.HITL_REQUIRED,
            {"hitl_id": "h1", "form": "question", "tool_call_id": "tc9",
             "capability_id": "control:ask_user", "question": "Which DB?"}, 1),
        _ev(EventType.HITL_ANSWERED, {"hitl_id": "h1", "message": "use postgres"}, 2),
    ]
    req = fold_cold_hitl_decision(events, "tc9")

    assert req is not None
    assert req.outcome == "accepted"
    assert req.resolved is True
    assert req.message == "use postgres"
```

`_TS` 用该文件里既有的时间戳常量；若没有，就地加
`_TS = datetime(2026, 9, 1, tzinfo=UTC)` 并 import `datetime` / `UTC`。

- [ ] **Step 9: 跑全量确认绿**

Run:
```bash
find src -name __pycache__ -type d -exec rm -rf {} + 2>/dev/null
.venv/Scripts/python.exe -m pytest tests -q 2>&1 | tail -6
```
Expected: 只剩 Global Constraints 里列的三条既有失败。

- [ ] **Step 10: ruff 确认无新增告警**

Run: `.venv/Scripts/python.exe -m ruff check --output-format=concise src tests 2>&1 | grep -v "RUF00" | tail -10`
Expected: 与改动前同样的行（RUF001/2/3 中文标点是全仓基线，忽略）。

- [ ] **Step 11: 提交**

```bash
git add -A src/ctx_weft/protocols src/ctx_weft/core tests/unit
git commit -m "refactor(hitl): status 四值 Literal 换成开放值域的 outcome

status 的三个终态词是按「批准一次工具调用」造的，却要同时服务 question（accepted
= 人答了）与 wait（accepted = 人发了条消息）。form 早已放宽成开放 str，status 没跟上
——开放的是 core 不解释的那一维，封闭的是 host 最想扩展的那一维。

resolved 做成推导属性而非存储字段：存两份就有一条要维护的不变量，而漏维护是静默的
（find_resolved_for_tool_call 会误判成没答过，把问题重新问一遍）。

事件 payload、快照序列化、golden 全部不变——status 本就没进过它们任何一个。

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

## Task 2: `split_for_tool_result` 共享助手

**Files:**
- Modify: `src/ctx_weft/core/content.py:30-47`（`__all__`）、函数体加在 `downgrade_images_to_text` 之后
- Test: `tests/unit/test_split_for_tool_result.py`（新建）

**Interfaces:**
- Produces: `ctx_weft.core.content.split_for_tool_result(content: "str | list[ContentPart] | None") -> "tuple[str, list[ContentPart]]"`。任务 5、6 消费。

- [ ] **Step 1: 写失败测试 `tests/unit/test_split_for_tool_result.py`**

```python
"""provider 经 CONTENT_PARTS_KEY 回传时要把文本与非文本 part 分开，这是那个拆分器。"""

from ctx_weft.core.content import split_for_tool_result
from ctx_weft.protocols import ImagePart, TextPart


def _img(n: int = 1) -> ImagePart:
    return ImagePart(data=f"blob:{n:064x}", media_type="image/png", source_type="ref")


def test_str_passes_through_as_same_object():
    """纯文本零开销：返回的就是传进去的那个对象（Global Constraint 第一条）。"""
    s = "hello"
    text, parts = split_for_tool_result(s)
    assert text is s
    assert parts == []


def test_none_becomes_empty_text():
    assert split_for_tool_result(None) == ("", [])


def test_empty_list_becomes_empty_text():
    assert split_for_tool_result([]) == ("", [])


def test_text_parts_are_concatenated_images_collected():
    content = [TextPart(text="see "), _img(7), TextPart(text="this")]
    text, parts = split_for_tool_result(content)
    assert text == "see this"
    assert [p.data for p in parts] == [_img(7).data]


def test_image_only_content_yields_empty_text():
    text, parts = split_for_tool_result([_img(3)])
    assert text == ""
    assert len(parts) == 1


def test_part_order_is_preserved():
    a, b = _img(1), _img(2)
    _, parts = split_for_tool_result([a, TextPart(text="x"), b])
    assert [p.data for p in parts] == [a.data, b.data]
```

- [ ] **Step 2: 跑测试确认失败**

Run: `.venv/Scripts/python.exe -m pytest tests/unit/test_split_for_tool_result.py -q`
Expected: FAIL —— `ImportError: cannot import name 'split_for_tool_result'`

- [ ] **Step 3: 实现（加到 `core/content.py` 末尾，`downgrade_images_to_text` 之后）**

```python
def split_for_tool_result(
    content: "str | list[ContentPart] | None",
) -> "tuple[str, list[ContentPart]]":
    """拆成 ``(文本, 非文本 part)``，供 provider 经 ``CONTENT_PARTS_KEY`` 回传给 gateway。

    gateway 收到 ``metadata[CONTENT_PARTS_KEY]`` 后会自己拼成 ``[TextPart(text), *parts]``
    （``capability_gateway.py`` 的 ``CONTENT_PARTS_KEY`` 一节），所以 provider 必须把两者
    分开交出去，不能自己拼好。

    ``str`` 进 → 返回**同一个对象**与空列表；``None`` / 空列表 → ``("", [])``。
    纯文本路径因此零开销、逐字节不变。

    非文本判据 ``not hasattr(p, "text")`` 与 ``utils.content_to_text`` /
    ``utils.image_part_count`` 同源（spec 2026-08-20 §13 冻结），不在此另写一份。
    """
    if content is None:
        return "", []
    if isinstance(content, str):
        return content, []
    text = "".join(p.text for p in content if hasattr(p, "text"))
    parts = [p for p in content if not hasattr(p, "text")]
    return text, parts
```

在 `__all__`（第 30-47 行）里加入 `"split_for_tool_result"`。

- [ ] **Step 4: 跑测试确认通过**

Run: `.venv/Scripts/python.exe -m pytest tests/unit/test_split_for_tool_result.py -q`
Expected: 6 passed

- [ ] **Step 5: 提交**

```bash
git add src/ctx_weft/core/content.py tests/unit/test_split_for_tool_result.py
git commit -m "feat(content): 新增 split_for_tool_result——文本与非文本 part 的拆分器

provider 经 CONTENT_PARTS_KEY 回传时必须把两者分开交给 gateway（gateway 自己拼
[TextPart(text), *parts]）。ask_user 与 approval 两条出口都要用，抽一份共享的，
避免各写一遍非文本判据——那条判据是冻结的单一真源。

str 进返回同一对象，纯文本路径零开销。

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

## Task 3: 冷恢复的 event → memory ref 转换

**必须先于任务 5。** 今天 ask_user 把图展平的 bug 正遮蔽着这颗雷；先让 ask_user 透传 parts 会立刻把 memory 永远打不开的 event ref 写进工具结果、再随 `TOOL_RESULT` 落进 memory。

**Files:**
- Modify: `src/ctx_weft/core/runtime.py:1925-1937`
- Test: `tests/unit/test_hitl_cold_multimodal.py`（新建）

**Interfaces:**
- Consumes: 无（`fold_cold_hitl_decision` 保持纯函数不变）。
- Produces: `CtxWeftRuntime._cold_hitl_decision` 返回的 `HitlRequest.message` 里的 `ImagePart` 一律是 **memory 侧 ref**；取不回时是 `[image {media_type}]` 文本占位。任务 5 依赖此性质。

- [ ] **Step 1: 写失败测试 `tests/unit/test_hitl_cold_multimodal.py`**

```python
"""冷 HITL 决定的 event → memory ref 转换。

`fold_cold_hitl_decision` 从事件日志还原 `req.message`，而事件 payload 里存的是
**event 侧的 ref**（EventBlobStore 的命名空间）。这个 req 会被 ask_user / approval
消费并最终进 memory——直接透传就是往 memory 里写一个永远打不开的引用。
"""

import pytest

from ctx_weft.protocols import ImagePart, ProviderContext, TextPart


class _EventBlobs:
    """event 侧 store：按 event ref 存字节。"""
    can_externalize = True

    def __init__(self, data=None):
        self._data = data or {}

    async def put(self, data, media_type, ctx):
        raise NotImplementedError                       # 本测试不写入 event 侧

    async def get(self, ref, ctx):
        raw = self._data.get(ref)
        return (raw, "image/png") if raw is not None else None


class _MemoryBlobs:
    """memory 侧 store：put 产出自己命名空间里的 ref。"""
    can_externalize = True

    def __init__(self):
        self.puts = []

    async def put(self, data, media_type, ctx):
        self.puts.append(data)
        return "blob:" + "m" * 64

    async def get(self, ref, ctx):
        return None


EVENT_REF = "blob:" + "e" * 64
MEMORY_REF = "blob:" + "m" * 64
PNG = b"\x89PNG\r\n\x1a\n" + b"payload"


def _msg_with_event_ref():
    return [TextPart(text="看这个"),
            ImagePart(data=EVENT_REF, media_type="image/png", source_type="ref")]


@pytest.mark.asyncio
async def test_cold_decision_converts_event_ref_to_memory_ref(cold_runtime):
    """🔴 本任务存在的理由：还原出来的 event ref 必须被换成 memory ref。"""
    rt, _ = cold_runtime(event_blobs=_EventBlobs({EVENT_REF: PNG}),
                         folded_message=_msg_with_event_ref())
    req = await rt._cold_hitl_decision("s1", "tc1")

    images = [p for p in req.message if not hasattr(p, "text")]
    assert len(images) == 1
    assert images[0].data == MEMORY_REF, "必须是 memory 侧 ref，不是 event 侧的"
    assert images[0].source_type == "ref"


@pytest.mark.asyncio
async def test_cold_decision_degrades_loudly_when_event_blob_is_gone(cold_runtime, caplog):
    """event blob 取不回 → 降级成确定性文本占位，并记 error；绝不让解不开的 ref 流下去。"""
    rt, _ = cold_runtime(event_blobs=_EventBlobs({}),      # 字节没了
                         folded_message=_msg_with_event_ref())
    req = await rt._cold_hitl_decision("s1", "tc1")

    assert all(hasattr(p, "text") for p in req.message), "不得留下任何 ImagePart"
    assert "[image image/png]" in "".join(p.text for p in req.message)


@pytest.mark.asyncio
async def test_plain_text_decision_is_untouched(cold_runtime):
    """纯文本零成本直通：返回的就是折叠出来的那个对象（Global Constraint 第一条）。"""
    rt, _ = cold_runtime(event_blobs=_EventBlobs({}), folded_message="use postgres")
    req = await rt._cold_hitl_decision("s1", "tc1")
    assert req.message == "use postgres"


@pytest.mark.asyncio
async def test_no_decision_returns_none(cold_runtime):
    """没有可用决定时照旧返回 None，转换逻辑不得把它变成别的东西。"""
    rt, _ = cold_runtime(event_blobs=_EventBlobs({}), folded_message=None)
    assert await rt._cold_hitl_decision("s1", "tc1") is None
```

同文件顶部加 fixture（放在 import 之后、测试之前）：

```python
@pytest.fixture
def cold_runtime(monkeypatch):
    """造一个只够跑 _cold_hitl_decision 的壳：桩掉事件读取与 fold，注入两个 blob store。"""
    from types import SimpleNamespace

    from ctx_weft.core.runtime import CtxWeftRuntime
    from ctx_weft.protocols.hitl import HITL_OUTCOME_ACCEPTED, HitlRequest

    def _make(*, event_blobs, folded_message):
        rt = object.__new__(CtxWeftRuntime)          # 不跑 __init__（要一整套 provider）
        memory_blobs = _MemoryBlobs()
        rt.providers = SimpleNamespace(
            get_event_blob_store=lambda: event_blobs,
            get_memory_blob_store=lambda: memory_blobs,
        )
        rt.event_store = SimpleNamespace(
            read_session_events_of_types=lambda sid, types: _aio([]),
        )

        def _fold(events, tool_call_id):
            if folded_message is None:
                return None
            req = HitlRequest(id="h1", form="question", session_id="s1", task_id="t1",
                              tool_call_id=tool_call_id)
            req.resolve(HITL_OUTCOME_ACCEPTED)
            req.message = folded_message
            return req

        monkeypatch.setattr("ctx_weft.core.control.reducers.fold_cold_hitl_decision", _fold)
        monkeypatch.setattr(CtxWeftRuntime, "_tenant_for_session",
                            lambda self, sid: _aio("tn"))
        return rt, memory_blobs

    return _make


async def _aio(value):
    return value
```

- [ ] **Step 2: 跑测试确认失败**

Run: `.venv/Scripts/python.exe -m pytest tests/unit/test_hitl_cold_multimodal.py -q`
Expected: FAIL —— 第一条断言 `images[0].data == MEMORY_REF` 失败，实际是 `EVENT_REF`

- [ ] **Step 3: 改 `src/ctx_weft/core/runtime.py:1925-1937`**

把 `_cold_hitl_decision` 的函数体末尾（`return fold_cold_hitl_decision(events, tool_call_id)`）
换成：

```python
        req = fold_cold_hitl_decision(events, tool_call_id)
        # event 侧 ref → memory 侧 ref。事件 payload 里存的是 EventBlobStore 命名空间的
        # ref，而这个 req 的 message 会被 ask_user / approval 消费、最终随 TOOL_RESULT 进
        # memory——直接透传就是往 memory 里写一个永远打不开的引用（两个 ref 命名空间独立，
        # spec 2026-08-27 dual-blob-store）。纯文本 message 零成本直通。
        if req is None or not req.message or isinstance(req.message, str):
            return req
        from ctx_weft.core.content import (
            downgrade_images_to_text, hydrate_event_content, normalize_content,
        )
        ctx = ProviderContext(
            session_id=session_id, tenant_id=await self._tenant_for_session(session_id))
        try:
            hydrated = await hydrate_event_content(
                req.message, event_blob_store=self.providers.get_event_blob_store(), ctx=ctx)
            blob_store = self.providers.get_memory_blob_store()
            req.message = (await normalize_content(hydrated, blob_store=blob_store, ctx=ctx)
                           if blob_store.can_externalize else hydrated)
        except Exception:
            # 与 _restore_task_prompts 同一姿态：绝不让解不开的 ref 流下去，降级成确定性
            # 占位并响亮记账。冷恢复是最不能再崩一次的地方，也是最不能静默的地方。
            logger.error(
                "_cold_hitl_decision: event ref 转换失败，降级为文本占位 (tool_call=%s)",
                tool_call_id, exc_info=True)
            req.message = downgrade_images_to_text(req.message)
        return req
```

注意 `hydrate_event_content` 取不回字节时**不抛**、而是产出 `[image unavailable: …]`
文本占位（`content.py` 的 `_IMAGE_UNAVAILABLE_TMPL`）。第二条测试断言的是
`[image image/png]`——若实际产出 `[image unavailable: image/png]`，把测试断言改成
匹配实际实现，**不要**为了迁就测试去改 `hydrate_event_content` 的语义；两种占位都满足
「不留 ImagePart」这条真正的要求。

- [ ] **Step 4: 跑测试确认通过**

Run: `.venv/Scripts/python.exe -m pytest tests/unit/test_hitl_cold_multimodal.py -q`
Expected: 4 passed

- [ ] **Step 5: 变异验证**

把 Step 3 加的那段整体注释掉、恢复成 `return fold_cold_hitl_decision(events, tool_call_id)`，
重跑：前两条必须红。确认后改回。

- [ ] **Step 6: 跑既有冷路径测试确认没回归**

Run: `.venv/Scripts/python.exe -m pytest tests/unit/test_hitl_cold_decision.py tests/unit/test_hitl_cold_resume.py tests/unit/test_hitl_ask_human_cold.py tests/integration/test_hitl_cold_input.py -q`
Expected: all passed

- [ ] **Step 7: 提交**

```bash
git add src/ctx_weft/core/runtime.py tests/unit/test_hitl_cold_multimodal.py
git commit -m "fix(hitl): 冷决定的 event ref 转成 memory ref，别把打不开的引用喂进 memory

fold_cold_hitl_decision 从事件日志还原 req.message，而事件 payload 里存的是 event
侧 ref。这个 req 交给 ask_user / approval 后最终会随 TOOL_RESULT 进 memory——两个
ref 命名空间独立，透传等于往 memory 里写一个永远打不开的引用。

今天不出事，只因为那两条出口都把图 content_to_text 展平了；下一个任务要把图接通，
所以这一条必须先落。转换放在 runtime 的边界 handler 上，reducers 保持纯函数不碰 IO。

取不回字节时降级成文本占位并 logger.error——姿态与 _restore_task_prompts 一致。

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

## Task 4: 启用 `defer` + `wait_for_decision`，删 `Authorizer.filter`

**Files:**
- Modify: `src/ctx_weft/core/orchestrator/hitl_manager.py:160-179`
- Modify: `src/ctx_weft/providers/authorizer/human.py:29-54`
- Modify: `src/ctx_weft/protocols/capability.py:274-285`（删 `filter`）
- Modify: `docs/spec/05-authz-and-hitl.md:26, 206`
- Test: `tests/unit/test_hitl_park.py`（增补）

**Interfaces:**
- Produces: `HitlManager.wait_for_decision(hitl_id: str) -> HitlRequest | None`（驱逐返回 `None`，不抛）。`HitlManager.wait(hitl_id) -> HitlRequest` 语义不变（`None` → `raise HitlPark`）。
- Removes: `Authorizer.filter`。

- [ ] **Step 1: 写失败测试（追加到 `tests/unit/test_hitl_park.py` 末尾）**

```python
# ── defer：内置 authorizer 走协议的挂起语义，不再让 BaseException 穿过 gateway ──────


@pytest.mark.asyncio
async def test_wait_for_decision_returns_none_on_eviction():
    """热→冷驱逐时返回 None 而不是抛——authorizer 由此不必 import core 的 HitlPark。"""
    from ctx_weft.core.orchestrator.hitl_manager import HitlManager

    hm = HitlManager(timeout_sec=0)
    hid = await hm.request(form="approval", session_id="s", task_id="t")
    assert await hm.wait_for_decision(hid) is None


@pytest.mark.asyncio
async def test_wait_still_raises_park_for_ask_user():
    """wait() 语义不变：ask_user 在 provider 里，够不着 defer，必须靠异常 unwind。"""
    from ctx_weft.core.loop.park import HitlPark
    from ctx_weft.core.orchestrator.hitl_manager import HitlManager

    hm = HitlManager(timeout_sec=0)
    hid = await hm.request(form="question", session_id="s", task_id="t")
    with pytest.raises(HitlPark):
        await hm.wait(hid)


@pytest.mark.asyncio
async def test_human_authorizer_defers_instead_of_raising():
    """🔴 本任务存在的理由：驱逐后 authorize() 返回 defer 决定，不抛异常。"""
    from ctx_weft.core.orchestrator.hitl_manager import HitlManager
    from ctx_weft.protocols import ProviderContext, ToolCapability
    from ctx_weft.providers.authorizer.human import HumanConfirmationAuthorizer

    hm = HitlManager(timeout_sec=0)
    az = HumanConfirmationAuthorizer(hitl_manager=hm)
    cap = ToolCapability(id="bash:run", name="run", kind="tool")
    decision = await az.authorize(cap, ProviderContext(session_id="s", tenant_id="tn"),
                                 {}, tool_call_id="tc1")
    assert decision.defer is True
    assert decision.allowed is False


def test_authorizer_filter_is_gone():
    """filter 零调用点，且对 HumanConfirmation 会真的发一个 HITL 请求并等人——是陷阱。"""
    from ctx_weft.protocols.capability import Authorizer
    assert not hasattr(Authorizer, "filter")
```

- [ ] **Step 2: 跑测试确认失败**

Run: `.venv/Scripts/python.exe -m pytest tests/unit/test_hitl_park.py -q -k "defer or wait_for_decision or filter_is_gone or still_raises"`
Expected: FAIL —— `AttributeError: 'HitlManager' object has no attribute 'wait_for_decision'`

- [ ] **Step 3: 改 `hitl_manager.py:160-179`，把 `wait` 拆成两层**

```python
    async def wait_for_decision(self, hitl_id: str) -> HitlRequest | None:
        """阻塞至应答；**热→冷驱逐返回 ``None``，不抛**。timeout_sec=None（默认）永不超时。

        给 `Authorizer` 实现方用：授权是 protocols 层的 host 扩展点，不该要求实现方去
        catch 一个 core 内部的 `BaseException`——它们该返回
        `AuthorizationDecision(defer=True)`，由 gateway 决定怎么挂起（spec/07 §7
        「同一套基础设施……合并实现」）。

        `ask_user` 那类 `ToolCapabilityProvider` 够不着 `defer` 那个接缝，走 `wait()`。
        未知 id 抛 KeyError。
        """
        future = self._futures.get(hitl_id)
        if future is None:
            raise KeyError(f"No HITL request found: {hitl_id}")
        try:
            async with asyncio.timeout(self._timeout_sec):
                return await future
        except TimeoutError:
            async with self._lock:
                req = self._requests[hitl_id]
                if req.resolved:
                    return req                       # answer 先到：走热已解决
                self._futures.pop(hitl_id, None)     # 驱逐 future，保留 pending
            return None

    async def wait(self, hitl_id: str) -> HitlRequest:
        """`wait_for_decision` 之上的薄层：驱逐 → `raise HitlPark`（协程栈 unwind 到
        SUSPENDED）。控制工具（ask_user / wait_for_user）走这条。"""
        decision = await self.wait_for_decision(hitl_id)
        if decision is not None:
            return decision
        from ctx_weft.core.loop.park import HitlPark
        raise HitlPark(hitl_id=hitl_id,
                       tool_call_id=self._requests[hitl_id].tool_call_id)
```

- [ ] **Step 4: 改 `providers/authorizer/human.py:46`**

```python
            approval = await self.hitl_manager.wait_for_decision(hitl_id)
            if approval is None:
                # 热→冷驱逐：不放行也不拒绝。gateway 见 defer 即「绝不调 provider.invoke
                # + 挂起」（capability_gateway.py 的 defer 分支）。走协议的挂起语义而不是
                # 让 core 的 HitlPark 穿过 authorize()——Authorizer 是 host 扩展点，
                # 内置实现该做契约的范例。
                return AuthorizationDecision(allowed=False, defer=True)
```

同时把类 docstring 里「可在 approve 时携带 modified_arguments」那句后面补一行：

```python
    热窗口被驱逐（超时）时返回 ``defer=True`` 的决定，由 gateway 挂起本次调用。
```

- [ ] **Step 5: 删 `protocols/capability.py:274-285` 的 `filter`**

整个 `async def filter(...)` 方法删除，并把 `Authorizer` 类 docstring 里
「``filter`` 是基于 ``authorize`` 的批量便捷默认（可见性过滤），保留给装配期/外部用。」
换成：

```
    曾有一个基于 ``authorize`` 的批量 ``filter`` 默认实现，**已删除**：它零调用点，且对
    ``HumanConfirmationAuthorizer`` 会**真的发出一个 HITL 请求并等人**——把「列一下有哪些
    工具可见」变成「向人类逐个求批」。真需要装配期可见性过滤时应另行设计，届时必须显式
    排除会挂起的 authorizer。
```

- [ ] **Step 6: 同步 `docs/spec/05-authz-and-hitl.md`**

- 第 26 行（`filter` 那句）整行删除。
- 第 206 行清单项里的「；`filter` 为基于它的默认」删掉。
- 第 44-46 行「HumanConfirmation 规则（热路径）」末句改为：
  `热窗口超时 → authorizer 返回 defer=True 决定，gateway 挂起本次调用（不是失败，见下方热/冷模型）。`

- [ ] **Step 7: 跑测试确认通过**

Run: `.venv/Scripts/python.exe -m pytest tests/unit/test_hitl_park.py tests/unit/test_hitl.py tests/unit/test_authorizer.py -q`
Expected: all passed

- [ ] **Step 8: 变异验证**

把 human.py 的 `if approval is None: return ...defer...` 改回 `approval = await self.hitl_manager.wait(hitl_id)`，
`test_human_authorizer_defers_instead_of_raising` 必须红（变成抛 `HitlPark`）。确认后改回。

- [ ] **Step 9: 提交**

```bash
git add src/ctx_weft/core/orchestrator/hitl_manager.py src/ctx_weft/providers/authorizer/human.py src/ctx_weft/protocols/capability.py docs/spec/05-authz-and-hitl.md tests/unit/test_hitl_park.py
git commit -m "refactor(authz): 内置 authorizer 走 defer，不再让 HitlPark 穿过 authorize()

Authorizer 是 protocols 层的 host 扩展点，而挂起的唯一办法此前是抛 core 内部的
HitlPark（BaseException）。spec/05 §1 与 spec/07 §7 早就把 defer 写成了协议自己的
挂起语义（两条入口合并实现：defer 给新的 authorize() 当场判定，HitlPark 给已经悬在
await 上的活协程 unwind）。让内置实现成为契约的范例而非反例。

新增 HitlManager.wait_for_decision（驱逐返回 None 不抛）；wait() 变成它之上的薄层，
语义不变——ask_user 在 provider 里够不着 defer，仍须靠异常 unwind。

顺带删掉 Authorizer.filter：零调用点，且对 HumanConfirmationAuthorizer 会真的发一个
HITL 请求并等人，把「列出可见工具」变成「逐个求批」。

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

## Task 5: ask_user 出口接图

**Files:**
- Modify: `src/ctx_weft/core/orchestrator/control_capability.py:724-734`
- Test: `tests/unit/test_hitl_ask_user_multimodal.py`（新建）

**Interfaces:**
- Consumes: `split_for_tool_result`（任务 2）；`_cold_hitl_decision` 的 memory-ref 保证（任务 3）。

- [ ] **Step 1: 写失败测试 `tests/unit/test_hitl_ask_user_multimodal.py`**

**必须走真实代码路径**（`provider.invoke` → HITL → 出口），不要在测试里复刻一份出口逻辑
——那样即使 `control_capability.py` 一个字不改测试也会绿，Step 5 的变异验证抓不到。
下面的 harness 照搬 `tests/unit/test_hitl.py:215-232` 与 `:161-167` 的既有写法，
只把「只收 content 字符串」改成「收整个 result payload」。

```python
"""ask_user（form=question）的答复带图时，图必须到达模型。

此前 control_capability 用 content_to_text 展平 approval.message、并把 metadata 硬写成
{}——非文本 part 被静默丢弃（content_to_text 连占位都不留）。而 CONTENT_PARTS_KEY 通道
就在旁边，media:get_image 走的正是它。

本文件全程走真实路径：provider.invoke → HitlManager → 出口 payload。
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from ctx_weft.core.loop.capability_gateway import CONTENT_PARTS_KEY
from ctx_weft.core.orchestrator.control_capability import (
    PROVIDER_NAME,
    ControlCapabilityProvider,
)
from ctx_weft.core.orchestrator.hitl_manager import HitlManager
from ctx_weft.core.state.models import Session, Task
from ctx_weft.protocols import ImagePart, ProviderContext, TextPart

REF = "blob:" + "a" * 64


def _img() -> ImagePart:
    return ImagePart(data=REF, media_type="image/png", source_type="ref")


def _control_provider(mgr: HitlManager):
    provider = ControlCapabilityProvider(hitl_manager=mgr)
    session = Session(id="s1", tenant_id="default", user_prompt="do it", status="RUNNING")
    task = Task(id="tsk_1", session_id="s1", status="ACTIVE", title="T1")
    tm = SimpleNamespace(get_task=lambda tid: task, reopen_chain=None)
    provider.register_session("s1", tm, session)
    return provider


async def _await_pending(mgr: HitlManager):
    for _ in range(200):
        pend = mgr.list_pending()
        if pend:
            return pend[0]
        await asyncio.sleep(0)
    raise AssertionError("no pending HITL request appeared")


async def _ask_and_respond(mgr, provider, respond) -> dict:
    """跑一次真实的 ask_user，用 respond(hitl_id) 应答，返回收到的 result payload。"""
    ctx = ProviderContext(session_id="s1", tenant_id="default",
                          task_id="tsk_1", agent_id="agt_1")
    payloads: list[dict] = []

    async def drain():
        async for ev in provider.invoke(
            f"{PROVIDER_NAME}:ask_user", {"questions": [{"question": "Which DB?"}]}, ctx
        ):
            if ev.kind == "result":
                payloads.append(ev.payload)

    handle = asyncio.create_task(drain())
    req = await _await_pending(mgr)
    await respond(req.id)
    await handle
    assert payloads, "ask_user 没有产出 result 事件"
    return payloads[0]


@pytest.mark.asyncio
async def test_accepted_answer_with_image_carries_the_part():
    """🔴 本任务存在的理由：图经 CONTENT_PARTS_KEY 交给 gateway，不再被展平掉。"""
    mgr = HitlManager()
    provider = _control_provider(mgr)
    payload = await _ask_and_respond(
        mgr, provider,
        lambda hid: mgr.answer(hid, [TextPart(text="就是这个"), _img()]),
    )
    assert payload["content"] == "就是这个"
    assert [p.data for p in payload["metadata"][CONTENT_PARTS_KEY]] == [REF]


@pytest.mark.asyncio
async def test_rejected_answer_with_image_keeps_the_part_and_the_prefix():
    """拒绝路径的前缀必须并进首个 TextPart，不能用 f-string 把 parts 拍成 repr。"""
    mgr = HitlManager()
    provider = _control_provider(mgr)
    payload = await _ask_and_respond(
        mgr, provider,
        lambda hid: mgr.reject(hid, message=[TextPart(text="不行"), _img()]),
    )
    assert payload["content"] == "Human declined: 不行"
    assert [p.data for p in payload["metadata"][CONTENT_PARTS_KEY]] == [REF]


@pytest.mark.asyncio
async def test_plain_text_answer_is_byte_identical_and_has_no_parts_key():
    """纯文本路径逐字节不变（Global Constraint 第一条）。"""
    mgr = HitlManager()
    provider = _control_provider(mgr)
    payload = await _ask_and_respond(mgr, provider,
                                     lambda hid: mgr.answer(hid, "use postgres"))
    assert payload["content"] == "use postgres"
    assert CONTENT_PARTS_KEY not in payload["metadata"]


@pytest.mark.asyncio
async def test_rejected_without_message_uses_default_sentence():
    mgr = HitlManager()
    provider = _control_provider(mgr)
    payload = await _ask_and_respond(mgr, provider, lambda hid: mgr.reject(hid))
    assert payload["content"] == "Human rejected the request."
    assert CONTENT_PARTS_KEY not in payload["metadata"]
```

> 注：裸 `HitlManager()` 未注入 content normalizer，`_normalize_message` 会对携图内容
> 走 `content_to_event_jsonable(NullEventBlobStore())`。上面用的是 `source_type="ref"`
> 的 `ImagePart`——那一支**不抛**，只降级 event 侧载荷并记 warning（见任务 7 Step 3 订正的
> docstring），`req.message` 仍原样保留 parts。**不要**改用 inline base64，那会撞
> `NotImplementedError`。

- [ ] **Step 2: 跑测试确认失败**

Run: `.venv/Scripts/python.exe -m pytest tests/unit/test_hitl_ask_user_multimodal.py -q`
Expected: FAIL —— 前两条报 `KeyError: 'content_parts'`（出口仍在 `content_to_text` 展平、
`metadata` 恒为 `{}`）；后两条应已通过（纯文本路径本来就对）。

- [ ] **Step 3: 改 `control_capability.py:724-734`**

```python
            _, session = self._sessions.get(ctx.session_id, (None, None))
            if session is not None:
                session.status = "RUNNING"
            # 出口：文本走工具文本，图片 part 走 CONTENT_PARTS_KEY 交给 gateway
            # （Phase 4 Task 3 建的通用接缝，media:get_image 走的也是它）。
            # 此前这里是 content_to_text + metadata={}，人贴的图被静默丢弃。
            from ctx_weft.core.content import content_with_prefix, split_for_tool_result
            from ctx_weft.core.loop.capability_gateway import CONTENT_PARTS_KEY
            from ctx_weft.protocols.hitl import HITL_OUTCOME_REJECTED
            msg = approval.message
            if approval.outcome == HITL_OUTCOME_REJECTED:
                content = (content_with_prefix(msg, "Human declined: ") if msg
                           else "Human rejected the request.")
            else:
                content = msg or result.content
            text, parts = split_for_tool_result(content)
            payload: dict[str, Any] = {"content": text, "metadata": {}}
            if parts:
                payload["metadata"] = {CONTENT_PARTS_KEY: parts}
            yield CapabilityEvent(kind="result", payload=payload)
            return
```

`capability_gateway` 的 import 必须是**惰性**的（放在函数体内，如上）——
`core.orchestrator` 对 `core.loop` 的模块级引用会成环，先例见
`core/media/capability.py:418`。

- [ ] **Step 4: 跑测试确认通过**

Run: `.venv/Scripts/python.exe -m pytest tests/unit/test_hitl_ask_user_multimodal.py -q`
Expected: 5 passed

- [ ] **Step 5: 变异验证**

把 Step 3 的出口改回 `msg = content_to_text(approval.message)` + `payload["metadata"] = {}`，
前两条测试必须红（`KeyError: 'content_parts'`），后两条仍绿。确认后改回。
因为测试走的是真实的 `provider.invoke`，这次变异是真的能被抓到的。

- [ ] **Step 6: 跑 ask_user 既有测试确认没回归**

Run: `.venv/Scripts/python.exe -m pytest tests/unit/test_hitl_ask_human_cold.py tests/unit/test_hitl_reconcile.py tests/integration/test_interactive_task.py -q`
Expected: all passed

- [ ] **Step 7: 提交**

```bash
git add src/ctx_weft/core/orchestrator/control_capability.py tests/unit/test_hitl_ask_user_multimodal.py
git commit -m "fix(hitl): ask_user 的答复带图时不再静默丢图

三个 form 里只有 wait 把图送到了模型面前。question 恰恰是「向人要信息」的那个，
却在最后一步被 content_to_text 展平——那个函数的实现是 if hasattr(item,'text') 才收，
非文本 part 直接跳过，连占位都不留。metadata 还硬写成 {}，而 CONTENT_PARTS_KEY 通道
就在旁边，media:get_image 走的正是它。

拒绝分支的前缀改走 content_with_prefix 而非 f-string：对 str 逐字节原样，对 parts
并进首个 TextPart，图不丢。

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

## Task 6: approval 备注接图

**Files:**
- Modify: `src/ctx_weft/protocols/capability.py:242-249`（`message` 类型）
- Modify: `src/ctx_weft/providers/authorizer/human.py:47-54`
- Modify: `src/ctx_weft/core/loop/capability_gateway.py:189-197, 254-267`
- Test: `tests/unit/test_approval_note_multimodal.py`（新建）

**Interfaces:**
- Consumes: `split_for_tool_result`（任务 2）。
- Produces: `AuthorizationDecision.message: "str | list[ContentPart]"`。

- [ ] **Step 1: 写失败测试 `tests/unit/test_approval_note_multimodal.py`**

**走真实 gateway**（`CapabilityGateway.invoke` → authorizer → 出口 content），
harness 照搬 `tests/unit/test_tool_result_content_parts.py:86-127` 的 `_state_ctx`/`_gw`/`_run`。
不要只测 `content_with_prefix` ——那是在测归一层，不是在测本任务改的接线。

```python
"""approval 的人工备注带图时，图必须随工具结果到达模型。

AuthorizationDecision.message 此前是 str，human.py 用 content_to_text 展平；gateway 两处
拼接是 f-string，遇到 parts 会拍成 repr。改走 content_with_prefix / content_with_suffix
——对 str 逐字节原样，对 parts 并进首/末个 TextPart。

本文件全程走真实 gateway 路径。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from types import SimpleNamespace

import pytest

from ctx_weft.core.loop.capability_gateway import CONTENT_PARTS_KEY, CapabilityGateway
from ctx_weft.core.loop.driver import LoopContext, LoopState
from ctx_weft.core.orchestrator.capability_cache import CapabilityCache
from ctx_weft.protocols import ImagePart, MemoryAddress, ProviderContext, TextPart
from ctx_weft.protocols.capability import (
    AuthorizationDecision,
    Authorizer,
    CapabilityEvent,
    CapabilityProviderInfo,
    ToolCapability,
    ToolCapabilityProvider,
)
from ctx_weft.providers.events import InProcessEventBus
from ctx_weft.providers.memory.in_memory import InMemoryMemoryProvider

NOTE_REF = "blob:" + "b" * 64
TOOL_REF = "blob:" + "c" * 64


def _img(ref: str) -> ImagePart:
    return ImagePart(data=ref, media_type="image/png", source_type="ref")


class _Prov(ToolCapabilityProvider):
    name = "mcp:t"

    def __init__(self, text: str, metadata: dict | None = None) -> None:
        self._text, self._metadata = text, metadata

    def _cap(self) -> ToolCapability:
        return ToolCapability(id="mcp:t:go", name="go", description="d")

    async def list(self, ctx): return [self._cap()]
    async def retrieve(self, ctx): return [self._cap()]
    async def describe(self, ctx): return CapabilityProviderInfo(name=self.name, capability_count=1)
    async def cancel(self, invocation_id, ctx) -> None: return None

    def invoke(self, capability_id, arguments, ctx) -> AsyncIterator[CapabilityEvent]:
        async def _run():
            payload: dict = {"content": self._text}
            if self._metadata is not None:
                payload["metadata"] = self._metadata
            yield CapabilityEvent(kind="result", payload=payload)
        return _run()


class _Az(Authorizer):
    """按构造参数返回放行/拒绝 + 任意形态的 message。"""

    def __init__(self, message, *, allowed: bool = True) -> None:
        self._message, self._allowed = message, allowed

    async def authorize(self, capability, ctx, arguments=None, *, tool_call_id=""):
        return AuthorizationDecision(allowed=self._allowed, message=self._message)


async def _run(provider, authorizer):
    mem = InMemoryMemoryProvider()
    scope = MemoryAddress(session_id="s1", task_id="tsk_1", agent_id="agt_1")
    state = LoopState(
        run_id="r1", session=SimpleNamespace(id="s1", tenant_id="default"),
        task=SimpleNamespace(id="tsk_1"), agent=SimpleNamespace(id="agt_1", template_id="t"),
        scope=scope,
    )
    ctx = LoopContext(
        assembler=None, llm=None, memory=mem, event_bus=InProcessEventBus(),
        provider_ctx=ProviderContext(session_id="s1", tenant_id="default",
                                     task_id="tsk_1", agent_id="agt_1"),
    )
    cache = CapabilityCache()
    cache.put("agt_1", [provider._cap()])
    gw = CapabilityGateway(
        capability_cache=cache, capability_providers=[provider],
        memory=mem, event_bus=InProcessEventBus(), default_authorizer=authorizer,
    )
    return await gw.invoke("mcp__t__go", {}, state, ctx), mem


@pytest.mark.asyncio
async def test_human_note_with_image_reaches_the_model():
    """🔴 本任务存在的理由：审批备注里的图必须随工具结果送到模型面前。"""
    res, _ = await _run(_Prov("tool output"),
                        _Az([TextPart(text="看这个"), _img(NOTE_REF)]))
    assert isinstance(res.content, list)
    assert res.content[0].text == "[Human note: 看这个]\ntool output"
    assert [p.data for p in res.content if not hasattr(p, "text")] == [NOTE_REF]


@pytest.mark.asyncio
async def test_note_parts_precede_tool_result_parts():
    """顺序裁定：备注图在工具图之前——与文本顺序一致（[Human note: …] 也在前）。"""
    res, _ = await _run(_Prov("out", {CONTENT_PARTS_KEY: [_img(TOOL_REF)]}),
                        _Az([TextPart(text="注意"), _img(NOTE_REF)]))
    assert [p.data for p in res.content if not hasattr(p, "text")] == [NOTE_REF, TOOL_REF]


@pytest.mark.asyncio
async def test_plain_text_note_keeps_content_a_str():
    """备注是纯 str 且工具无 parts → content 仍是 str，逐字节不变（Global Constraint 一）。"""
    res, _ = await _run(_Prov("tool output"), _Az("be careful"))
    assert isinstance(res.content, str)
    assert res.content == "[Human note: be careful]\ntool output"


@pytest.mark.asyncio
async def test_blocked_with_image_keeps_the_part_and_both_affixes():
    """拒绝路径：[Blocked by human: …] 的前后缀都并进文本 part，图不丢。"""
    res, mem = await _run(_Prov("never runs"),
                          _Az([TextPart(text="不许跑"), _img(NOTE_REF)], allowed=False))
    assert res.is_error is True
    assert isinstance(res.content, list)
    assert res.content[0].text == "[Blocked by human: 不许跑]"
    assert [p.data for p in res.content if not hasattr(p, "text")] == [NOTE_REF]


@pytest.mark.asyncio
async def test_blocked_plain_text_is_byte_identical():
    """纯文本拒绝路径必须与改造前的 f-string 逐字节相同。"""
    res, _ = await _run(_Prov("never runs"), _Az("no", allowed=False))
    assert res.content == "[Blocked by human: no]"
```

- [ ] **Step 2: 跑测试确认失败**

Run: `.venv/Scripts/python.exe -m pytest tests/unit/test_approval_note_multimodal.py -q`
Expected: FAIL —— 带图的三条红（f-string 把 `list[ContentPart]` 拍成了 repr，
`res.content[0].text` 里会出现 `[TextPart(text='看这个'), ImagePart(...)]` 这样的字面量）；
两条纯文本对照应已通过。

- [ ] **Step 3: 改 `protocols/capability.py:242-249`**

```python
@dataclass
class AuthorizationDecision:
    """一次授权的结构化结果。"""

    allowed: bool
    # 反馈 / 拒绝指导，回灌给 LLM（allow / deny 都可带）。
    # **可以是 `list[ContentPart]`**：人类经 HITL 递进来的备注可能带图
    # （`HumanConfirmationAuthorizer` 直接透传 `HitlRequest.message`）。gateway 的两处
    # 拼接走 `content_with_prefix` / `content_with_suffix`，对 str 逐字节原样。
    message: "str | list[ContentPart]" = ""
    modified_arguments: dict[str, Any] | None = None  # allow 时的有效参数（None = 用原参）
    defer: bool = False                            # spec/07 §7：挂起本次调用（gateway 绝不 invoke）
```

文件顶部 `if TYPE_CHECKING:` 块里加 `from ctx_weft.protocols.context import ContentPart`。

- [ ] **Step 4: 改 `providers/authorizer/human.py:47-54`**

```python
        if approval.accepted:
            return AuthorizationDecision(
                allowed=True,
                message=approval.message,          # 透传，不再 content_to_text 展平
                modified_arguments=approval.modified_arguments,
            )
        logger.info("HITL blocked '%s' (outcome=%s)", capability.id, approval.outcome)
        return AuthorizationDecision(allowed=False, message=approval.message)
```

顶部 `from ctx_weft.core.utils import content_to_text` 若不再被使用则删除。

- [ ] **Step 5: 改 `capability_gateway.py` 两处拼接**

第 189-197 行（拒绝）：

```python
        if not decision.allowed:
            logger.warning("Capability '%s' blocked by authorizer for agent %s", cap.id, state.agent.id)
            # 走 content_with_prefix/suffix 而非 f-string：备注可能是 list[ContentPart]
            # （人类审批时贴的图），f-string 会把它拍成 repr。对 str 逐字节原样。
            content = (
                content_with_suffix(
                    content_with_prefix(decision.message, "[Blocked by human: "), "]")
                if decision.message
                else f"[Error: capability '{tool_name}' not authorized]"
            )
            return await self._error_and_record(
                state, ctx, tool_name, invocation_id, content, is_dispatch, is_silent, tool_call_id,
            )
```

第 254-267 行（放行备注 + parts 汇总）：

```python
        text = "\n".join(result_parts) or ("(no output)" if not is_error else "")
        # 工具输出过长 → 委托 fs provider 落盘；在 human note / 审计 / memory ingest 之前。
        text = await self._maybe_spill(text, ctx, invocation_id, tool_name, cap.spillable)
        # 人类备注：文本前置进 text，备注里的图片 part 与工具结果的 part 一起进最终 content。
        # 顺序为「备注图 → 工具图」，与文本顺序一致（[Human note: …] 也在工具输出之前）。
        note_text, note_parts = split_for_tool_result(decision.message)
        if note_text or note_parts:
            text = f"[Human note: {note_text}]\n{text}"
        content: str | list[ContentPart] = text
        parts = metadata.get(CONTENT_PARTS_KEY)
        parts = list(parts) if isinstance(parts, (list, tuple)) else []
        if note_parts or parts:
            # 过归一层：宿主 provider 可能给 dict 形态的 part（JSON 往返），
            # 与 MemoryEvent / LLMMessage 的 __post_init__ 共用同一份归一。
            content = normalize_content_parts([TextPart(text=text), *note_parts, *parts])
```

文件顶部的 `from ctx_weft.core.content import ...` 里补上
`content_with_prefix`、`content_with_suffix`、`split_for_tool_result`。

- [ ] **Step 6: 跑测试确认通过**

Run: `.venv/Scripts/python.exe -m pytest tests/unit/test_approval_note_multimodal.py tests/unit/test_tool_result_content_parts.py -q`
Expected: all passed

- [ ] **Step 7: 变异验证**

两次，各自确认能被抓到：
1. 把 Step 5 第二处的 `note_parts` 从 `normalize_content_parts([...])` 里去掉 →
   `test_human_note_with_image_reaches_the_model` 与 `test_note_parts_precede_tool_result_parts` 必须红。
2. 把 Step 5 第一处改回 f-string `f"[Blocked by human: {decision.message}]"` →
   `test_blocked_with_image_keeps_the_part_and_both_affixes` 必须红。

两次都确认后改回。另外确认 `test_tool_result_content_parts.py::test_human_note_prefixes_only_the_text_part`
（既有测试，note 是纯 str）仍绿——它钉的正是纯文本路径不变。

- [ ] **Step 8: 跑全量**

Run:
```bash
find src -name __pycache__ -type d -exec rm -rf {} + 2>/dev/null
.venv/Scripts/python.exe -m pytest tests -q 2>&1 | tail -6
```
Expected: 只剩三条既有失败。

- [ ] **Step 9: 提交**

```bash
git add src/ctx_weft/protocols/capability.py src/ctx_weft/providers/authorizer/human.py src/ctx_weft/core/loop/capability_gateway.py tests/unit/test_approval_note_multimodal.py
git commit -m "fix(authz): approval 的人工备注带图时不再静默丢图

AuthorizationDecision.message 放宽为 str | list[ContentPart]，human.py 直接透传
HitlRequest.message 而不是 content_to_text 展平。gateway 的两处拼接
（[Human note: …] / [Blocked by human: …]）改走 content_with_prefix / content_with_suffix
——对 str 逐字节原样，对 parts 并进首/末个 TextPart，不会被 f-string 拍成 repr。

备注图排在工具结果图之前，与文本顺序一致。备注是纯 str 且工具无 parts 时 content
仍是同一个 str，纯文本路径不变。

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

## Task 7: 文档订正 + 升级须知

**Files:**
- Modify: `src/ctx_weft/protocols/hitl.py`（模块 docstring + `modified_arguments` 注释）
- Modify: `src/ctx_weft/core/orchestrator/hitl_manager.py`（模块 docstring + `_normalize_message` docstring）
- Modify: `docs/spec/05-authz-and-hitl.md:118-126, 135-142, 157-163, 176-186, 212`
- Modify: `README.md`（在第 1097 行「升级须知（blob 字节移出 memory）」之后新增一节）
- Test: `tests/unit/test_hitl_request_model.py`（已有 `test_hitl_status_symbol_is_gone` 覆盖；本任务无新测试）

- [ ] **Step 1: 订正 `protocols/hitl.py:67` 的 `modified_arguments` 注释**

```python
    # approval form：人类改写后的工具参数。**生效**——经 `human.py` 透进
    # `AuthorizationDecision.modified_arguments`，`capability_gateway` 的
    # `effective_args` 用它替换原参，再过 `_coerce_args` 交给 provider。
    # （旧注释写的「暂仅记录，不生效」是错的，会让人以为人工改参是安全的空操作。）
    modified_arguments: dict[str, Any] | None = None
```

- [ ] **Step 2: 订正 `protocols/hitl.py` 模块 docstring 的开闭说明**

把「``status`` 相反是**闭集**——状态机是 core 的不变式，新增状态会破坏 reducer 投影。」
换成：

```
``outcome`` 与 ``form`` 一样是开放 ``str``。真正封闭的是 **事件类型**——`HitlManager._emit`
对 ``EVENT_TYPES`` 做运行期校验并抛 `ValueError`，而 5 个 resolve 事件
（Approved/Modified/Answered/Rejected/Cancelled）映到 3 个内建 outcome，
``outcome`` 是它的**有损投影**。reducer 不消费 outcome，reducer **生产**它。
```

- [ ] **Step 3: 订正 `hitl_manager._normalize_message` 的 docstring**

把「携图内容则**响亮抛错**」那段换成：

```
        未注入 normalizer（纯单测直接构造 `HitlManager()`）→ 内容原样返回同一对象，
        event 侧载荷就地按 `NullEventBlobStore` 算。纯文本是零开销直通。

        携图内容的实际行为（实测，**不是**统一的「响亮抛错」）：
        - 合法 base64 / 白名单外 media_type / 超 5 MiB → 抛 `NotImplementedError`，
          来自 `NullEventBlobStore.put`。注意这**不是**格式校验——白名单与尺寸上限在这条
          路上一次都没跑（`validate_content` 只在注入了 normalizer 时才经过），
          当前的「响亮」是巧合而非设计。
        - ``source_type="ref"`` → **不抛**，静默降级成 `[image {media_type}]` 占位 + warning
          （与生产路径同口径）。

        裸 `HitlManager` 从来不是生产路径（生产恒由 `CtxWeftRuntime` 构造并接线 normalizer）。
```

- [ ] **Step 4: 同步 `docs/spec/05-authz-and-hitl.md` 的状态机词汇**

- 第 118-126 行的状态图：`accepted` / `rejected` / `cancelled` 保留为**内建 outcome 值**，
  把「`pending`」一列改成「未决（`outcome == ""`）」，并在图下加一句：
  `> outcome 值域开放，host 可为自定义 form 定义自己的结局；core 只认上面三个内建值。`
- 第 135 行的字段清单：`status` 改 `outcome`。
- 第 138 行：`accepted` 便捷属性 = `outcome == "accepted"`；补一句
  `resolved 便捷属性 = outcome != ""`。
- 第 142 行：把「`status` 相反是闭集」按 Step 2 的口径改写。
- 第 157-163 行的表格与 `> 消费方按 status 判定` 一段：`status="accepted"` → `outcome="accepted"` 等。
- 第 212 行清单项：`状态机 pending→accepted/rejected/cancelled` 改为
  `状态机 未决(outcome="")→内建三值或 host 自定义 outcome；resolved/accepted 为推导属性`。

- [ ] **Step 5: 在 `README.md` 新增升级须知一节**

插在第 1097 行「## 升级须知（blob 字节移出 memory）」那一节之后：

```markdown
## 升级须知（HITL 契约两维化 + 多模态出口）

- **破坏性变更：`HitlRequest.status` 已删除，换成 `outcome`。**
  `status` 的四值 `Literal["pending","accepted","rejected","cancelled"]` 是按「批准一次
  工具调用」造的，却要同时服务 `question`（accepted = 人答了）与 `wait`（accepted = 人
  发了条消息）。现在是：

  ```python
  req.outcome     # str，开放值域。"" = 未决；"accepted"/"rejected"/"cancelled" 是内建值
  req.resolved    # 推导属性：bool(outcome)
  req.accepted    # 推导属性：outcome == "accepted"
  req.resolve(o)  # 终局的唯一写入点
  ```

  host 定义了自己的 `form`，现在也能定义自己的 `outcome`。空串是未决哨兵，不要占用。
  `HitlStatus` 已从 `ctx_weft.protocols` 移出，改用 `HitlOutcome` 与
  `HITL_OUTCOME_ACCEPTED` / `HITL_OUTCOME_REJECTED` / `HITL_OUTCOME_CANCELLED`。

- **破坏性变更：`AuthorizationDecision.message` 放宽为 `str | list[ContentPart]`。**
  自实现 Authorizer 只**写** str 的不受影响；**读** `decision.message` 的要处理 parts
  （用 `core.content.split_for_tool_result` 或 `core.utils.content_to_text`）。

- **破坏性变更：`Authorizer.filter()` 已删除。** 它零调用点，且对
  `HumanConfirmationAuthorizer` 会真的发出一个 HITL 请求并等人——把「列出可见工具」
  变成「逐个求批」。需要装配期可见性过滤请自行实现，并显式排除会挂起的 authorizer。

- **行为变更：`ask_user` 与 approval 备注现在会把人贴的图带给模型。** 此前这两条出口
  用 `content_to_text` 展平，非文本 part 被静默丢弃（连占位都不留）。工具结果因此可能
  是 `list[ContentPart]` 而非 `str`——`InvocationResult.content` 早已声明为该联合类型，
  host 通常无感。

- **不变**：事件类型、事件 payload 形状、`RunSnapshot` 序列化、golden 数据。
  旧事件日志与旧快照原样可读，无需迁移。
```

- [ ] **Step 6: 跑全量 + ruff**

Run:
```bash
find src -name __pycache__ -type d -exec rm -rf {} + 2>/dev/null
.venv/Scripts/python.exe -m pytest tests -q 2>&1 | tail -6
.venv/Scripts/python.exe -m ruff check --output-format=concise src tests 2>&1 | grep -v "RUF00" | tail -10
```
Expected: 只剩三条既有失败；ruff 无新增告警。

- [ ] **Step 7: 提交**

```bash
git add src/ctx_weft/protocols/hitl.py src/ctx_weft/core/orchestrator/hitl_manager.py docs/spec/05-authz-and-hitl.md README.md
git commit -m "docs: 订正三处与代码不符的说明，补 HITL 契约的升级须知

modified_arguments 的「暂仅记录，不生效」是错的——它经 human.py 透进
AuthorizationDecision，再由 gateway 的 effective_args 替换原参交给 provider。
这条注释会让人以为人工改参是安全的空操作。

hitl.py 模块 docstring 的「status 是闭集，新增状态会破坏 reducer 投影」因果反了：
reducer 不消费它、reducer 生产它。真正封闭的是 EventType（_emit 运行期校验）。

_normalize_message 的「携图必炸」也不准：source_type='ref' 不抛（静默降级 + warning），
而会抛的三种抛的都是 NullEventBlobStore.put 的 NotImplementedError，不是格式校验
——白名单与尺寸上限在裸路径上一次都没跑，当前的「响亮」是巧合。

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

## 完成判据

全部七个任务完成后：

1. `.venv/Scripts/python.exe -m pytest tests -q` 只剩 Global Constraints 里列的三条既有失败。
2. `grep -rn "HitlStatus\|\.status" src/ctx_weft/protocols/hitl.py src/ctx_weft/core/orchestrator/hitl_manager.py` 无 HITL status 残留。
3. `grep -rn "content_to_text(approval" src/` 无命中（两条出口都已改）。
4. `grep -rn "def filter" src/ctx_weft/protocols/capability.py` 无命中。
5. ruff 无新增告警（RUF001/2/3 中文标点是全仓基线）。
6. 每个任务的变异验证都实测过并记录在提交信息或 PR 描述里。
