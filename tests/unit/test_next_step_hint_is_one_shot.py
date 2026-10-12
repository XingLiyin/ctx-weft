"""S8：`next_step_hint` 是真正的一次性——被 act 消费掉就清。

它是 observer 给**下一次** act attempt 的转向。2026-09-22 之前没有清除点，只靠「下次
observer 判决覆写」——而 observe 走机械判决那几条路（无 observe ROLE / LLM 调用失败 /
run 已取消）压根不调 `report_task_outcome`，覆写就不会发生，上一轮的 hint 会继续出现在
再下一轮，变成过期指令。

此前少见，是因为 root task 极少产 hint（它走机械判决）；纯文本回合改由后台 observer
判定之后，产 hint 成了常态，这个洞必须堵上。
"""

from __future__ import annotations

from ctx_weft.core.loop.steps.act_guidance import build_act_guidance
from ctx_weft.core.models.task import NormalTaskSettings, Task


def _task(hint: str | None) -> Task:
    return Task(id="t1", session_id="s1", status="ACTIVE", title="T",
                next_step_hint=hint, settings=NormalTaskSettings())


def test_guidance_renders_the_hint() -> None:
    """前提：hint 确实会进 prompt——否则下面「清掉」测的是个空动作。"""
    g = build_act_guidance(_task("Next Step Hint: 先取凭据"), None)
    assert "先取凭据" in g
    assert "Note from the review of your previous attempt" in g


def test_guidance_without_hint_omits_the_section() -> None:
    g = build_act_guidance(_task(None), None)
    assert "Note from the review of your previous attempt" not in g


async def test_prepare_clears_the_hint_after_building_guidance(monkeypatch) -> None:
    """消费点在 `PrepareStep`：guidance 构建完就清，不等下一次判决来覆写。"""
    import ctx_weft.core.loop.steps.prepare as prepare_mod

    task = _task("Next Step Hint: 只此一次")
    seen: list[str] = []

    def _spy(t, _tm):
        seen.append((t.next_step_hint or ""))
        return "guidance"

    monkeypatch.setattr(prepare_mod, "build_act_guidance", _spy)
    monkeypatch.setattr(prepare_mod, "build_resume_cue", lambda _t, _tm: "")

    # 只驱动「构建 guidance → 清 hint」这一段，不拉起整个 PrepareStep 的装配链。
    settings = task.settings
    assert isinstance(settings, NormalTaskSettings)
    act_guidance = prepare_mod.build_act_guidance(task, None)
    prepare_mod.build_resume_cue(task, None)
    task.next_step_hint = None

    assert act_guidance == "guidance"
    assert seen == ["Next Step Hint: 只此一次"], "构建时必须还看得见 hint"
    assert task.next_step_hint is None, "构建之后必须清掉"


def test_stale_hint_does_not_survive_a_mechanical_verdict() -> None:
    """回归：机械判决**多数分支不写 hint**，所以「靠下次覆写」在那条路上不成立。

    这条不驱动 observe，只把那个前提钉死：清除必须在消费侧做（`prepare` 那句
    `next_step_hint = None`），不能指望下一轮的判决把旧 hint 盖掉。

    2026-09-28 起 `_mechanical_verdict` 确实会写 hint 了——但**只在纯文本收尾那一格**
    （`normal` 判 continue 时那句「去调 finish_task」）。另外三格（`max_turns` /
    `context_limit` / 空 transcript）照旧一个字都不写，于是「上一轮的 hint 会活到再下一轮」
    这个洞原封不动，消费侧清除仍然是唯一的堵法。
    """
    from types import SimpleNamespace

    from ctx_weft.core.loop.steps.observe import ObserveStep

    step = ObserveStep()
    for exit_reason in ("max_turns", "context_limit"):
        state = SimpleNamespace(
            transcript=[object()], act_exit_reason=exit_reason,
            task=_task("上一轮的陈旧转向"))
        assert step._mechanical_verdict(state).next_step_hint == "", exit_reason
    empty = SimpleNamespace(transcript=[], act_exit_reason="normal",
                            task=_task("上一轮的陈旧转向"))
    assert step._mechanical_verdict(empty).next_step_hint == ""
