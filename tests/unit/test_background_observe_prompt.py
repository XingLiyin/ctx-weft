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


def _req(boundary):
    return SimpleNamespace(purpose="background_observe", task=SimpleNamespace(
        id="t1", user_prompt_in_memory=True, title="", description="", user_prompt="x",
        outputs="", process_report="", process_report_at=None, tracking_task_ids=[],
        parent_task_id=None), session=SimpleNamespace(user_prompt="x"),
        template=None, bound_capabilities=[], actor_transcript=[],
        extra={"observe_boundary": boundary})


def test_background_cue_only_process_report_no_verdict():
    msgs = DefaultComposer()._build_background_observe_messages(_blocks(), _req("interrupt"))
    joined = "\n".join(m.content for m in msgs if isinstance(m.content, str))
    assert "collect_process_report" in joined
    assert "无需判断" in joined            # 抑制三态裁决
    assert _BACKGROUND_BOUNDARY_DESC["interrupt"] in joined


@pytest.mark.parametrize("boundary", ["interrupt", "plain_text", "finish", "normal"])
def test_background_cue_injects_each_boundary(boundary):
    msgs = DefaultComposer()._build_background_observe_messages(_blocks(), _req(boundary))
    joined = "\n".join(m.content for m in msgs if isinstance(m.content, str))
    assert _BACKGROUND_BOUNDARY_DESC[boundary] in joined
