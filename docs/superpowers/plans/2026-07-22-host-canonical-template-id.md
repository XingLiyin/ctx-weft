# host template_id 全链 canonical 化 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** host 记录层统一 `agent:` 规范形（新建即规范 + 存量行载入即规范），根治重启前后展示漂移；session_import 导入旧 dump 时按 m010 同口径规范化载荷，补上迁移够不到的导入路径。

**Architecture:** 依据 spec `docs/superpowers/specs/2026-07-22-host-canonical-template-id-display-design.md`。全部改动在 IpMasterCoworkPy 的三个点：`create_session` 求值后立即 canonical、`_entry_from_record` 读 DB 行时 canonical、`session_import` 落库前对事件/快照/config 载荷 canonical。引擎零改动，m010 保留。

**Tech Stack:** Python 3.11+ / `uv`。仓库 `C:\Users\Xing\Documents\codes\IpMasterCoworkPy`，分支 `feat/agent-capability-template-protocol`（延续）。

## Global Constraints

- **引擎零改动**（TemplateLookup / TemplateNotFoundError / 严格语义不动）；`canonical_template_id` 助手本身不动；m010 迁移保留。
- `canonical_template_id("")` 会产出 `"agent:"`——**空值必须在调用点守卫**（`if raw else ""` / truthiness 判断），本计划所有调用点都带守卫。
- 测试基线：host 1 个既有失败（`test_skills_reference.py`），验收 = 除它外全绿零新增。
- 测试命令：仓根 `uv run pytest <path> -q`。
- 提交纪律：`git status --short` 全检、逐文件点名 add、禁止 `git add -A` / `git add .`；信息结尾 `Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>`。

---

### Task 1: 三点 canonical 化 + 测试

**Files:**
- Modify: `src/ipmastercowork/api/sessions.py`（create_session 求值块 ~180-196）
- Modify: `src/ipmastercowork/api/models/session.py`（`_entry_from_record` ~944-949 + 模块 import）
- Modify: `src/ipmastercowork/observability/session_import.py`（新增 `_canonicalize_template_ids` + `import_session_db` 调用）
- Test: `tests/test_host_canonical_template_id.py`（新建）

**Interfaces:**
- Consumes: `canonical_template_id(template_id: str) -> str`（`ipmastercowork.providers.templates`，裸 id → `agent:<id>`，已含 `:` 幂等）；`import_session_db(sqlite_bytes, factory) -> str`；`_entry_from_record(rec) -> SessionEntry`
- Produces: 无新公开接口（`_canonicalize_template_ids(collected: dict) -> None` 为 session_import 模块私有）

- [ ] **Step 1: 写失败测试**——新建 `tests/test_host_canonical_template_id.py`：

