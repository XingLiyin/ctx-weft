# /messages 职责拆分 + 前端走精确 HITL 接口 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** HITL 应答的会话副作用+resolve 下沉为共享服务 `resolve_hitl`，`/hitl/*` 成为完整第一公民入口，`/messages` PAUSED* 分支瘦身为薄委托；SSE `waiting_input` 增发 `hitl_id`/`form`；frontend-desktop 与老 frontend/ 的应答改走 `/hitl/{id}/*` 并带兜底链。

**Architecture:** 新模块 `api/hitl_service.py` 承载定位自愈 + 会话副作用（transcript 追加→置 RUNNING→重启 consumer→workspace 重登记→entry LLM 语义）+ resolve；`api/hitl.py` 与 `api/sessions.py` 都只调它。前端加 `api/hitl.ts`，应答按 `waitingInput.hitl_id` 精确路由，兜底链：SSE 无 id → `GET /hitl/pending` 匹配 → 仍无 → 旧 `/messages` 通道。

**Tech Stack:** FastAPI/pydantic v2（`model_fields_set`）、pytest（`uv run pytest`）、React+TanStack Query+TS（两前端）。

**Spec:** `docs/superpowers/specs/2026-07-05-messages-split-precise-hitl-design.md`（含 2026-07-05b 修订）

## Global Constraints

- 分支：继续在 `refactor/hitl-id-unify` 上开发（同一特性），不拉新分支。
- 不动 core（ctx-weft）任何代码。
- `/messages` 其余分支（RUNNING/INTERRUPTED 409、终态续聊）不动；PAUSED* 分支保留为薄委托（deprecated）。
- SSE `waiting_input` 只增字段（`hitl_id`、`form`），既有字段名与取值不动；`GET /hitl/pending` 响应不变。
- `/hitl` 三端点响应形状不变（`{id, status}`）；body 新增字段全部可选（旧调用方不受影响）。
- llm 语义（spec 修订 2026-07-05b）：`/hitl` body 的 `llm_account/llm_model` **出现在请求里**才生效（`model_fields_set`）；truthy account→设 entry 两值；显式 null→重置为 None；缺席→不动 entry。
- 副作用顺序不可变（提炼自 `_submit_hitl_response`）：transcript 追加 → 置 RUNNING+session_update → 重启 consumer（**resolve 前**）→ workspace 重登记 → resolve。
- 测试命令一律 `uv run pytest ...`；前端构建 `npm run build`（各自目录）。已知 master 既有失败 5 个（ctx-weft `test_fs_config`×2、`tests/test_host_config_runner_settings`×1、`tests/test_skills_pull_store`×2），不计入本特性。
- `uv.lock` 被 `uv run` 顺手改动时 `git checkout -- uv.lock`，勿提交。
- 不改 frontend-desktop-v2。

---

### Task 1: 后端共享服务 `hitl_service.py`

**Files:**
- Create: `src/ipmastercowork/api/hitl_service.py`
- Modify: `src/ipmastercowork/api/sessions.py:66-89`（删本地 `_ensure_workspace_registered`，改为 re-export import）
- Test: `tests/test_hitl_service.py`（新建）

**Interfaces:**
- Consumes: `deps.get_hitl_manager/get_runtime_optional`；`_sm._sessions/_now/session_consumer`（`api/models/session.py`）；entry 鸭子类型（`_append_json/cond/sse_events/_session_update_json/_consumer_token/sse_finished/llm_account/llm_model/status/updated_at`）。
- Produces（后续任务依赖）:
  - `async def resolve_hitl(hitl_id: str, action: str, *, text: str = "", message: str = "", modify: dict | None = None, echo_text: str | None = None, entry: Any | None = None, llm: tuple[str | None, str | None] | None = None) -> HitlRequest`，未知 id 抛 `KeyError`
  - `async def _ensure_workspace_registered(runtime, session_id) -> None`（自 sessions.py 原样迁入，成为唯一定义）
- 本任务行为零变化：sessions.py 仅改 import；`_submit_hitl_response` 尚未接入（Task 3）。

- [ ] **Step 1: 写失败测试**

新建 `tests/test_hitl_service.py`（惯用法取自 `tests/test_hitl_reply_self_heal.py`：真实 `CtxWeftRuntime` + 事件流 + `_Entry` stub + deps set/reset）：

