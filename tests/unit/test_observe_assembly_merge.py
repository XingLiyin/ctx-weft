"""observe 装配合并（S2）：一条路径、按 purpose 分流，三处差异各自守住。

`_build_observe_messages` 现在是前台 `observe` 与后台 `background_observe` 的唯一实现。
本文件钉住的是**分流本身**——合并没有把两边的特征串味，也没有把任何一边的文案改掉。
三处差异（cue / subtask 指名清单 / actor 产出注入）各有一对用例。

2026-09-28 起分流的判据从「前台 vs 后台」变成**同一个 `observe_boundary`**：前台恒判定、后台
看边界，而「注入产出」「事实句」两者都由一张共用的边界表定。本文件只留粗粒度的分流断言，
逐边界的细节在 `test_background_observe_prompt.py`。
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


def test_the_recap_tier_has_no_verdict_tool_at_all() -> None:
    """只摘要档：cue 点的是 `collect_process_report`，而且**不提**判决工具。

    它此前靠一句「do not judge success/retry/fail」来表达这件事，而那句话与桌面上唯一那个工具
    的「`task_status` 必填」直接冲突（2026-09-28 拆回两个工具后删掉了）。现在「这一档不判」是
    工具面的事实，不是一句叮嘱。
    """
    cue = _cue("background_observe", extra={"observe_boundary": "interrupt"})
    assert "collect_process_report" in cue
    assert "report_task_outcome" not in cue
    assert "do not judge" not in cue


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


# ── 差异三：actor 产出注入（2026-09-28 起两边共用一段文案，按边界决定注不注）──────

def test_foreground_injects_outputs_only_when_finish_task_closed_the_segment() -> None:
    """不再是「前台无条件注入」。

    注入的理由是 `finish_task` 走 SILENT、产出不在重建的对话里；而纯文本收尾（`normal`）的产出
    **本身就是**一条 assistant 回合，注入等于让它出现两遍。所以判据是边界，不是前台/后台。
    """
    closed = _cue("observe", outputs="最终交付物",
                  extra={"observe_boundary": "actor_done"})
    assert "## Actor's Final Output" in closed
    assert "最终交付物" in closed

    prose = _cue("observe", outputs="最终交付物", extra={"observe_boundary": "normal"})
    assert "## Actor's Final Output" not in prose


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