```python
"""host template_id 全链 canonical 化（spec 2026-07-22 host-canonical-template-id-display）。

_entry_from_record：存量裸行载入即规范；session_import：旧 dump 载荷落库前规范化
（m010 已打标不重跑，导入路径须自行补）。
"""

from __future__ import annotations

import gzip
import json
from types import SimpleNamespace

from ipmastercowork.api.models.session import _entry_from_record
from ipmastercowork.observability.session_import import import_session_db
from ipmastercowork.persistence.postgres import init_db
from ipmastercowork.persistence.postgres.models import (
    Base, EventModel, SessionModel, SnapshotModel,
)


# ── _entry_from_record ───────────────────────────────────────────────────────

def _rec(template_id):
    return SimpleNamespace(
        id="ses_1", config={"template_id": template_id}, user_prompt="hi",
        tenant_id="default", llm_model="m", llm_provider="acc", status="FINISHED",
        goal=None, root_agent_id="agt_1", token_budget=0, failure_counter=0,
        workspace=None, created_at=None, updated_at=None,
    )


def test_entry_from_record_prefixes_bare_template_id():
    assert _entry_from_record(_rec("default")).template_id == "agent:default"


def test_entry_from_record_canonical_passthrough_and_empty():
    assert _entry_from_record(_rec("agent:default")).template_id == "agent:default"
    assert _entry_from_record(_rec("")).template_id == ""  # 空值不得变成 "agent:"


# ── session_import ───────────────────────────────────────────────────────────

def _make_dump(tmp_path) -> bytes:
    """最小旧版会话 dump：用真实模型建表（_read_dump 按 ORM 全列读，手写 DDL 会缺列），
    sessions/events/snapshots 三表带裸 template_id 载荷。"""
    from sqlalchemy import create_engine, insert

    db = tmp_path / "dump.sqlite"
    engine = create_engine(f"sqlite:///{db.as_posix()}")
    Base.metadata.create_all(engine)
    with engine.begin() as conn:
        conn.execute(insert(SessionModel.__table__).values(
            id="ses_old", user_prompt="hi", status="FINISHED", tenant_id="default",
            config_json=json.dumps({"template_id": "default"}),
        ))
        conn.execute(insert(EventModel.__table__).values(
            id="evt_01AAAAAAAAAAAAAAAAAAAAAAAA", session_id="ses_old", tenant_id="default",
            type="SESSION_CREATED", sequence=1,
            payload_json=json.dumps({"template_id": "default", "user_prompt": "hi"}),
            metadata_json="{}",
        ))
        conn.execute(insert(EventModel.__table__).values(
            id="evt_01AAAAAAAAAAAAAAAAAAAAAAAB", session_id="ses_old", tenant_id="default",
            type="SESSION_CREATED", sequence=2,
            payload_json=json.dumps({"template_id": "agent:default"}),  # 已规范 → 幂等不动
            metadata_json="{}",
        ))
        conn.execute(insert(SnapshotModel.__table__).values(
            id="snp_1", session_id="ses_old",
            last_event_id="evt_01AAAAAAAAAAAAAAAAAAAAAAAA", last_event_sequence=1,
            state_blob_json=json.dumps(
                {"sessions": {"ses_old": {"id": "ses_old", "template_id": "default"}}}),
        ))
    engine.dispose()
    return gzip.compress(db.read_bytes())


async def test_import_canonicalizes_payload_template_ids(tmp_path):
    factory = await init_db(f"sqlite:///{(tmp_path / 'live.db').as_posix()}")
    new_sid = await import_session_db(_make_dump(tmp_path), factory)

    from sqlalchemy import select
    async with factory() as db:
        payloads = [json.loads(p) for (p,) in (await db.execute(
            select(EventModel.payload_json).where(EventModel.session_id == new_sid)
        )).all()]
        blob = (await db.execute(
            select(SnapshotModel.state_blob_json).where(SnapshotModel.session_id == new_sid)
        )).scalar_one()

    tids = sorted(p["template_id"] for p in payloads)
    assert tids == ["agent:default", "agent:default"]  # 裸的被规范化，已规范的幂等
    state = json.loads(blob)
    assert all(s["template_id"] == "agent:default" for s in state["sessions"].values())
```

- [ ] **Step 2: 跑测试确认失败**

Run: `uv run pytest tests/test_host_canonical_template_id.py -q`
Expected: FAIL——entry 两个用例断言 `agent:default` 得到 `default`；import 用例断言规范化未发生。

- [ ] **Step 3: 实现三点**——

3a. `api/models/session.py`：模块 import 区加 `from ipmastercowork.providers.templates import canonical_template_id`；`_entry_from_record` 开头改为：

```python
    raw_tid = (rec.config or {}).get("template_id", "")
    # 存量 host 行是裸 id：载入即规范，与新建会话（create_session 已规范）展示一致。
    # 空值守卫：canonical_template_id("") 会产出 "agent:"，不可直包。
    template_id = canonical_template_id(raw_tid) if raw_tid else ""
```

（`SessionEntry(template_id=template_id, ...)` 不变。）

3b. `api/sessions.py` `create_session`：求值块之后（~188 行，`session_id` 生成之前）插入一行，并把 `SessionStartParams.create` 里的 `template_id=canonical_template_id(template_id)` 简化回 `template_id=template_id`：