```python
"""resolve_hitl 共享服务:会话副作用 + resolve 的单一实现(spec 2026-07-05 /messages 拆分)。"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone

import pytest

from ctx_weft.core import CtxWeftRuntime
from ctx_weft.core.events.types import Event, EventType
from ipmastercowork.api import deps
from ipmastercowork.api.models import session as _sm

_TS = datetime(2026, 6, 13, tzinfo=timezone.utc)


class _StubResolver:
    async def get(self, *a, **k): raise KeyError("not used")
    async def list_summaries(self, *a, **k): return []


class _Entry:
    def __init__(self, sid: str) -> None:
        self.session_id = sid
        self.status = "PAUSED_HITL"
        self.llm_account = "acct-a"
        self.llm_model = "model-a"
        self.updated_at = ""
        self.sse_events: list[str] = []
        self.cond = asyncio.Condition()
        self.appended: list[str] = []
        self._consumer_token = 0
        self.sse_finished = False

    async def _append_json(self, s: str) -> None:
        self.appended.append(s)

    async def append_event(self, ev) -> None:
        pass

    def _session_update_json(self, status: str) -> str:
        return f'{{"type":"session_update","status":"{status}"}}'

    def to_dict(self) -> dict:
        return {"status": self.status}


def _ev(sid: str, seq: int, type_: EventType, **payload) -> Event:
    return Event(id=f"evt_{sid}_{seq}", run_id="r1", sequence=seq, session_id=sid,
                 type=type_, timestamp=_TS, payload=payload)


async def _mk_runtime(sid: str, *, form: str = "question") -> CtxWeftRuntime:
    runtime = CtxWeftRuntime(template_resolver=_StubResolver())
    await runtime.event_store.append(_ev(sid, 1, EventType.SESSION_CREATED, template_id="t"))
    await runtime.event_store.append(
        _ev(sid, 2, EventType.HITL_REQUIRED, hitl_id="h1", form=form, tool_call_id="tc1"))
    await runtime.rebuild_hitl(sid)
    return runtime


async def test_answer_full_side_effects_in_order(monkeypatch) -> None:
    """entry 在:transcript 追加→RUNNING→consumer 重启→workspace 重登记→resolve(带 entry LLM)。"""
    from ipmastercowork.api import hitl_service

    sid = "ses_svc"
    runtime = await _mk_runtime(sid)

    async def _noop_recover_session(s, **k): return None
    monkeypatch.setattr(runtime, "recover_session", _noop_recover_session)

    order: list[str] = []
    async def _spy_ws(rt, session_id):
        order.append(f"ws:{session_id}")
    monkeypatch.setattr(hitl_service, "_ensure_workspace_registered", _spy_ws)

    entry = _Entry(sid)
    deps.set_runtime(runtime)
    deps.set_hitl_manager(runtime.hitl_manager)
    try:
        req = await hitl_service.resolve_hitl("h1", "answer", text="use postgres", entry=entry)
        assert req.status == "accepted" and req.message == "use postgres"
        # transcript:user 消息且内容为答复原文
        msg = json.loads(entry.appended[0])
        assert msg["role"] == "user" and msg["content"] == "use postgres"
        # 状态翻转 + SSE
        assert entry.status == "RUNNING"
        assert any("session_update" in s for s in entry.sse_events)
        # consumer token 自增(重启)
        assert entry._consumer_token == 1 and entry.sse_finished is False
        # workspace 重登记发生在 resolve 之前(resolve 后才 append 不了序;以 spy 顺序为证)
        assert order == [f"ws:{sid}"]
        # entry LLM 未被动过(未传 llm)
        assert entry.llm_account == "acct-a" and entry.llm_model == "model-a"
    finally:
        deps.set_runtime(None)
        deps.set_hitl_manager(None)


async def test_approve_reject_echo_defaults(monkeypatch) -> None:
    """approve/reject 的 transcript 缺省回显词与前端旧字面量一致。"""
    from ipmastercowork.api import hitl_service

    sid = "ses_echo"
    runtime = await _mk_runtime(sid, form="approval")
    async def _noop(s, **k): return None
    monkeypatch.setattr(runtime, "recover_session", _noop)
    async def _noop_ws(rt, session_id): return None
    monkeypatch.setattr(hitl_service, "_ensure_workspace_registered", _noop_ws)

    entry = _Entry(sid)
    deps.set_runtime(runtime)
    deps.set_hitl_manager(runtime.hitl_manager)
    try:
        await hitl_service.resolve_hitl("h1", "reject", message="danger", entry=entry)
        msg = json.loads(entry.appended[0])
        assert msg["content"] == "rejected danger"
    finally:
        deps.set_runtime(None)
        deps.set_hitl_manager(None)


async def test_llm_semantics(monkeypatch) -> None:
    """llm=None 不动 entry;llm=("b","m2") 设置;llm=(None,None) 重置。"""
    from ipmastercowork.api import hitl_service

    async def _noop_ws(rt, session_id): return None
    monkeypatch.setattr(hitl_service, "_ensure_workspace_registered", _noop_ws)

    for llm, want in [(None, ("acct-a", "model-a")),
                      (("acct-b", "model-b"), ("acct-b", "model-b")),
                      ((None, None), (None, None))]:
        sid = f"ses_llm_{id(llm)}"
        runtime = await _mk_runtime(sid)
        async def _noop(s, **k): return None
        monkeypatch.setattr(runtime, "recover_session", _noop)
        entry = _Entry(sid)
        deps.set_runtime(runtime)
        deps.set_hitl_manager(runtime.hitl_manager)
        try:
            await hitl_service.resolve_hitl("h1", "answer", text="x", entry=entry, llm=llm)
            assert (entry.llm_account, entry.llm_model) == want
        finally:
            deps.set_runtime(None)
            deps.set_hitl_manager(None)


async def test_entry_miss_resolves_without_session_side_effects(monkeypatch) -> None:
    """entry 不在注册表:跳过会话副作用,resolve 照走,workspace 仍重登记。"""
    from ipmastercowork.api import hitl_service

    sid = "ses_nomem"
    runtime = await _mk_runtime(sid)
    async def _noop(s, **k): return None
    monkeypatch.setattr(runtime, "recover_session", _noop)
    calls: list[str] = []
    async def _spy_ws(rt, session_id): calls.append(session_id)
    monkeypatch.setattr(hitl_service, "_ensure_workspace_registered", _spy_ws)

    deps.set_runtime(runtime)
    deps.set_hitl_manager(runtime.hitl_manager)
    try:
        req = await hitl_service.resolve_hitl("h1", "answer", text="ok")
        assert req.status == "accepted"
        assert calls == [sid]
    finally:
        deps.set_runtime(None)
        deps.set_hitl_manager(None)


async def test_unknown_id_raises_keyerror() -> None:
    runtime = CtxWeftRuntime(template_resolver=_StubResolver())
    await runtime.event_store.append(_ev("s", 1, EventType.SESSION_CREATED, template_id="t"))
    deps.set_runtime(runtime)
    deps.set_hitl_manager(runtime.hitl_manager)
    try:
        from ipmastercowork.api.hitl_service import resolve_hitl
        with pytest.raises(KeyError):
            await resolve_hitl("nope", "answer", text="x")
    finally:
        deps.set_runtime(None)
        deps.set_hitl_manager(None)
```

- [ ] **Step 2: 跑测试确认失败**

Run: `uv run pytest tests/test_hitl_service.py -v`
Expected: FAIL，`ModuleNotFoundError: No module named 'ipmastercowork.api.hitl_service'`

- [ ] **Step 3: 实现 `hitl_service.py`**

新建 `src/ipmastercowork/api/hitl_service.py`：

