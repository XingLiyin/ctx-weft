"""observe 装配合并（S2）：一条路径、按 purpose 分流，三处差异各自守住。

`_build_observe_messages` 现在是前台 `observe` 与后台 `background_observe` 的唯一实现。
本文件钉住的是**分流本身**——合并没有把两边的特征串味，也没有把任何一边的文案改掉。
三处差异（cue / subtask 指名清单 / actor 产出注入）各有一对用例。

S5 会把 `plain_text` 边界改成判定版 cue + 带 subtasks；届时这里的后台断言要跟着改，
而前台那几条不该动——这也是本文件存在的意义：让那次改动的影响面一眼可见。
"""

from __future__ import annotations

from types import SimpleNamespace

from ctx_weft.core.assembler.assembler import ContextBlock
from ctx_weft.core.assembler.composer import DefaultComposer


def _blocks():
    return [
        ContextBlock(id="b0", source="identity", kind="identity", target="system",
                     content="ROLE-OBSERVE-BODY", priority=0, token_estimate=1, metadata={}),
        ContextBlock(id="b1", source="task_conversation", kind="history", target="messages",
                     content="原始诉求", priority=3, token_estimate=1,
                     metadata={"role": "user", "type": "user_prompt",
                               "timestamp": "2026-01-01T00:00:00+00:00"}),
    ]


def _req(purpose: str, *, outputs: str = "", extra: dict | None = None):
    return SimpleNamespace(
        purpose=purpose,
        task=SimpleNamespace(
            id="t1", user_prompt_in_memory=True, title="", description="", user_prompt="x",
            outputs=outputs, process_report="", process_report_at=None, parent_task_id=None),
        session=SimpleNamespace(user_prompt="x"),
        template=None, bound_capabilities=[],
        extra=extra if extra is not None else {},
    )


def _cue(purpose: str, **kw) -> str:
    return DefaultComposer()._build_observe_messages(_blocks(), _req(purpose, **kw))[-1].content


# ── 差异一：cue ───────────────────────────────────────────────────────────────

def test_foreground_cue_asks_for_a_verdict() -> None:
    cue = _cue("observe")
    assert "report_task_outcome" in cue


def test_background_cue_forbids_judging() -> None:
    """后台 cue 明令只摘要、不判定——S5 改的正是 plain_text 边界的这一句。"""
    cue = _cue("background_observe", extra={"observe_boundary": "interrupt"})
    assert "do not judge" in cue
    assert "collect_process_report" in cue


# ── 差异二：subtask 指名清单 ──────────────────────────────────────────────────

_SUBTASKS = {"subtasks": [{"task_id": "tsk_a", "title": "甲", "outcome": "finished"}]}


def test_foreground_lists_subtasks_for_next_step_hint() -> None:
    cue = _cue("observe", extra=_SUBTASKS)
    assert "tsk_a" in cue
    assert "next_step_hint" in cue


def test_background_omits_subtasks_even_when_supplied() -> None:
    """后台今天不产 hint，所以不给指名清单——传了也不渲染。"""
    extra = {"observe_boundary": "interrupt", **_SUBTASKS}
    assert "tsk_a" not in _cue("background_observe", extra=extra)


# ── 差异三：actor 产出注入（两边文案不同，不是同一段话）────────────────────────

def test_foreground_injects_outputs_unconditionally() -> None:
    cue = _cue("observe", outputs="最终交付物")
    assert "## Final output" in cue
    assert "最终交付物" in cue


def test_background_injects_outputs_only_on_close_boundary() -> None:
    close = _cue("background_observe", outputs="最终交付物",
                 extra={"observe_boundary": "finish"})
    assert "## Actor's Final Output" in close
    assert "最终交付物" in close

    # plain_text 不是 close 边界：纯文本本身就是 assistant 回合、在重建的对话里看得见，
    # 不像 finish_task 的产出走 SILENT，所以不注入是对的。
    plain = _cue("background_observe", outputs="最终交付物",
                 extra={"observe_boundary": "plain_text"})
    assert "## Actor's Final Output" not in plain


# ── 分流的兜底 ────────────────────────────────────────────────────────────────

def test_missing_purpose_falls_back_to_foreground() -> None:
    """鸭子类型的手构 request 没有 purpose 时按前台走——要判定的是多数场景。"""
    req = _req("observe")
    del req.purpose
    cue = DefaultComposer()._build_observe_messages(_blocks(), req)[-1].content
    assert "report_task_outcome" in cue
