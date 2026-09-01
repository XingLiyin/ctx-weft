# HITL 重设计 · 段 3（收口）实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** host 应答端点收敛为一个命令，并在退役闸门核对通过后删除双读折叠。

**Architecture:** core 侧不再变动。段 3 只做两件事：把 host 的三个动作端点转译成一个 `HitlReply` 命令（core 早已只认这一个入口），以及在确认「升级前产生的未决请求已全部终局」后，删掉只为跨版本存活而写的 legacy 折叠分支。

**Tech Stack:** Python 3.11+、pytest（`asyncio_mode = "auto"`）、ruff（line-length 100）。

**Spec:** `docs/superpowers/specs/2026-09-01-hitl-redesign-design.md`（§8 host API、§12.3.6 退役闸门）

**前置：** 段 2 已完成——`reply_to_hitl(HitlReply)` 是 core 侧唯一应答入口，旧 `HitlManager` 已删除。

## Global Constraints

- **Task 2 是有条件的。** 双读折叠只有在闸门核对通过后才能删。核对不过就**停在 Task 1**，把结论写进报告——留着的成本只是一份纯函数，删早了的代价是在途请求恢复不出来。
- host 兼容层可长期保留，不必与双读同期退役（成本近似为零）。
- 关键词路由（首词 approve/reject）留在 host 侧，core 不做。且**只对 approval 语义的 form 适用**——`question`/`wait` 的文字回复一律是答复，`"no"` 是一个否定答复而非拒绝。
- 既有测试的验收口径仍是「不新增失败」（本仓已有 3 条既有失败，见段 1/段 2 计划）。

---

## Task 1: host 应答端点收敛为一个命令

**Files:**
- Modify: host 的 HITL 路由模块（`grep -rn "hitl/{.*}/approve\|/answer\|/reject" --include=*.py .` 定位；若 host 在本仓外，本任务改为产出一份升级须知，见 Step 5）
- Test: `tests/unit/test_hitl_reply_endpoint.py`

**Interfaces:**
- Consumes: 段 2 的 `CtxWeftRuntime.reply_to_hitl(reply: HitlReply) -> HitlRequestView | None`
- Produces: `POST /hitl/{id}/reply` 接受 `HitlReply`；三个旧端点转为薄转译层

- [ ] **Step 1: 写失败的测试**

```python
"""host 应答端点：一个命令 + 三个旧端点的转译（段 3 · Task 1）。"""

from __future__ import annotations


async def test_reply_endpoint_accepts_outcome_and_payload_directly():
    rt = _runtime()
    hitl_id = await _open_approval(rt, tool_call_id="call_1")
    resp = await _post(f"/hitl/{hitl_id}/reply", {
        "outcome": "accepted", "message": "ok",
        "modified_arguments": {"command": "ls -l"}})
    assert resp.status == 200 and resp.json()["outcome"] == "accepted"


async def test_host_defined_outcome_needs_no_new_endpoint():
    """开放 outcome 的直接收益：host 自定义结局不必为每个值加一个端点。"""
    rt = _runtime()
    hitl_id = await _open_approval(rt, tool_call_id="call_1")
    resp = await _post(f"/hitl/{hitl_id}/reply", {"outcome": "escalated",
                                                  "message": "转风控组"})
    assert resp.status == 200 and resp.json()["outcome"] == "escalated"


async def test_legacy_approve_translates_to_an_accepted_reply():
    rt = _runtime()
    hitl_id = await _open_approval(rt, tool_call_id="call_1")
    await _post(f"/hitl/{hitl_id}/approve", {"message": "ok",
                                             "modified_arguments": {"x": 1}})
    view = rt.hitl_registry.get(hitl_id).to_view()
    assert view.outcome == "accepted"


async def test_legacy_answer_translates_to_an_accepted_reply_with_the_text():
    rt = _runtime()
    hitl_id = await _open_question(rt, tool_call_id="call_1")
    await _post(f"/hitl/{hitl_id}/answer", {"text": "小明"})
    decision = rt.hitl_registry.get(hitl_id).decision
    assert decision.outcome == "accepted" and decision.message == "小明"


async def test_legacy_reject_translates_to_a_rejected_reply():
    rt = _runtime()
    hitl_id = await _open_approval(rt, tool_call_id="call_1")
    await _post(f"/hitl/{hitl_id}/reject", {"message": "先列目录"})
    assert rt.hitl_registry.get(hitl_id).decision.outcome == "rejected"


async def test_replying_to_an_already_resolved_request_is_a_noop():
    rt = _runtime()
    hitl_id = await _open_approval(rt, tool_call_id="call_1")
    await _post(f"/hitl/{hitl_id}/reply", {"outcome": "accepted"})
    resp = await _post(f"/hitl/{hitl_id}/reply", {"outcome": "rejected"})
    assert resp.status in (200, 409)
    assert rt.hitl_registry.get(hitl_id).decision.outcome == "accepted"


async def test_pending_listing_exposes_the_view_without_the_idempotency_key():
    """tool_call_id 是 core 的幂等键，host 不需要（spec §4）。"""
    rt = _runtime()
    await _open_approval(rt, tool_call_id="call_1")
    row = (await _get("/hitl/pending")).json()[0]
    assert "form" in row and "prompt" in row
    assert "tool_call_id" not in row


async def test_keyword_routing_applies_only_to_approval_forms():
    """「no」对 question/wait 是一个否定**答复**，不是拒绝（spec §8）。"""
    rt = _runtime()
    q = await _open_question(rt, tool_call_id="call_1")
    await _post_freetext(q, "no")
    assert rt.hitl_registry.get(q).decision.outcome == "accepted"

    a = await _open_approval(rt, tool_call_id="call_2")
    await _post_freetext(a, "no, 先列目录")
    d = rt.hitl_registry.get(a).decision
    assert d.outcome == "rejected" and d.message == "先列目录"
```