```python
"""HITL 应答共享服务:会话副作用 + resolve 的单一实现(spec 2026-07-05 /messages 拆分)。

`/hitl/{id}/*` 端点与 `/messages` 薄委托(PAUSED* 分支)都经由 resolve_hitl,消除双份副作用逻辑。
副作用顺序有意义(提炼自旧 _submit_hitl_response,勿重排):
  transcript 追加 user 消息 → entry 置 RUNNING+session_update → 重启 session_consumer
  (resolve 前,先订阅 bus 再产生事件) → workspace 重登记 → resolve(带 entry 当前 LLM)。
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from ipmastercowork.api import deps
from ipmastercowork.api.models import session as _sm

logger = logging.getLogger(__name__)


async def _ensure_workspace_registered(runtime: Any, session_id: str) -> None:
    """resume / 重跑前确保 fs provider 持有该 session 的 workspace。

    fs provider 的映射是内存缓存：session 结束（_on_done）或进程重启都会丢失，而 sessions 行的
    workspace 列是真值来源。这里从存储读回并重新登记（register 幂等），覆盖跨进程 resume 与
    同进程 terminal→重跑两种情况。无存储/无记录/无 fs provider 时静默跳过。
    """
    if _sm._state_store is None:
        return
    workspace = await _sm._state_store.get_workspace(session_id)
    if not workspace:
        return
    from ctx_weft.providers.capability_filesystem import FilesystemToolsProvider
    fs = next(
        (p for p in runtime.providers.get_capability_providers()
         if isinstance(p, FilesystemToolsProvider)),
        None,
    )
    if fs is None:
        return
    try:
        fs.register_session(session_id, workspace)
    except Exception:
        logger.warning("Workspace re-register failed for session %s (path %r)", session_id, workspace)


async def _locate_request(hitl: Any, hitl_id: str) -> Any:
    """按 id 取请求;miss(重启后内存未 recover)→ 据事件重建全部 active pending 再取(spec/07 §9 自愈);
    仍 miss → KeyError(调用方转 404)。"""
    req = hitl.get(hitl_id)
    if req is not None:
        return req
    runtime = deps.get_runtime_optional()
    if runtime is not None:
        try:
            await runtime.rebuild_all_pending_hitl()
        except Exception:
            pass
        req = hitl.get(hitl_id)
    if req is None:
        raise KeyError(f"No HITL request found: {hitl_id}")
    return req


def _default_echo(action: str, *, text: str, message: str) -> str:
    """transcript 回显缺省:answer=答复原文;approve/reject=旧前端按钮字面量(+可选备注),气泡配对不破。"""
    if action == "answer":
        return text
    word = "approved" if action == "approve" else "rejected"
    return f"{word} {message}".strip() if message else word


async def resolve_hitl(
    hitl_id: str,
    action: str,
    *,
    text: str = "",
    message: str = "",
    modify: dict[str, Any] | None = None,
    echo_text: str | None = None,
    entry: Any | None = None,
    llm: tuple[str | None, str | None] | None = None,
) -> Any:
    """resolve 一条 HITL 请求,并补齐前端可见的全部会话副作用。

    action ∈ {"answer","approve","reject"}。``echo_text``=transcript 展示的 user 消息(None→按
    action 缺省)。``entry``:/messages 薄委托传入其 entry;None → 按请求的 session_id 查注册表。
    ``llm``:(account, model)——truthy account→设 entry 两值;None account→重置为 None(与 /messages
    语义同);整个参数为 None→不动 entry(面板应答不再误重置)。entry 不在(纯 API 调用、无前端
    会话)→ 跳过会话副作用,resolve 照走。未知 hitl_id → KeyError。
    """
    hitl = deps.get_hitl_manager()
    if hitl is None:
        raise RuntimeError("HitlManager not initialized")
    req = await _locate_request(hitl, hitl_id)

    if entry is None:
        entry = _sm._sessions.get(req.session_id)
    runtime = deps.get_runtime_optional()

    if entry is not None:
        if llm is not None:
            account, model = llm
            if account:
                entry.llm_account = account
                entry.llm_model = model or None
            else:
                entry.llm_account = None
                entry.llm_model = None
        shown = echo_text if echo_text is not None else _default_echo(action, text=text, message=message)
        await entry._append_json(json.dumps({
            "type": "message", "role": "user",
            "content": shown, "created_at": _sm._now(),
        }))
        async with entry.cond:
            entry.status = "RUNNING"
            entry.updated_at = _sm._now()
            entry.sse_events.append(entry._session_update_json("RUNNING"))
            entry.cond.notify_all()
        if runtime is not None:
            # 重启 consumer:重启后原 consumer 已随进程消失,冷恢复续跑事件到不了前端即"卡住"。
            entry._consumer_token += 1
            entry.sse_finished = False
            asyncio.create_task(_sm.session_consumer(entry, runtime, entry._consumer_token))
            # 冷应答触发 recover_session 续跑前,从存储重新登记 workspace(内存映射随重启丢失)。
            await _ensure_workspace_registered(runtime, req.session_id)
        llm_kw: dict[str, Any] = {"llm_account": entry.llm_account, "llm_model": entry.llm_model}
    else:
        logger.warning(
            "resolve_hitl: no session entry for %s (session %s) — resolving without session side effects",
            hitl_id, req.session_id)
        if runtime is not None:
            await _ensure_workspace_registered(runtime, req.session_id)
        llm_kw = {}

    if action == "reject":
        return await hitl.reject(hitl_id, message=message, **llm_kw)
    if action == "approve":
        return await hitl.approve(hitl_id, message=message, modified_arguments=modify, **llm_kw)
    return await hitl.answer(hitl_id, text, **llm_kw)
```

`src/ipmastercowork/api/sessions.py`：删除 66-89 行的 `_ensure_workspace_registered` 函数定义，在 import 区（`from ipmastercowork.api.schemas.sessions import ...` 之后）加：

```python
from ipmastercowork.api.hitl_service import _ensure_workspace_registered  # noqa: F401  (本模块 3 处调用 + 既有测试 monkeypatch 目标)
```

（sessions.py 内 `resume_session`/`send_message` 等既有调用点无需改动——模块内引用同名全局。）

- [ ] **Step 4: 跑测试确认通过**

Run: `uv run pytest tests/test_hitl_service.py tests/test_hitl_reply_self_heal.py tests/test_workspace_endpoints.py -v`
Expected: 全 PASS（self_heal 与 workspace 既有测试不受影响——`_submit_hitl_response` 本任务未动）

- [ ] **Step 5: Commit**

```bash
git add src/ipmastercowork/api/hitl_service.py src/ipmastercowork/api/sessions.py tests/test_hitl_service.py
git commit -m "feat(host): resolve_hitl 共享服务——HITL 应答会话副作用+resolve 单一实现"
```

---

### Task 2: `/hitl` 端点接入服务 + body 可选 llm 字段

**Files:**
- Modify: `src/ipmastercowork/api/hitl.py`
- Modify: `src/ipmastercowork/api/schemas/hitl.py`
- Modify: `tests/test_hitl_reply_self_heal.py:149-183`（REST workspace spy 目标迁移）
- Test: `tests/test_hitl_service.py`（追加端点级用例）

**Interfaces:**
- Consumes: Task 1 的 `resolve_hitl`。
- Produces: `AnswerRequest{answer, llm_account?, llm_model?}`、`ApproveRequest{modify?, llm_account?, llm_model?}`、`RejectRequest{message="", llm_account?, llm_model?}`；端点响应 `{id, status}` 不变。
- llm 出现判定：`("llm_account" in req.model_fields_set or "llm_model" in req.model_fields_set)` → `llm=(req.llm_account, req.llm_model)`，否则 `llm=None`。

- [ ] **Step 1: 写失败测试（端点带副作用 + llm 字段）**

`tests/test_hitl_service.py` 追加：

```python
async def test_rest_answer_touches_entry_side_effects(monkeypatch) -> None:
    """/hitl/{id}/answer 经服务:注册表里的 entry 拿到 transcript/RUNNING/consumer 副作用。"""
    from ipmastercowork.api import hitl as hitl_api
    from ipmastercowork.api import hitl_service
    from ipmastercowork.api.schemas.hitl import AnswerRequest

    sid = "ses_rest_fx"
    runtime = await _mk_runtime(sid)
    async def _noop(s, **k): return None
    monkeypatch.setattr(runtime, "recover_session", _noop)
    async def _noop_ws(rt, session_id): return None
    monkeypatch.setattr(hitl_service, "_ensure_workspace_registered", _noop_ws)

    entry = _Entry(sid)
    _sm._sessions[sid] = entry
    deps.set_runtime(runtime)
    deps.set_hitl_manager(runtime.hitl_manager)
    try:
        out = await hitl_api.answer("h1", AnswerRequest(answer="ok"), hitl=runtime.hitl_manager)
        assert out["status"] == "accepted"
        assert entry.status == "RUNNING" and entry._consumer_token == 1
        assert json.loads(entry.appended[0])["content"] == "ok"
        # body 未带 llm → entry LLM 不被动(修复旧 /messages 面板应答重置 bug)
        assert entry.llm_account == "acct-a"
    finally:
        _sm._sessions.pop(sid, None)
        deps.set_runtime(None)
        deps.set_hitl_manager(None)


async def test_rest_answer_llm_fields_apply_when_present(monkeypatch) -> None:
    """body 带 llm_account → entry 更新;显式 null → 重置。"""
    from ipmastercowork.api import hitl as hitl_api
    from ipmastercowork.api import hitl_service
    from ipmastercowork.api.schemas.hitl import AnswerRequest

    async def _noop_ws(rt, session_id): return None
    monkeypatch.setattr(hitl_service, "_ensure_workspace_registered", _noop_ws)

    for body, want in [
        (AnswerRequest(answer="x", llm_account="acct-b", llm_model="model-b"), ("acct-b", "model-b")),
        (AnswerRequest(answer="x", llm_account=None, llm_model=None), (None, None)),
    ]:
        sid = f"ses_rest_llm_{want[0]}"
        runtime = await _mk_runtime(sid)
        async def _noop(s, **k): return None
        monkeypatch.setattr(runtime, "recover_session", _noop)
        entry = _Entry(sid)
        _sm._sessions[sid] = entry
        deps.set_runtime(runtime)
        deps.set_hitl_manager(runtime.hitl_manager)
        try:
            await hitl_api.answer("h1", body, hitl=runtime.hitl_manager)
            assert (entry.llm_account, entry.llm_model) == want
        finally:
            _sm._sessions.pop(sid, None)
            deps.set_runtime(None)
            deps.set_hitl_manager(None)
```

- [ ] **Step 2: 跑测试确认失败**

Run: `uv run pytest tests/test_hitl_service.py -v -k rest`
Expected: 新增两用例 FAIL（现端点不产生 entry 副作用 / schema 无 llm 字段报 ValidationError 或 entry 未更新）

- [ ] **Step 3: 实现**

a. `src/ipmastercowork/api/schemas/hitl.py` 三个请求类改为：

```python
class ApproveRequest(BaseModel):
    modify: dict[str, Any] | None = None
    # 可选:随应答切换会话 LLM(语义与 /messages 同;缺席=不动,见 hitl_service.resolve_hitl)
    llm_account: str | None = None
    llm_model: str | None = None


class AnswerRequest(BaseModel):
    answer: str
    llm_account: str | None = None
    llm_model: str | None = None


class RejectRequest(BaseModel):
    message: str = ""
    llm_account: str | None = None
    llm_model: str | None = None
```

b. `src/ipmastercowork/api/hitl.py` 全量替换为：

```python
"""HITL API endpoints。应答经 resolve_hitl 共享服务:会话副作用(transcript/RUNNING/consumer/
workspace)与 resolve 一体,是前端应答的第一公民入口(spec 2026-07-05 /messages 拆分)。"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException

from . import deps
from .hitl_service import resolve_hitl
from .schemas.hitl import AnswerRequest, ApproveRequest, HitlPendingItem, RejectRequest

router = APIRouter(prefix="/hitl", tags=["hitl"])


def _llm_of(req: Any) -> tuple[str | None, str | None] | None:
    """body 里**出现** llm 字段才生效(缺席=不动 entry,面板应答不误重置)。"""
    if "llm_account" in req.model_fields_set or "llm_model" in req.model_fields_set:
        return (req.llm_account, req.llm_model)
    return None


@router.get("/pending", response_model=list[HitlPendingItem])
async def list_pending(
    session_id: str | None = None,
    hitl=Depends(deps.require_hitl_manager),
):
    pending = hitl.list_pending(session_id=session_id)
    if not pending:
        # 自愈:重启后内存可能还没被 recover 填上 → 据事件重建再列。
        runtime = deps.get_runtime_optional()
        if runtime is not None:
            try:
                if session_id:
                    await runtime.rebuild_hitl(session_id)
                else:
                    await runtime.rebuild_all_pending_hitl()
            except Exception:
                pass
            pending = hitl.list_pending(session_id=session_id)
    return [
        HitlPendingItem(
            id=a.id, kind=("approval" if a.form == "approval" else "input"),
            status=a.status, capability_id=a.capability_id,
            question=a.question, task_id=a.task_id, session_id=a.session_id,
            agent_id=a.agent_id, form=a.form,
        )
        for a in pending
    ]


@router.post("/{hitl_id}/approve")
async def approve(
    hitl_id: str,
    req: ApproveRequest,
    hitl=Depends(deps.require_hitl_manager),
):
    """放行 approval-form 请求（工具门控）。冷应答的 session resume 由 HitlManager 处理。"""
    try:
        resolved = await resolve_hitl(hitl_id, "approve", modify=req.modify, llm=_llm_of(req))
        return {"id": resolved.id, "status": resolved.status}
    except KeyError:
        raise HTTPException(status_code=404, detail=f"HITL request {hitl_id} not found")


@router.post("/{hitl_id}/answer")
async def answer(
    hitl_id: str,
    req: AnswerRequest,
    hitl=Depends(deps.require_hitl_manager),
):
    """应答 question/wait form 请求（向人提问/纯文本暂停），文字答复回灌给 LLM。"""
    try:
        resolved = await resolve_hitl(hitl_id, "answer", text=req.answer, llm=_llm_of(req))
        return {"id": resolved.id, "status": resolved.status}
    except KeyError:
        raise HTTPException(status_code=404, detail=f"HITL request {hitl_id} not found")


@router.post("/{hitl_id}/reject")
async def reject(
    hitl_id: str,
    req: RejectRequest | None = None,
    hitl=Depends(deps.require_hitl_manager),
):
    """拒绝（approval 与 question/wait 通用）；可带 message 作为指导反馈回灌给 agent。"""
    try:
        resolved = await resolve_hitl(
            hitl_id, "reject",
            message=req.message if req else "",
            llm=_llm_of(req) if req else None,
        )
        return {"id": resolved.id, "status": resolved.status}
    except KeyError:
        raise HTTPException(status_code=404, detail=f"HITL request {hitl_id} not found")
```

（`_heal_and_retry`、`_reregister_workspace_for_hitl` 整体删除——自愈定位与 workspace 重登记已在服务内。）

c. `tests/test_hitl_reply_self_heal.py` 的 `test_rest_reply_reregisters_workspace_before_cold_resume`（149-183 行）：spy 目标从 `sess_api._ensure_workspace_registered` 改为 `hitl_service._ensure_workspace_registered`：

```python
    from ipmastercowork.api import hitl_service
    ...
    monkeypatch.setattr(hitl_service, "_ensure_workspace_registered", _spy_ensure)
```

（该测试原 import 的 `sess_api` 若不再使用则一并删除该 import。）

- [ ] **Step 4: 跑测试确认通过**

Run: `uv run pytest tests/test_hitl_service.py tests/test_hitl_reply_self_heal.py tests/test_hitl_pending_contract.py -v`
Expected: 全 PASS（`test_rest_answer_self_heals` 经服务自愈仍绿；pending 契约不变）

- [ ] **Step 5: Commit**

```bash
git add src/ipmastercowork/api/hitl.py src/ipmastercowork/api/schemas/hitl.py tests/
git commit -m "feat(host): /hitl 端点接入 resolve_hitl 服务,body 可选 llm 字段(出现才生效)"
```

---

### Task 3: `/messages` 薄委托（deprecated）

**Files:**
- Modify: `src/ipmastercowork/api/sessions.py:130-188`（`_submit_hitl_response` 瘦身）
- Modify: `tests/test_hitl_reply_self_heal.py:86-119`（/messages workspace spy 目标迁移）

**Interfaces:**
- Consumes: `resolve_hitl(req.id, action, text=..., message=..., echo_text=content, entry=entry)`（llm=None——`send_message` 的 PAUSED 分支已先按 body 更新 entry，`sessions.py:479-484` 不动）。
- Produces: `_submit_hitl_response(entry, content) -> dict` 签名与对外行为不变（404 语义、返回 `entry.to_dict()`）。

- [ ] **Step 1: 瘦身实现**

`_submit_hitl_response`（130-188 行）替换为：

```python
async def _submit_hitl_response(entry: Any, content: str) -> dict:
    """[DEPRECATED] /messages 的 HITL 应答薄委托——聊天框回复的兼容通道。

    固定解决 pending[0](最老一条):多 pending 时有歧义,精确应答请走 /hitl/{id}/answer|approve|reject
    (前端正常流量已迁移,本分支仅剩兜底)。副作用与 resolve 统一在 hitl_service.resolve_hitl。
    文本按 route_hitl_reply 词表解析:approval form 首词判 approve/reject;question/wait 一律 answer。
    """
    from ipmastercowork.api.hitl_service import resolve_hitl

    hitl = deps.get_hitl_manager()
    if hitl is None:
        raise HTTPException(status_code=503, detail="HitlManager not initialized")
    pending = hitl.list_pending(session_id=entry.session_id)
    if not pending:
        # 自愈:重启后内存 HitlManager 可能还没被 recover 填上 → 据事件即时重建再试,避免误 404。
        runtime = deps.get_runtime_optional()
        if runtime is not None:
            try:
                await runtime.rebuild_hitl(entry.session_id)
            except Exception:
                pass
            pending = hitl.list_pending(session_id=entry.session_id)
    if not pending:
        raise HTTPException(status_code=404, detail="No pending HITL for this session")

    req = pending[0]
    action, message = route_hitl_reply(req.form, content)
    try:
        # llm=None:send_message 的 PAUSED 分支已按 body 更新过 entry,这里不重复;
        # echo_text=原文:transcript 展示用户敲的字,气泡配对不变。
        await resolve_hitl(
            req.id, action,
            text=message if action == "answer" else "",
            message=message if action != "answer" else "",
            echo_text=content, entry=entry,
        )
    except KeyError:
        raise HTTPException(status_code=404, detail="No pending HITL for this session")
    return entry.to_dict()
```

（原函数体内 transcript/RUNNING/consumer/workspace 代码全部删除；文件顶部无需新 import——函数内局部 import 避免环。）

- [ ] **Step 2: 迁移 /messages workspace spy 目标**

`tests/test_hitl_reply_self_heal.py` 的 `test_reply_reregisters_workspace_before_cold_resume`（86-119 行）：`monkeypatch.setattr(sess_api, "_ensure_workspace_registered", _spy_ensure)` 改为：

```python
    from ipmastercowork.api import hitl_service
    monkeypatch.setattr(hitl_service, "_ensure_workspace_registered", _spy_ensure)
```

- [ ] **Step 3: 跑相关测试**

Run: `uv run pytest tests/test_hitl_reply_self_heal.py tests/test_hitl_service.py tests/test_sessions_paused_routing.py tests/test_session_entry_paused.py tests/test_hitl_reply_routing.py tests/test_recovery_paused_hitl.py tests/test_hitl_rest_cold_resume.py -v`
Expected: 全 PASS（薄委托对外行为等价）

- [ ] **Step 4: 跑 host 全量**

Run: `uv run pytest tests/ -q`
Expected: 除 3 个 master 既有失败外全 PASS

- [ ] **Step 5: Commit**

```bash
git add src/ipmastercowork/api/sessions.py tests/test_hitl_reply_self_heal.py
git commit -m "refactor(host): /messages PAUSED* 分支瘦身为 resolve_hitl 薄委托(deprecated)"
```

---

### Task 4: SSE `waiting_input` 增发 `hitl_id`/`form`

**Files:**
- Modify: `src/ipmastercowork/api/models/session.py:343-356`（waiting_input dict）
- Test: `tests/test_session_entry_paused.py`（扩展断言）

**Interfaces:**
- Produces: SSE `waiting_input` 新增 `"hitl_id"`（来自 payload）与 `"form"` 字段；既有字段名与取值不动（Task 5/6 前端消费）。

- [ ] **Step 1: 写失败测试**

`tests/test_session_entry_paused.py`：找到现有构造 `HITL_REQUIRED` 事件并断言 `waiting_input` 输出的测试（含上一特性补的 form=question/approval 派生断言），在这些断言旁追加（沿用该文件既有事件构造 helper——其 HITL_REQUIRED payload 已含 `hitl_id`，若个别用例未含则补上 `hitl_id="h1"`）：

```python
    assert out["hitl_id"] == "h1"
    assert out["form"] == "question"   # form=approval 的用例断言 "approval"
```

- [ ] **Step 2: 跑测试确认失败**

Run: `uv run pytest tests/test_session_entry_paused.py -v`
Expected: 新断言 FAIL（`KeyError: 'hitl_id'`）

- [ ] **Step 3: 实现**

`src/ipmastercowork/api/models/session.py` 的 waiting_input dict（343-356 行）在 `"kind"` 行后加两行：

```python
                # 精确应答所需:前端据 hitl_id 调 /hitl/{id}/answer|approve|reject(additive 字段)
                "hitl_id": p.get("hitl_id", ""),
                "form": form,
```

- [ ] **Step 4: 跑测试确认通过**

Run: `uv run pytest tests/test_session_entry_paused.py tests/test_sse_paused_hitl_resend.py -v`
Expected: 全 PASS（重连补发同 snapshot 自动带新字段；既有断言不受影响）

- [ ] **Step 5: Commit**

```bash
git add src/ipmastercowork/api/models/session.py tests/test_session_entry_paused.py
git commit -m "feat(host): SSE waiting_input 增发 hitl_id+form(additive,前端精确应答用)"
```

---

### Task 5: frontend-desktop 改走精确接口

**Files:**
- Create: `frontend-desktop/src/api/hitl.ts`
- Modify: `frontend-desktop/src/hooks/useSessionSSE.ts:42-53`（`ChatWaitingInput`）、`:372-378`（waiting_input 处理）
- Modify: `frontend-desktop/src/components/ChatPanel.tsx:339-401`（mutations + submit 分流）、`:609-614`（面板回调）

**Interfaces:**
- Consumes: Task 2 端点、Task 4 SSE 字段；既有 `sessionsApi.answerInput/sendMessage`（兜底）、`nextProvider/nextModel`（ChatPanel 既有变量）、`http`（`./client`）。
- Produces: `hitlApi.pending/answer/approve/reject`；`resolveHitlId(sessionId, wi)` helper。

- [ ] **Step 1: 新建 `frontend-desktop/src/api/hitl.ts`**

```ts
import { http } from './client'

export interface HitlPendingItem {
  id: string
  kind: 'approval' | 'input'
  status: string
  capability_id: string
  question: string
  task_id: string
  session_id: string
  agent_id: string
  form: 'approval' | 'question' | 'wait' | ''
}

export const hitlApi = {
  pending: (sessionId: string) =>
    http.get<HitlPendingItem[]>(`/hitl/pending?session_id=${encodeURIComponent(sessionId)}`),
  answer: (hitlId: string, answer: string, llmAccount?: string | null, llmModel?: string | null) =>
    http.post<{ id: string; status: string }>(
      `/hitl/${hitlId}/answer`,
      // llm 字段只在显式给出时进 body(缺席=后端不动会话 LLM)
      llmAccount === undefined
        ? { answer }
        : { answer, llm_account: llmAccount, llm_model: llmModel ?? null },
    ),
  approve: (hitlId: string) =>
    http.post<{ id: string; status: string }>(`/hitl/${hitlId}/approve`, {}),
  reject: (hitlId: string, message = '') =>
    http.post<{ id: string; status: string }>(`/hitl/${hitlId}/reject`, { message }),
}
```

- [ ] **Step 2: SSE 类型与透传**

`useSessionSSE.ts`：`ChatWaitingInput`（42-53 行）增加两个可选字段：

```ts
  hitl_id?: string                  // 精确应答用;旧 snapshot 补发的事件可能没有 → 兜底链
  form?: 'approval' | 'question' | 'wait'
```

waiting_input 处理（374 行的 item 构造对象里）追加：

```ts
hitl_id: (data.hitl_id as string) || undefined, form: (data.form as ChatWaitingInput['form']) || undefined,
```

- [ ] **Step 3: ChatPanel mutations + 分流**

a. import 区加：`import { hitlApi } from '@/api/hitl'`（对齐该文件既有 `@/api` 别名风格；若现用相对路径则 `../api/hitl`）。

b. 组件外（模块级）加 helper：

```ts
// 解析应答目标:SSE 带的 hitl_id 优先;没有(旧 snapshot)→ 查 pending 按 form 匹配;
// wi=null(PAUSED 软待命,无面板)→ 取 form=wait 那条。查不到 → null(调用方兜底旧 /messages 通道)。
async function resolveHitlId(sessionId: string, wi: ChatWaitingInput | null): Promise<string | null> {
  if (wi?.hitl_id) return wi.hitl_id
  try {
    const pending = await hitlApi.pending(sessionId)
    if (!wi) return pending.find(p => p.form === 'wait')?.id ?? null
    const wantApproval = wi.hitl_kind === 'approval'
    return (pending.find(p => (p.form === 'approval') === wantApproval) ?? pending[0])?.id ?? null
  } catch {
    return null
  }
}
```

（`ChatWaitingInput` 需在此文件可见——它已从 `@/hooks/useSessionSSE` 导出/导入，沿用既有 import。）

c. `answerMut`（344-347 行）替换，并新增 `approveMut`/`rejectMut`/`waitReplyMut`：

```ts
  const answerMut = useMutation({
    // 面板应答走精确端点;拿不到 id/id 失效 → 兜底旧 /messages 通道(薄委托仍在)
    mutationFn: async (text: string) => {
      const id = await resolveHitlId(sessionId!, sse.waitingInput)
      if (id) {
        try { return await hitlApi.answer(id, text) } catch { /* fall through */ }
      }
      return sessionsApi.answerInput(sessionId!, text)
    },
    onSuccess: () => { qc.invalidateQueries({ queryKey: ['sessions'] }) },
  })
  const approveMut = useMutation({
    mutationFn: async () => {
      const id = await resolveHitlId(sessionId!, sse.waitingInput)
      if (id) {
        try { return await hitlApi.approve(id) } catch { /* fall through */ }
      }
      return sessionsApi.answerInput(sessionId!, 'approved')
    },
    onSuccess: () => { qc.invalidateQueries({ queryKey: ['sessions'] }) },
  })
  const rejectMut = useMutation({
    mutationFn: async () => {
      const id = await resolveHitlId(sessionId!, sse.waitingInput)
      if (id) {
        try { return await hitlApi.reject(id) } catch { /* fall through */ }
      }
      return sessionsApi.answerInput(sessionId!, 'rejected')
    },
    onSuccess: () => { qc.invalidateQueries({ queryKey: ['sessions'] }) },
  })
  const waitReplyMut = useMutation({
    // PAUSED 软待命(人为打断/纯文本暂停)回复:精确端点+携带当前模型选择(语义=旧 /messages);
    // 拿不到 id → 兜底 sendMessage(带 llm,走薄委托)
    mutationFn: async (text: string) => {
      const id = await resolveHitlId(sessionId!, null)
      if (id) {
        try { return await hitlApi.answer(id, text, nextProvider || null, nextModel || null) } catch { /* fall through */ }
      }
      return sessionsApi.sendMessage(sessionId!, text, nextProvider || null, nextModel || null)
    },
    onSuccess: () => { qc.invalidateQueries({ queryKey: ['sessions'] }); composerRef.current?.clear(); sse.reconnect() },
  })
