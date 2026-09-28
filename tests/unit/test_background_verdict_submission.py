"""S5：后台 observe 在 `plain_text` 边界产 verdict 并提交带外入口。

这是整个改造的**第一个行为切换点**：root task 说完一段纯文本 park 之后，不再无限期停
着——observer 判 success 就终结并放行 DAG 后继，判 retry/fail 则维持 park 等人。

三条不变量在这里守住：
- **只有 `plain_text` 判定**。其余边界的判定已由别处给出（`mechanical` 刚机械判过、
  close 边界是 actor 自己宣布的），后台再判一次会把那份判决覆盖掉。
- **verdict 缺失 ≡ retry**。拿不到判决就什么都不提交，task 维持 park。默认态是 park，
  只有 success 触发状态转移——绝不因为观察失败而静默放行后继。
- **免折不免判**。短段仍跑判定（短回合恰恰是提问最典型的形态），只是不折。
- **park 气泡只收交出去了的那条线**。判 success 终结 task 时：子任务的气泡跟着收（线交回
  parent，没人会来答它），`parent_task_id is None` 的留着（没有别的线接管，用户下一句还是
  给它）——见下方那一节。
"""

from __future__ import annotations

from types import SimpleNamespace

from ctx_weft.core.capabilities.control_tools import ControlMetaKey as K
from ctx_weft.core.loop.steps.background_observe import (
    _judges,
    _submit_verdict,
)


class _RecordingTM:
    """带外入口的替身：**只记下收到了什么，不代替真品去调 `finalize`**。

    分层是刻意的。「什么时候调收尾」是带外入口自己的契约（只在判决被接受且是 `success`
    时，且跑在「状态已写定」与「转移已宣布」之间），那三条约束钉在
    `test_out_of_band_verdict.py` 里、打的是**真** `TaskManager`——替身复述一遍只会让两处
    对同一件事各有一份说法。本文件管的是另一半：传了什么进去、那个回调自己做了什么。

    曾经让这个替身照契约调过一次 `finalize`，于是它必须背上 memory / llm / event_bus 一整套
    替身，而气泡规则本身反倒读不清了。
    """

    def __init__(self, accepted: bool = True) -> None:
        self.calls: list[tuple] = []
        self._accepted = accepted

    async def apply_out_of_band_verdict(self, task_id, outcome, **kw):
        self.calls.append((task_id, outcome, kw))
        return self._accepted


def _state():
    return SimpleNamespace(task=SimpleNamespace(id="t1", outputs="交付物"))


def _ctx(tm):
    return SimpleNamespace(task_manager=tm)


_ctx_with_tm = _ctx


def _meta(outcome: str = "success", **kw) -> dict:
    base = {
        K.OBSERVER_OUTCOME: outcome,
        K.OBSERVER_ACT_RECAP: "做了甲乙丙",
        K.OBSERVER_TASK_SUMMARY: "全程小结",
        K.OBSERVER_NEXT_STEP_HINT: "",
        K.OBSERVER_FAILURE_REASON: "",
    }
    base.update(kw)
    return base


# ── 只有 plain_text 判定 ──────────────────────────────────────────────────────

def test_only_plain_text_boundary_judges() -> None:
    assert _judges("plain_text") is True
    for boundary in ("mechanical", "finish", "normal", "interrupt", "dispatch"):
        assert _judges(boundary) is False, f"{boundary} 不该产 verdict"


# ── 提交 ──────────────────────────────────────────────────────────────────────

async def test_success_verdict_is_submitted_with_every_field() -> None:
    tm = _RecordingTM()
    await _submit_verdict(_state(), _ctx(tm), _meta(
        **{K.OBSERVER_NEXT_STEP_HINT: "Next Step Hint: 下一步"}))

    assert len(tm.calls) == 1
    task_id, outcome, kw = tm.calls[0]
    assert task_id == "t1"
    assert outcome.verdict == "success"
    assert outcome.outputs == "交付物"
    assert kw["process_report"] == "做了甲乙丙"
    assert kw["task_summary"] == "全程小结"
    assert kw["next_step_hint"] == "Next Step Hint: 下一步"


async def test_retry_verdict_is_submitted_too() -> None:
    """retry 也提交——hint 要落地供下一轮用；「维持 park」是带外入口那边的分支。"""
    tm = _RecordingTM()
    await _submit_verdict(_state(), _ctx(tm), _meta(
        "retry", **{K.OBSERVER_FAILURE_REASON: "缺凭据"}))

    _tid, outcome, _kw = tm.calls[0]
    assert outcome.verdict == "retry"
    assert outcome.error == "缺凭据"


# ── verdict 缺失 ≡ retry（什么都不提交，维持 park）────────────────────────────

async def test_missing_verdict_submits_nothing() -> None:
    tm = _RecordingTM()
    await _submit_verdict(_state(), _ctx(tm), _meta(outcome=""))
    assert tm.calls == []


