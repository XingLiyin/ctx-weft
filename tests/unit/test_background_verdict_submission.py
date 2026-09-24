"""S5：后台 observe 在 `plain_text` 边界产 verdict 并提交带外入口。

这是整个改造的**第一个行为切换点**：root task 说完一段纯文本 park 之后，不再无限期停
着——observer 判 success 就终结并放行 DAG 后继，判 retry/fail 则维持 park 等人。

三条不变量在这里守住：
- **只有 `plain_text` 判定**。其余边界的判定已由别处给出（`mechanical` 刚机械判过、
  close 边界是 actor 自己宣布的），后台再判一次会把那份判决覆盖掉。
- **verdict 缺失 ≡ retry**。拿不到判决就什么都不提交，task 维持 park。默认态是 park，
  只有 success 触发状态转移——绝不因为观察失败而静默放行后继。
- **免折不免判**。短段仍跑判定（短回合恰恰是提问最典型的形态），只是不折。
"""

from __future__ import annotations

from types import SimpleNamespace

from ctx_weft.core.capabilities.control_tools import ControlMetaKey as K
from ctx_weft.core.loop.steps.background_observe import (
    _judges,
    _submit_verdict,
)


class _RecordingTM:
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


# ── park 气泡的收口（2026-09-24 线上 bug）────────────────────────────────────
#
# 不收的后果：气泡还挂着 → 前端认为「agent 在等你说话」→ 把用户的下一条输入当成对这个
# 气泡的应答投过来 → 消息注进已 FINISHED 的 task、重排被终态守卫挡掉 → 死在那里。
# 用户看到的是「发了没反应，再发一遍才回」。

class _Bubble:
    def __init__(self, hid: str, task_id: str) -> None:
        self.id, self.task_id = hid, task_id


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


def _ctx_with_hitl(tm, hitl):
    return SimpleNamespace(task_manager=tm, hitl=hitl)


def _state_with_session():
    return SimpleNamespace(task=SimpleNamespace(id="t1", outputs="交付物"),
                           session=SimpleNamespace(id="s1"))


async def test_success_closes_the_park_bubble() -> None:
    hitl = _Hitl([_Bubble("hit_1", "t1")])
    await _submit_verdict(_state_with_session(), _ctx_with_hitl(_RecordingTM(), hitl), _meta())
    assert hitl.cancelled == ["hit_1"]


async def test_retry_leaves_the_bubble_alone() -> None:
    """retry 维持 park——那个气泡正是它等人的入口，收掉会把会话变哑。"""
    hitl = _Hitl([_Bubble("hit_1", "t1")])
    await _submit_verdict(_state_with_session(), _ctx_with_hitl(_RecordingTM(), hitl),
                          _meta("retry"))
    assert hitl.cancelled == []


async def test_rejected_verdict_leaves_the_bubble_alone() -> None:
    """判决被仲裁拒绝（人先开口）→ task 没终结，气泡照旧归它用。"""
    hitl = _Hitl([_Bubble("hit_1", "t1")])
    await _submit_verdict(_state_with_session(),
                          _ctx_with_hitl(_RecordingTM(accepted=False), hitl), _meta())
    assert hitl.cancelled == []


async def test_only_this_tasks_bubbles_are_closed() -> None:
    """同 agent 上别的请求（例如子任务的 ask_user）不归这次判决管。"""
    hitl = _Hitl([_Bubble("hit_mine", "t1"), _Bubble("hit_other", "t2")])
    await _submit_verdict(_state_with_session(), _ctx_with_hitl(_RecordingTM(), hitl), _meta())
    assert hitl.cancelled == ["hit_mine"]


async def test_cancel_failure_does_not_break_the_verdict() -> None:
    """收不掉气泡只记日志——绝不让它把一次已经落定的判决变成异常。"""
    class _Boom(_Hitl):
        async def cancel(self, hitl_id, **kw):
            raise RuntimeError("registry is cold")

    await _submit_verdict(_state_with_session(),
                          _ctx_with_hitl(_RecordingTM(), _Boom([_Bubble("h", "t1")])), _meta())


async def test_missing_hitl_service_is_tolerated() -> None:
    await _submit_verdict(_state_with_session(),
                          SimpleNamespace(task_manager=_RecordingTM(), hitl=None), _meta())
