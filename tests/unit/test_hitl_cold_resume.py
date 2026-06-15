"""HitlManager 冷应答自触发 session resume（spec/07 §6/§9）。

热/冷分流在 core 内闭环,不外泄给 host：
  - 冷应答（Future 已驱逐 / 重启后无 future）→ 触发 on_cold_resolve(session_id)，让该 session
    reconcile 续跑。answer / approve / reject 三种应答都如此。
  - 热应答（Future 仍存活）→ set Future 就地续跑,绝不 resume。
  - cancel 是终态、不 requeue → 绝不 resume（即便冷）。
"""

from __future__ import annotations

from loomex_core.core.control.types import HitlRequestView
from loomex_core.core.orchestrator.hitl_manager import HitlManager


class _Recorder:
    """on_cold_resolve 回调收到**已解决的 HitlRequest**；这里只记其 session_id 方便断言。"""

    def __init__(self) -> None:
        self.calls: list[str] = []

    async def __call__(self, req) -> None:
        self.calls.append(req.session_id)


def _cold_mgr(rec: _Recorder, *, kind: str = "input", session_id: str = "s1") -> HitlManager:
    """重启后状态：rebuild_pending 重建 pending 但不建 future → 应答必走冷。"""
    mgr = HitlManager(on_cold_resolve=rec)
    mgr.rebuild_pending({"hit1": HitlRequestView(id="hit1", kind=kind, session_id=session_id, task_id="t1")})
    return mgr


async def test_cold_answer_resumes() -> None:
    rec = _Recorder()
    mgr = _cold_mgr(rec, kind="input")
    req = await mgr.answer("hit1", "use postgres")
    assert req.status == "accepted"
    assert rec.calls == ["s1"]


async def test_cold_approve_resumes() -> None:
    rec = _Recorder()
    mgr = _cold_mgr(rec, kind="approval")
    await mgr.approve("hit1")
    assert rec.calls == ["s1"]


async def test_cold_reject_resumes() -> None:
    rec = _Recorder()
    mgr = _cold_mgr(rec, kind="approval")
    await mgr.reject("hit1", message="no")
    assert rec.calls == ["s1"]


async def test_hot_answer_does_not_resume() -> None:
    rec = _Recorder()
    mgr = HitlManager(on_cold_resolve=rec)
    rid = await mgr.request(kind="input", session_id="s1", task_id="t1", tool_call_id="tc1")  # 建 future → 热
    await mgr.answer(rid, "hi")
    assert rec.calls == []


async def test_cancel_never_resumes() -> None:
    rec = _Recorder()
    mgr = _cold_mgr(rec, kind="input")
    await mgr.cancel("hit1")
    assert mgr.get("hit1").status == "cancelled"
    assert rec.calls == []                       # 终态,不 requeue,不 resume


async def test_resolved_idempotent_does_not_resume_twice() -> None:
    rec = _Recorder()
    mgr = _cold_mgr(rec, kind="input")
    await mgr.answer("hit1", "first")
    await mgr.answer("hit1", "second")           # 已 accepted → 幂等 no-op
    assert rec.calls == ["s1"]                    # 只 resume 一次


async def test_setter_binds_handler_late() -> None:
    rec = _Recorder()
    mgr = HitlManager()                           # 无回调构造
    mgr.set_cold_resolve_handler(rec)            # 晚绑定（Runtime 绑 recover_session 的方式）
    mgr.rebuild_pending({"hit1": HitlRequestView(id="hit1", kind="input", session_id="s9", task_id="t1")})
    await mgr.answer("hit1", "x")
    assert rec.calls == ["s9"]