async def test_empty_metadata_submits_nothing() -> None:
    """LLM 没调 terminal tool → metadata 空 → 绝不静默放行后继。"""
    tm = _RecordingTM()
    await _submit_verdict(_state(), _ctx(tm), {})
    assert tm.calls == []


async def test_missing_task_manager_is_tolerated() -> None:
    """拿不到 TaskManager（手构 ctx / 已拆会话）：记日志，不炸。"""
    await _submit_verdict(_state(), SimpleNamespace(task_manager=None), _meta())


# ── 被拒绝不是错误 ────────────────────────────────────────────────────────────

async def test_rejected_verdict_is_not_an_error() -> None:
    """人先开口 → 仲裁拒绝。后台只记一笔，不抛、不重试。"""
    tm = _RecordingTM(accepted=False)
    await _submit_verdict(_state(), _ctx(tm), _meta())
    assert len(tm.calls) == 1


# ── park 气泡：只收交出去了的那条线（2026-09-27）────────────────────────────
#
# 判 success 终结 task 之后那个「等你说话」的气泡该不该收，取决于**这条线交回给谁了**：
#
# - **子任务**（`parent_task_id` 非空，含跨 agent）→ 线交回 parent，`_try_resume_parent`
#   随即接管，再没有人会来答这个气泡 → 收。不收的后果实测过：宿主折会话状态时气泡优先于
#   task 终态（`SessionStatusFold.status` 里 `pending_hitl` 排在 `terminal` 之前），于是
#   父任务收尾之后**会话永久停在 PAUSED**，SSE 的终态收口不触发，每个这样的子任务留一个。
# - **`parent_task_id is None`**（会话 root / 每条用户消息新开的 task）→ 没有别的线接管，
#   用户下一句还是给它 → 留。收掉它的后果同样实测过（`2ee524b` → `faedd25`）：root 的
#   `TaskFinished` 会写 `_terminal`，气泡一收 SUCCEEDED 当场浮出来，会话在用户正要打字的
#   那一刻跳成「已完成」。它由用户真的开口时收口（Runtime 的两条投递分支）。
#
# 判据是 `parent_task_id`，**不是 `_is_own_root`**——后者对跨 agent 子任务返回 True，会把
# 整类漏掉。那一格下面单独钉了一条。
#
# 这一节直接测 `_close_park_bubble`：它是气泡规则的所在，而它的兄弟（带外 finalize 的
# memory 那一半）要一整套 memory/llm 替身，混在一个用例里只会让规则本身读不清。两者怎么
# 组装、由谁调，钉在下一节。

from ctx_weft.core.loop.steps.background_observe import (  # noqa: E402
    _close_park_bubble,
    _out_of_band_finalize,
)
from ctx_weft.protocols.hitl import ToolResultDelivery, UserTurnDelivery  # noqa: E402


class _Bubble:
    def __init__(self, hid: str, task_id: str, delivery=None) -> None:
        self.id, self.task_id = hid, task_id
        self.delivery = delivery if delivery is not None else UserTurnDelivery(task_id=task_id)


class _Registry:
    def __init__(self, bubbles: list) -> None:
        self._bubbles = bubbles

    def list_pending(self, session_id=None, **kw):
        return list(self._bubbles)


class _Hitl:
    def __init__(self, bubbles: list) -> None:
        self.registry = _Registry(bubbles)
        self.cancelled: list[str] = []

    async def cancel(self, hitl_id, *, message="", defer=False):
        self.cancelled.append(hitl_id)
        return None


def _ctx_with_hitl(hitl):
    return SimpleNamespace(hitl=hitl)


def _state_with_session(*, parent: str | None = None, creator="ag_1", assigned="ag_1"):
    return SimpleNamespace(
        task=SimpleNamespace(id="t1", outputs="交付物", parent_task_id=parent,
                             creator_agent_id=creator, assigned_agent_id=assigned),
        session=SimpleNamespace(id="s1"),
    )


# ── 子任务：收 ────────────────────────────────────────────────────────────────

async def test_a_subtask_bubble_is_closed() -> None:
    """**要害**：线交回 parent 了，这个气泡再没有人会来答——留着会把会话钉死在 PAUSED。"""
    hitl = _Hitl([_Bubble("hit_1", "t1")])
    await _close_park_bubble(_state_with_session(parent="t0"), _ctx_with_hitl(hitl))
    assert hitl.cancelled == ["hit_1"]


async def test_a_cross_agent_subtask_bubble_is_closed_too() -> None:
    """**判据不能写 `_is_own_root`**：它对跨 agent 子任务（parent 非空、creator≠assigned）
    返回 True，照它过滤会把整类漏掉——而这类任务的线同样是交回 parent 的。"""
    hitl = _Hitl([_Bubble("hit_1", "t1")])
    await _close_park_bubble(
        _state_with_session(parent="t0", creator="ag_1", assigned="ag_2"), _ctx_with_hitl(hitl))
    assert hitl.cancelled == ["hit_1"]


# ── root：留 ──────────────────────────────────────────────────────────────────

