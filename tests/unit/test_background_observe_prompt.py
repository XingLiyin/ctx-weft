"""composer：purpose=background_observe 装配——ROLE facet + background cue（按 boundary）。"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from ctx_weft.core.assembler.composer import (
    DefaultComposer, _BACKGROUND_BOUNDARY_DESC,
)


def _blocks():
    # 一条 identity facet（模拟 ROLE）+ 一条 user 历史，保证 _build_actor_messages 以 user 收尾
    from ctx_weft.core.assembler.assembler import ContextBlock
    return [
        ContextBlock(id="b0", source="identity", kind="identity", target="system",
                     content="ROLE-OBSERVE-BODY", priority=0, token_estimate=1, metadata={}),
        ContextBlock(id="b1", source="task_conversation", kind="history", target="messages",
                     content="原始诉求", priority=3, token_estimate=1,
                     metadata={"role": "user", "timestamp": "2026-01-01T00:00:00+00:00"}),
    ]


def _req(boundary, outputs=""):
    return SimpleNamespace(purpose="background_observe", task=SimpleNamespace(
        id="t1", user_prompt_in_memory=True, title="", description="", user_prompt="x",
        outputs=outputs, process_report="", process_report_at=None, tracking_task_ids=[],
        parent_task_id=None), session=SimpleNamespace(user_prompt="x"),
        template=None, bound_capabilities=[],
        extra={"observe_boundary": boundary})


def test_background_cue_only_process_report_no_verdict():
    msgs = DefaultComposer()._build_background_observe_messages(_blocks(), _req("interrupt"))
    joined = "\n".join(m.content for m in msgs if isinstance(m.content, str))
    assert "collect_process_report" in joined
    assert "无需判断" in joined            # 抑制三态裁决
    assert _BACKGROUND_BOUNDARY_DESC["interrupt"] in joined


@pytest.mark.parametrize("boundary", ["interrupt", "plain_text", "finish", "normal", "dispatch"])
def test_background_cue_injects_each_boundary(boundary):
    msgs = DefaultComposer()._build_background_observe_messages(_blocks(), _req(boundary))
    joined = "\n".join(m.content for m in msgs if isinstance(m.content, str))
    assert _BACKGROUND_BOUNDARY_DESC[boundary] in joined


def test_dispatch_boundary_cue_is_not_normal_close_wording():
    """dispatch 段：父挂起等子任务完成，不是"正常结束"——不该落回 normal 的兜底文案。"""
    from ctx_weft.core.assembler.composer import _background_observe_cue
    dispatch_cue = _background_observe_cue("dispatch")
    assert _BACKGROUND_BOUNDARY_DESC["normal"] not in dispatch_cue
    assert "dispatch" in _BACKGROUND_BOUNDARY_DESC
    assert _BACKGROUND_BOUNDARY_DESC["dispatch"] in dispatch_cue
    # 语义：委派出去 + 挂起等待，而非"正常结束"
    assert "委派" in _BACKGROUND_BOUNDARY_DESC["dispatch"]
    assert "挂起" in _BACKGROUND_BOUNDARY_DESC["dispatch"]


_FINISH_RESULT = "工作目录现状：仅一个 即兴演讲训练.pptx，无活跃项目。"


@pytest.mark.parametrize("boundary", ["finish", "normal"])
def test_close_boundary_injects_finish_result(boundary):
    """close 段把 actor 最终产出注入 prompt，使观察者据实总结、不虚构。"""
    msgs = DefaultComposer()._build_background_observe_messages(
        _blocks(), _req(boundary, outputs=_FINISH_RESULT))
    joined = "\n".join(m.content for m in msgs if isinstance(m.content, str))
    assert _FINISH_RESULT in joined
    assert "Actor 的最终产出" in joined
    # 注入段在 cue 之前（先看产出，再被要求总结）
    assert joined.index(_FINISH_RESULT) < joined.index("collect_process_report")


@pytest.mark.parametrize("boundary", ["interrupt", "plain_text"])
def test_non_close_boundary_does_not_inject_finish_result(boundary):
    """非 close 段有真实 actor 动作可见，不注入 finish 产出。"""
    msgs = DefaultComposer()._build_background_observe_messages(
        _blocks(), _req(boundary, outputs=_FINISH_RESULT))
    joined = "\n".join(m.content for m in msgs if isinstance(m.content, str))
    assert "Actor 的最终产出" not in joined


def test_close_boundary_no_outputs_no_injection():
    """close 段但无产出（task.outputs 空）→ 不注入，保持原行为。"""
    msgs = DefaultComposer()._build_background_observe_messages(
        _blocks(), _req("finish", outputs=""))
    joined = "\n".join(m.content for m in msgs if isinstance(m.content, str))
    assert "Actor 的最终产出" not in joined


def test_observe_cue_mentions_both_fields():
    from ctx_weft.core.assembler.composer import _OBSERVE_JUDGMENT_CUE, _background_observe_cue
    assert "act_recap" in _OBSERVE_JUDGMENT_CUE
    assert "task_summary" in _OBSERVE_JUDGMENT_CUE
    close_cue = _background_observe_cue("finish")
    assert "act_recap" in close_cue and "task_summary" in close_cue
    # 综合子任务结果的引导
    assert "sub-task" in close_cue.lower() or "子任务" in close_cue