```

d. submit 分流（389-392 行）改为（若 `Session['status']` 联合类型缺 `'PAUSED'` 字面量，先在 `frontend-desktop/src/types/index.ts` 的状态联合里补上——后端本就会发该状态）：

```ts
    if (isWaiting && sse.waitingInput) {
      answerMut.mutate(text)
      return
    }
    if (session?.status === 'PAUSED') {
      waitReplyMut.mutate(text)
      return
    }
```

e. 面板回调（609-614 行）改为：

```tsx
            <WaitingInputPanel
              item={waitingItem}
              onAnswer={(t) => answerMut.mutate(t)}
              onApprove={() => approveMut.mutate()}
              onReject={() => rejectMut.mutate()}
            />
```

- [ ] **Step 4: 构建验证**

Run: `cd frontend-desktop && npm run build`
Expected: tsc+vite 构建通过，无类型错误。
Run: `cd frontend-desktop && npx vitest run`（若脚本存在则 `npm test`）
Expected: 既有单测全绿（本任务未动纯函数面）。

- [ ] **Step 5: Commit**

```bash
git add frontend-desktop/src/
git commit -m "feat(desktop): HITL 应答改走 /hitl 精确端点(hitl_id 优先+pending 匹配+旧通道兜底)"
```

---

### Task 6: 老 frontend/ 改走精确接口

**Files:**
- Create: `frontend/src/api/hitl.ts`
- Modify: `frontend/src/hooks/useSessionSSE.ts:60-75`（`ChatWaitingInput` 定义区）、`:737-749`（waiting_input 处理）
- Modify: `frontend/src/components/chat/ChatPanel.tsx:854-962`（`BashExecConfirmArea`/`WaitingInputArea` 的 mutation）

**Interfaces:**
- Consumes: Task 2 端点、Task 4 SSE 字段；既有 `sessionsApi.sendMessage`（兜底）。
- 注：老前端 PAUSED 软待命回复走普通 composer → `sendMessage`（`/messages` 薄委托），**刻意不迁**——遗留 UI 只迁面板路径，成本收益比。

- [ ] **Step 1: 新建 `frontend/src/api/hitl.ts`**

内容与 Task 5 Step 1 的 `frontend-desktop/src/api/hitl.ts` 完全相同（同为 `import { http } from './client'`）。

- [ ] **Step 2: SSE 类型与透传**

`frontend/src/hooks/useSessionSSE.ts`：本文件内定义的 `ChatWaitingInput`（`kind: 'waiting_input'` 那个 interface，约 60-70 行）增加：

```ts
  hitl_id?: string
  form?: 'approval' | 'question' | 'wait'