- [ ] **Step 2: 跑测试确认失败**

Run: `uv run pytest tests/unit/test_hitl_reply_endpoint.py -v`
Expected: FAIL —— `/reply` 端点不存在

- [ ] **Step 3: 实现新端点**

```python
@router.post("/hitl/{hitl_id}/reply")
async def reply(hitl_id: str, body: ReplyBody) -> dict:
    """唯一应答入口。三个旧端点是它的转译层（见下）。

    合成一个命令的收益：host 自定义 outcome（硬需求之一）不必为每个新值加端点——
    旧的 approve/answer/reject 三分本质是三个「动作」，而结局其实是 outcome + 载荷。
    """
    view = await runtime.reply_to_hitl(HitlReply(
        hitl_id=hitl_id,
        outcome=body.outcome,
        message=body.message,
        modified_arguments=body.modified_arguments,
        resume_hint=ResumeHint(llm_account=body.llm_account, llm_model=body.llm_model),
    ))
    if view is None:
        raise HTTPException(409, "HITL request already resolved")
    return _view_to_json(view)
```

- [ ] **Step 4: 三个旧端点改为薄转译**

```python
@router.post("/hitl/{hitl_id}/approve")
async def approve(hitl_id: str, body: ApproveBody) -> dict:
    """兼容层：成本近似为零，可长期保留，不必与双读同期退役。"""
    return await reply(hitl_id, ReplyBody(
        outcome=HITL_OUTCOME_ACCEPTED, message=body.message,
        modified_arguments=body.modified_arguments,
        llm_account=body.llm_account, llm_model=body.llm_model))


@router.post("/hitl/{hitl_id}/answer")
async def answer(hitl_id: str, body: AnswerBody) -> dict:
    return await reply(hitl_id, ReplyBody(
        outcome=HITL_OUTCOME_ACCEPTED, message=body.text,
        llm_account=body.llm_account, llm_model=body.llm_model))


@router.post("/hitl/{hitl_id}/reject")
async def reject(hitl_id: str, body: RejectBody) -> dict:
    return await reply(hitl_id, ReplyBody(
        outcome=HITL_OUTCOME_REJECTED, message=body.message,
        llm_account=body.llm_account, llm_model=body.llm_model))
```

自由文本回话的关键词路由（若 host 有此入口）：**按 form 判定**，`approval` 语义的 form 才
按首词分流；其余一律 `outcome=accepted, message=原文`。

- [ ] **Step 5: 若 host 在本仓之外**

不改代码，改为在 `docs/` 下产出一份升级须知，内容必须包含：新端点与请求体、三个旧端点的
等价转译表、`GET /hitl/pending` 返回体的字段变化（少了 `tool_call_id`，多了 `subject_id`/
`detail`/`fields`/`proposal`），以及**最重要的一条**：host 自写的 authorizer / 工具 provider
**必须改**（`await hitl_manager.wait()` 那套没有兼容路径，spec §12.3.5），改不了就不能升级。
这条要写在文档最前面，不能藏在附录里。

- [ ] **Step 6: 提交**

```bash
git add -A src tests docs
git commit -m "feat(hitl): host 应答端点收敛为一个 reply 命令，三个旧端点转为兼容层"
```

---

## Task 2: 退役闸门核对与双读删除（**有条件**）

**Files:**
- Modify: `src/ctx_weft/core/control/reducers.py`（删 legacy 分支）
- Modify: `tests/unit/test_hitl_fold_snapshot.py`（删 legacy 用例）
- Test: 全量

**Interfaces:**
- Consumes: 段 1 的 `fold_hitl_snapshot`
- Produces: 只认新两类事件的折叠