```python
    # 求值即规范（spec 2026-07-22 host-canonical）：params 与 SessionEntry 共用同一
    # 规范值，host 记录从出生就是 agent:<id>，与重启后 _entry_from_record 载入形态一致。
    template_id = canonical_template_id(template_id)
```

（此处 `template_id` 经上方 if 块保证非空，无需守卫；终态→新轮 resume 路径的 `canonical_template_id(entry.template_id)` 包裹保留不动——entry 已规范时为幂等 no-op。）

3c. `observability/session_import.py`：在 `_rewrite_rows` 函数之后新增：

```python
def _canonicalize_template_ids(collected: dict) -> None:
    """升级前导出的旧 dump 载荷里是裸 template_id；m010 已打标不会重跑，导入路径须
    自行按同口径规范化，否则导入会话的引擎重放（崩溃恢复/冷 HITL/compact）报
    TemplateNotFoundError。覆盖 events.payload_json 顶层键、snapshots.state_blob_json
    的 sessions.*.template_id、sessions.config_json 顶层键。幂等：已含 ':' / 空值跳过。"""
    from ipmastercowork.providers.templates import canonical_template_id

    def _canon(tid):
        return canonical_template_id(tid) if isinstance(tid, str) and tid else tid

    def _rewrite_top_key(rows: list, col: str) -> None:
        for row in rows:
            raw = row.get(col)
            if not isinstance(raw, str) or '"template_id"' not in raw:
                continue
            try:
                obj = json.loads(raw)
            except ValueError:
                continue
            tid = obj.get("template_id")
            new = _canon(tid)
            if new != tid:
                obj["template_id"] = new
                row[col] = json.dumps(obj, ensure_ascii=False)

    _rewrite_top_key(collected.get("events", []), "payload_json")
    _rewrite_top_key(collected.get("sessions", []), "config_json")

    for row in collected.get("snapshots", []):
        raw = row.get("state_blob_json")
        if not isinstance(raw, str) or '"template_id"' not in raw:
            continue
        try:
            state = json.loads(raw)
        except ValueError:
            continue
        dirty = False
        for sess in (state.get("sessions") or {}).values():
            tid = sess.get("template_id")
            new = _canon(tid)
            if new != tid:
                sess["template_id"] = new
                dirty = True
        if dirty:
            row["state_blob_json"] = json.dumps(state, ensure_ascii=False)
```

`import_session_db` 中 `_rewrite_rows(collected, id_map)` 之后加一行调用：

```python
    _canonicalize_template_ids(collected)
```

- [ ] **Step 4: 跑新测试 + 相关回归**

Run: `uv run pytest tests/test_host_canonical_template_id.py -q`
Expected: PASS（4 用例）。
Run: `uv run pytest tests/test_session_resumed_projection.py tests/test_recover_active_after_resume.py -q -x`（`_entry_from_record` 消费方回归；若文件名有出入以 `Grep pattern="_entry_from_record" path=tests` 实际命中为准）
Expected: PASS。

- [ ] **Step 5: 存量 create_session HTTP 测试补断言（有则加，无则跳过）**

`Grep pattern="create_session|post(.*sessions" path=tests -i` 找现有走 HTTP/函数级 create 路径且能拿到 SessionEntry 的测试；若存在，就近加一行 `assert entry.template_id.startswith("agent:")`（或对返回体断言）。找不到合适挂点则跳过——`SessionEntry` 规范化已由 diff 直读 + `_entry_from_record` 用例夹住。

- [ ] **Step 6: 全量回归**

Run: `uv run pytest -q`
Expected: 除基线 1 个（test_skills_reference）外全绿、零新增。

- [ ] **Step 7: 提交**

```bash
git add src/ipmastercowork/api/sessions.py src/ipmastercowork/api/models/session.py src/ipmastercowork/observability/session_import.py tests/test_host_canonical_template_id.py
git commit -m "feat(host): template_id 全链 canonical 化——展示漂移根治 + session_import 补洞"
```

（若 Step 5 改了存量测试文件，一并点名 add。）