```

waiting_input 处理（738-746 行的 item 构造）追加两行字段：

```ts
          hitl_id: (data.hitl_id as string) || undefined,
          form: (data.form as ChatWaitingInput['form']) || undefined,
```

- [ ] **Step 3: ChatPanel 面板 mutation 迁移**

`frontend/src/components/chat/ChatPanel.tsx`：

a. import 区加 `import { hitlApi } from '@/api/hitl'`（对齐该文件 `sessionsApi` 的 import 风格）。

b. 模块级加 helper（老前端类型无 `hitl_kind`，按 `form` 匹配）：

```ts
async function resolveHitlId(sessionId: string, wi: ChatWaitingInput): Promise<string | null> {
  if (wi.hitl_id) return wi.hitl_id
  try {
    const pending = await hitlApi.pending(sessionId)
    if (wi.form) return (pending.find(p => p.form === wi.form) ?? pending[0])?.id ?? null
    return pending[0]?.id ?? null
  } catch {
    return null
  }
}
```

c. `BashExecConfirmArea` 的 mutation（865-872 行）改为：

```ts
  const mutation = useMutation({
    // 审批走精确端点:approved→approve / 其余文本→reject(message);拿不到 id → 旧 /messages 兜底
    mutationFn: async (answer: string) => {
      const id = await resolveHitlId(sessionId, waitingInput)
      if (id) {
        try {
          if (answer === 'approved') return await hitlApi.approve(id)
          return await hitlApi.reject(id, answer === 'rejected' ? '' : answer)
        } catch { /* fall through */ }
      }
      return sessionsApi.sendMessage(sessionId, answer)
    },
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['sessions'] })
      setReason('')
      setRejecting(false)
    },
  })