async def test_a_root_bubble_is_left_alone() -> None:
    """`faedd25` 保下来的那条：root 的 `TaskFinished` 会写 `_terminal`，气泡一收
    SUCCEEDED 当场浮出来，会话在用户正要打字的那一刻跳成「已完成」。"""
    hitl = _Hitl([_Bubble("hit_1", "t1")])
    await _close_park_bubble(_state_with_session(parent=None), _ctx_with_hitl(hitl))
    assert hitl.cancelled == []


# ── 过滤 ──────────────────────────────────────────────────────────────────────

async def test_only_this_task_and_only_user_turn_bubbles() -> None:
    """双重过滤：兄弟任务的气泡不归这次判决管；同一 task 上「等人拍板」那一档也不归——
    它有自己的处置路径，本函数只认「等人开口」。"""
    hitl = _Hitl([
        _Bubble("hit_mine", "t1"),
        _Bubble("hit_sibling", "t2"),
        _Bubble("hit_approval", "t1", delivery=ToolResultDelivery(tool_call_id="tc_1")),
    ])
    await _close_park_bubble(_state_with_session(parent="t0"), _ctx_with_hitl(hitl))
    assert hitl.cancelled == ["hit_mine"]


async def test_a_missing_hitl_service_is_tolerated() -> None:
    """手构 ctx 里没有 hitl（单测替身很常见）：静默跳过，不炸。"""
    await _close_park_bubble(_state_with_session(parent="t0"), SimpleNamespace(hitl=None))


# ── 收尾怎么组装、由谁调（2026-09-27）────────────────────────────────────────
#
# park 之后 `FinalizeStep` 压根没跑过（`HitlPark` 在 ActStep 里就把 run 结束了），而它是
# close/bubble、blackboard、TASK_FINALIZED 三者的唯一发生地。带外收尾把同一份实现照样跑
# 一遍——这一节只钉「调了什么、事件发出去了、谁来调它」，memory 那一半的语义归 finalize
# 自己的用例。

class _Bus:
    def __init__(self) -> None:
        self.emitted: list = []

    async def emit(self, event) -> None:
        self.emitted.append(event)


async def test_out_of_band_finalize_runs_close_and_emits_its_events(monkeypatch) -> None:
    """**要害**：带外收尾把 `apply_task_close` 的产物发出去，并补上 `TASK_FINALIZED`。

    不发 TASK_FINALIZED 的后果是 host 的 `tasks.outputs_json` 恒空——S5 起一直如此。
    """
    import ctx_weft.core.loop.steps.finalize as fin

    calls: dict = {}

    async def _fake_close(state, task, ctx, **kw):
        calls["close"] = kw
        return ["close-event"]

    monkeypatch.setattr(fin, "apply_task_close", _fake_close)
    monkeypatch.setattr(fin, "task_finalized_event",
                        lambda state, task, *, outcome: f"finalized:{outcome}")

    bus = _Bus()
    state = _state_with_session(parent="t0")
    ctx = SimpleNamespace(hitl=_Hitl([]), event_bus=bus)
    await _out_of_band_finalize(state, ctx, _meta())

    assert bus.emitted == ["close-event", "finalized:success"]
    assert calls["close"]["outcome"] == "success"
    assert calls["close"]["act_recap"] == "做了甲乙丙"
    assert calls["close"]["task_summary"] == "全程小结"
    # 判决方就是 LLM，摘要是真的 → close 即折末段 raw，不走占位-替换那条延迟路径。
    assert calls["close"]["has_llm_summary"] is True


async def test_out_of_band_finalize_also_closes_the_bubble(monkeypatch) -> None:
    """收尾与收气泡是同一次带外动作的两半，别让将来有人只接一半。"""
    import ctx_weft.core.loop.steps.finalize as fin

    async def _fake_close(state, task, ctx, **kw):
        return []

    monkeypatch.setattr(fin, "apply_task_close", _fake_close)
    monkeypatch.setattr(fin, "task_finalized_event", lambda *a, **kw: "finalized")

    hitl = _Hitl([_Bubble("hit_1", "t1")])
    ctx = SimpleNamespace(hitl=hitl, event_bus=_Bus())
    await _out_of_band_finalize(_state_with_session(parent="t0"), ctx, _meta())
    assert hitl.cancelled == ["hit_1"]


async def test_the_verdict_entry_is_handed_the_finalize_callback() -> None:
    """`_submit_verdict` 必须把收尾交给带外入口，而不是自己就地跑。

    位置是要害：带外入口在「状态已写定」与「转移已宣布」之间调它（见
    `apply_out_of_band_verdict` 的三条约束）。自己就地跑 = 跑在 `_settle` 之后，parent 可能
    已经醒过来装配、而子任务的产出还没落地。
    """
    tm = _RecordingTM()
    await _submit_verdict(_state_with_session(parent="t0"), _ctx_with_tm(tm), _meta())
    _tid, _outcome, kw = tm.calls[0]
    assert callable(kw["finalize"])
