"""装配重建：task_conversation + agent_experience → 结构化回合（spec/06 §4）。

锁定：
- task_conversation：LLM_RESPONSE 携 tool_calls、TOOL_RESULT 携 tool_call_id
- agent_experience：TASK_DISPATCH↔TASK_DISPATCH_RESULT 配对；未配对 dispatch 隐去
- composer 把两路 blocks 按 timestamp 归并为 user/assistant(tool_calls)/tool(tool_call_id) 回合
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from ctx_weft.core.assembler.assembler import AssemblerDeps, ContextRequest
from ctx_weft.core.assembler.composer import DefaultComposer
from ctx_weft.core.assembler.sources.agent_experience import AgentExperienceSource
from ctx_weft.core.assembler.sources.short_memory import RecentMemorySource
from ctx_weft.protocols import (
    MemoryEvent,
    MemoryEventType,
    MemoryScope,
    ProviderContext,
)
from ctx_weft.providers.memory_blackboard.in_memory import InMemoryMemoryProvider

pytestmark = pytest.mark.asyncio

T = MemoryEventType
_BASE = datetime(2026, 1, 1, tzinfo=UTC)


def _ctx() -> ProviderContext:
    return ProviderContext(session_id="s1", tenant_id="default")


def _sc() -> MemoryScope:
    return MemoryScope(session_id="s1", task_id="t1", agent_id="ag1")


def _ev(type_, content, t, role=None, **meta) -> MemoryEvent:
    return MemoryEvent(
        type=type_, scope=_sc(), content=content, timestamp=_BASE + timedelta(seconds=t),
        role=role, metadata=meta,
    )


async def _collect(source, mem) -> list:
    deps = AssemblerDeps(memory=mem, knowledge_providers=[], provider_ctx=_ctx())
    req = ContextRequest(
        purpose="act", scope=_sc(), task=None, agent=None, session=None,
        template=None, bound_capabilities=[],
    )
    return [b async for b in source.fetch(req, deps)]


async def test_task_conversation_carries_tool_call_links() -> None:
    mem = InMemoryMemoryProvider()
    await mem.ingest(_ev(T.USER_PROMPT, "hi", 0, role="user"), _ctx())
    await mem.ingest(_ev(T.LLM_RESPONSE, "thinking", 1, role="assistant",
                         tool_calls=[{"id": "tc1", "name": "web", "input": {"q": "x"}}]), _ctx())
    await mem.ingest(_ev(T.TOOL_RESULT, "result-text", 2, role="tool", tool_call_id="tc1"), _ctx())

    blocks = await _collect(RecentMemorySource(), mem)
    by_type = {b.metadata["type"]: b for b in blocks}
    assert by_type[T.LLM_RESPONSE].metadata["tool_calls"][0]["id"] == "tc1"
    assert by_type[T.TOOL_RESULT].metadata["tool_call_id"] == "tc1"


async def test_agent_experience_pairs_and_hides_unpaired() -> None:
    mem = InMemoryMemoryProvider()
    await mem.ingest(_ev(T.TASK_DISPATCH, "delegate_task(...)", 0, role="assistant",
                         tool_call_id="d1", tool_name="delegate_task", arguments={"title": "A"}), _ctx())
    await mem.ingest(_ev(T.TASK_DISPATCH_RESULT, "A done\n\nProcess Report: ok", 1, role="tool",
                         tool_call_id="d1"), _ctx())
    # 未配对的 dispatch（无结果）应隐去
    await mem.ingest(_ev(T.TASK_DISPATCH, "delegate_task(...)", 2, role="assistant",
                         tool_call_id="d2", tool_name="delegate_task", arguments={"title": "B"}), _ctx())

    blocks = await _collect(AgentExperienceSource(), mem)
    asst = [b for b in blocks if b.metadata["role"] == "assistant"]
    tool = [b for b in blocks if b.metadata["role"] == "tool"]
    assert len(asst) == 1 and asst[0].metadata["tool_calls"][0]["id"] == "d1"
    assert len(tool) == 1 and tool[0].metadata["tool_call_id"] == "d1"
    assert "A done" in tool[0].content
    assert all("d2" != b.metadata.get("tool_calls", [{}])[0].get("id") for b in asst)  # d2 隐去


async def test_composer_merges_two_sources_into_paired_turns() -> None:
    mem = InMemoryMemoryProvider()
    # task 层：user + assistant(real tool) + tool result
    await mem.ingest(_ev(T.USER_PROMPT, "do X", 0, role="user"), _ctx())
    await mem.ingest(_ev(T.LLM_RESPONSE, "searching", 1, role="assistant",
                         tool_calls=[{"id": "tc1", "name": "web", "input": {}}]), _ctx())
    await mem.ingest(_ev(T.TOOL_RESULT, "search out", 2, role="tool", tool_call_id="tc1"), _ctx())
    # agent 层：派发对（dispatch t=3, result t=4）
    await mem.ingest(_ev(T.TASK_DISPATCH, "delegate_task(...)", 3, role="assistant",
                         tool_call_id="d1", tool_name="delegate_task", arguments={"title": "sub"}), _ctx())
    await mem.ingest(_ev(T.TASK_DISPATCH_RESULT, "sub output", 4, role="tool", tool_call_id="d1"), _ctx())

    blocks = await _collect(RecentMemorySource(), mem) + await _collect(AgentExperienceSource(), mem)
    messages = DefaultComposer()._history_to_messages(blocks)

    # 期望顺序（按 timestamp）：user / assistant(tc1) / tool(tc1) / assistant(d1) / tool(d1)
    roles = [m.role for m in messages]
    assert roles == ["user", "assistant", "tool", "assistant", "tool"]
    assert messages[1].tool_calls[0]["id"] == "tc1"
    assert messages[2].tool_call_id == "tc1"
    assert messages[3].tool_calls[0]["id"] == "d1"
    assert messages[4].tool_call_id == "d1"


async def test_actor_messages_always_end_with_user() -> None:
    """actor prompt 必须以 user 结尾——history 以 assistant 收尾且无 Current Progress 时兜底补 user。"""
    from ctx_weft.core.assembler.assembler import ContextBlock

    blocks = [
        ContextBlock(id="b1", source="x", kind="history", target="messages", content="hi",
                     priority=3, token_estimate=1, metadata={"role": "user", "timestamp": "1"}),
        ContextBlock(id="b2", source="x", kind="history", target="messages", content="thinking",
                     priority=3, token_estimate=1, metadata={"role": "assistant", "timestamp": "2"}),
    ]
    task = SimpleNamespace(user_prompt_in_memory=True, process_report=None,
                           title="X", description="", user_prompt="hi")
    request = SimpleNamespace(task=task)
    msgs = DefaultComposer()._build_actor_messages(blocks, request)
    assert msgs[-1].role == "user"


async def test_observer_reuses_act_conversation_plus_observe_message() -> None:
    """观察者复用 act 风格会话（含全部 task 轮次 + 派发日志），尾部追加 observe 指令消息。"""
    from ctx_weft.core.assembler.assembler import ContextBlock

    def _blk(src, role, content, ts):
        return ContextBlock(id=f"b{ts}", source=src, kind="history", target="messages",
                            content=content, priority=3, token_estimate=1,
                            metadata={"role": role, "timestamp": ts})

    ident = ContextBlock(id="id", source="identity", kind="identity", target="system",
                         content="OBSERVER ROLE", priority=0, token_estimate=1, metadata={})
    blocks = [
        ident,
        _blk("task_conversation", "user", "round1 user", "1"),
        _blk("task_conversation", "assistant", "round1 reply", "2"),
        _blk("task_conversation", "assistant", "round2 reply", "4"),
        _blk("agent_experience", "assistant", "dispatch stuff", "5"),
    ]
    request = SimpleNamespace(
        task=SimpleNamespace(title="T", description="d", user_prompt="up",
                             user_prompt_in_memory=True, process_report=None),
        session=SimpleNamespace(user_prompt="up"),
        actor_transcript=[],
    )
    msgs = DefaultComposer()._build_observer_messages(blocks, request)
    joined = "\n".join(m.content for m in msgs if isinstance(m.content, str))
    assert "round1 reply" in joined and "round2 reply" in joined  # 全部轮次
    assert "dispatch stuff" in joined                             # act 风格：派发日志一并复用
    assert "OBSERVER ROLE" in joined                              # 尾部 observe ROLE
    assert "report_task_outcome" in joined                        # 判定提示