- [ ] **Step 1: 核对退役闸门**

spec §12.3.6 的条件——**两个信号都要可核对，缺一即停**：

1. 升级点之前产生的 `HitlRequired` 事件，**全部**有对应的终态事件（无悬挂未决）。
2. 这些 session 均已归档 / 超出最长存活期，不再需要回放到升级点之前的日志段。

给出核对命令并把**实际输出**贴进报告（不是"我认为通过"）：

```sql
-- 信号 1：升级点之前仍未终局的 HITL 请求
SELECT session_id, payload->>'hitl_id' AS hitl_id, timestamp
FROM events e
WHERE e.type = 'HitlRequired'
  AND e.timestamp < :cutover_ts
  AND NOT EXISTS (
    SELECT 1 FROM events r
    WHERE r.type IN ('HitlApproved','HitlModified','HitlAnswered',
                     'HitlRejected','HitlCancelled')
      AND r.payload->>'hitl_id' = e.payload->>'hitl_id')
ORDER BY e.timestamp;
```

**返回非空 ⟹ 闸门不通过。** 停在这里，把结果写进报告，不要继续 Step 2。留着双读的成本只是
一份纯函数；删早了的代价是那些在途请求恢复不出来——人已经答过的问题会被重新问，或者干脆
恢复不出决定。

- [ ] **Step 2: 闸门通过才做——删除 legacy 折叠分支**

从 `fold_hitl_snapshot` 删除 `HITL_REQUIRED` 与 `_HITL_RESOLVE_TYPES` 两个分支，连同
`_legacy_delivery` / `_legacy_decision` / `_LEGACY_PREFACE`；`HITL_FOLD_EVENT_TYPES` 收缩为
`(EventType.HITL_OPENED, EventType.HITL_RESOLVED)`。

同时删除 `tests/unit/test_hitl_fold_snapshot.py` 里全部 legacy 用例，保留新事件用例。

**顺带评估**（终审的 deferred #7 记在案）：双读一走，`fold_hitl_snapshot` 就只剩新模型逻辑，
此时把它从 `reducers.py` 挪到 `core/hitl/snapshot.py` 与 `HitlSnapshot` 同住是自然的——
`core.control` 也就不再需要 import `core.hitl`。**这一步可做可不做**，做了就在报告里说明；
不做则在 `reducers.py` 留一行注释指出这个位置是历史产物。

- [ ] **Step 3: 全量回归**

Run: `uv run pytest tests -q`
Expected: 无新增失败

- [ ] **Step 4: 事件类型退役（可选，需再一次闸门）**

`HitlRequired` / `HitlApproved` / `HitlModified` / `HitlAnswered` / `HitlRejected` /
`HitlCancelled` / `HitlTimeout` / `SessionPausedHitl` 这 8 个 `EventType` 成员，**在确认没有
任何历史日志需要被解读之后**才能从枚举里删除。这比 Step 1 的闸门更严格（它要求的是"没有任何
回放会碰到这些值"，而不只是"没有未决请求"），通常要等一整个归档周期。

**本任务不做这一步**，只在报告里记下它是最后一块尾巴，以及它的前置条件。

- [ ] **Step 5: 提交**

```bash
git add -A src tests
git commit -m "refactor(hitl): 退役闸门核对通过，删除双读折叠的 legacy 分支"
```

---

## 段 3 完成判据

- [ ] `POST /hitl/{id}/reply` 可用；三个旧端点仍工作（转译层）
- [ ] host 自定义 outcome 无需新增端点即可送达
- [ ] `GET /hitl/pending` 不暴露 `tool_call_id`
- [ ] 关键词路由只对 approval 语义的 form 生效
- [ ] 闸门核对结果（通过或不通过）**有实际查询输出**记录在案
- [ ] 若闸门通过：双读折叠已删除，全量测试无新增失败
- [ ] 若闸门未通过：双读保留，报告写明未通过的具体数据

---

## 三段做完之后，spec 里仍然悬着的东西

这些不是遗漏，是设计时明确记录的取舍与尾巴，留给下一次动 HITL 的人：

- **§12.1 delivery 封闭是会回来找我们的决定。** 出现第三种真正的回灌方式（例如「决定只改配置、
  不回灌给任何对话」）时要改 core。
- **§9.6 host 不能凭空发起 HITL。** 相邻需求（host 想打断在跑的 session 并插问一句）是
  `UserTurn` delivery 的 ask，机制现成，但入口属于 interrupt，本次三段都没做。
- **8 个 legacy 事件类型的最终退役**（段 3 Task 2 Step 4），需要一整个归档周期作前置。
- **`reducers.py` 的位置问题**：双读退役后 `fold_hitl_snapshot` 的自然归宿是
  `core/hitl/snapshot.py`。