```

d. `WaitingInputArea` 的 mutation（955-962 行）改为：

```ts
  const mutation = useMutation({
    // 提问/软待命面板应答走精确端点;拿不到 id → 旧 /messages 兜底
    mutationFn: async (content: string) => {
      const id = await resolveHitlId(sessionId, waitingInput)
      if (id) {
        try { return await hitlApi.answer(id, content) } catch { /* fall through */ }
      }
      return sessionsApi.sendMessage(sessionId, content)
    },
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['sessions'] })
      setText('')
    },
  })
```

- [ ] **Step 4: 构建验证**

Run: `cd frontend && npm run build`
Expected: 构建通过，无类型错误。

- [ ] **Step 5: Commit**

```bash
git add frontend/src/
git commit -m "feat(web): 老前端 HITL 面板应答改走 /hitl 精确端点(兜底旧通道)"
```

---

### Task 7: 全量验证 + 验收清单

**Files:** 无新改动（发现缺口回相应 Task 修）。

- [ ] **Step 1: host 全量测试**

Run: `uv run pytest tests/ tests/ -q`
Expected: 除 5 个 master 既有失败外全 PASS。

- [ ] **Step 2: 验收 grep（对照 spec 验收标准 1/5）**

```bash
# /hitl 端点与 /messages 无重复副作用逻辑:transcript 追加只在 hitl_service 出现
grep -rn "_append_json" src/ipmastercowork/api/ | grep -v hitl_service.py | grep -v models/
# 前端主路径已迁:answerInput/sendMessage 在 HITL 上下文只剩兜底(带 fall through/兜底注释)
grep -n "answerInput" frontend-desktop/src/components/ChatPanel.tsx
grep -n "sendMessage" frontend/src/components/chat/ChatPanel.tsx
```

Expected: 第一条仅剩 `sessions.py` 终态续聊分支的 transcript 调用（`send_message` 新 run 路径，非 HITL）；后两条命中处全部位于兜底分支。

- [ ] **Step 3: 端到端冒烟（dev 环境，三形态 + 打断 + 冷应答）**

启动后端 + frontend-desktop dev（或打包版）；逐项验证并在报告中记录：
1. ask_user（question）：面板答题 → Network 面板确认请求打到 `/hitl/{id}/answer`，会话续跑；
2. bash 审批（approval，manual 模式）：Approve/Reject 按钮 → `/hitl/{id}/approve|reject`；
3. 人为打断（wait）：打断后 composer 回复 → `/hitl/{id}/answer`（带 llm 字段），注入续跑；
4. 重启后端 → 对 PAUSED_HITL 会话应答（冷路径）：SSE 正常续流（consumer 重启生效）、workspace 正确；
5. 多 pending（如可构造：两个并行任务同时等审批）：面板应答命中所属那条。
无法构造的场景（如多 pending）如实记录跳过原因。

- [ ] **Step 4: 收尾**

调用 superpowers:finishing-a-development-branch（本特性与 hitl_id 重构同分支，一并处置）。
