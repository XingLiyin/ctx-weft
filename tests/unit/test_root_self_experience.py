"""Root-task self-experience: agent-layer rendering + finalize writes."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from ctx_weft.core.assembler.assembler import AssemblerDeps, ContextRequest
from ctx_weft.core.assembler.sources.agent_experience import AgentExperienceSource
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


def _sc(task_id: str = "t1", agent_id: str = "ag1") -> MemoryScope:
    return MemoryScope(session_id="s1", task_id=task_id, agent_id=agent_id)


def _conv_ev(content, t, role, **meta) -> MemoryEvent:
    return MemoryEvent(
        type=T.AGENT_CONVERSATION_TURN, scope=_sc(), content=content,
        timestamp=_BASE + timedelta(seconds=t), role=role, metadata=meta,
    )


async def _collect(source, mem) -> list:
    deps = AssemblerDeps(memory=mem, knowledge_providers=[], provider_ctx=_ctx())
    req = ContextRequest(
        purpose="act", scope=_sc(task_id="t2"), task=None, agent=None, session=None,
        template=None, bound_capabilities=[],
    )
    return [b async for b in source.fetch(req, deps)]


async def test_agent_experience_renders_conversation_turns() -> None:
    mem = InMemoryMemoryProvider()
    # 上一个 root task 的对话被保全为 AGENT_CONVERSATION_TURN（agent 层，按 agent_id 跨 task）
    await mem.ingest(_conv_ev("hi there", 0, "user"), _ctx())
    await mem.ingest(_conv_ev("on it", 1, "assistant",
                              tool_calls=[{"id": "tc1", "name": "web", "input": {"q": "x"}}]), _ctx())
    await mem.ingest(_conv_ev("search out", 2, "tool", tool_call_id="tc1"), _ctx())

    blocks = await _collect(AgentExperienceSource(), mem)
    by_role = {}
    for b in blocks:
        by_role.setdefault(b.metadata["role"], []).append(b)

    assert by_role["user"][0].content == "hi there"
    assert by_role["assistant"][0].metadata["tool_calls"][0]["id"] == "tc1"
    assert by_role["tool"][0].metadata["tool_call_id"] == "tc1"


# ---------------------------------------------------------------------------
# record_root_self_experience tests
# ---------------------------------------------------------------------------

from ctx_weft.core.loop.steps.finalize import (
    ROOT_SELF_EXPERIENCE_TURN_LIMIT,
    record_root_self_experience,
)
from ctx_weft.core.state.models import NormalTaskSettings, Task


def _root_task(task_id: str = "t1") -> Task:
    return Task(
        id=task_id, session_id="s1", status="ACTIVE", tenant_id="default",
        assigned_agent_id="ag1", creator_agent_id="ag1",
        title="Greet", description="", user_prompt="hello, who are you?",
        settings=NormalTaskSettings(),
    )


async def _seed_task_conversation(mem, scope, n_assistant: int) -> None:
    """Ingest USER_PROMPT + n_assistant LLM_RESPONSE into the task layer."""
    await mem.ingest(MemoryEvent(type=T.USER_PROMPT, scope=scope, content="hello, who are you?",
                                 timestamp=_BASE, role="user"), _ctx())
    for i in range(n_assistant):
        await mem.ingest(MemoryEvent(type=T.LLM_RESPONSE, scope=scope, content=f"reply {i}",
                                     timestamp=_BASE + timedelta(seconds=i + 1), role="assistant",
                                     metadata={"tool_calls": []}), _ctx())


async def test_few_turns_preserves_conversation() -> None:
    mem = InMemoryMemoryProvider()
    scope = _sc()
    await _seed_task_conversation(mem, scope, n_assistant=2)  # ≤3 → preserve

    result = await record_root_self_experience(mem, scope, _root_task(), "final out", "success", _ctx())

    assert result["mode"] == "conversation"
    turns = await mem.recall_recent(_sc(task_id="t2"), [T.AGENT_CONVERSATION_TURN], 100, _ctx())
    # 1 user + 2 assistant = 3 copied turns, recalled across task boundary (same agent_id)
    assert len(turns) == 3
    assert {r.role for r in turns} == {"user", "assistant"}
    # 没有写 dispatch pair
    pair = await mem.recall_recent(_sc(task_id="t2"), [T.TASK_DISPATCH_RESULT], 100, _ctx())
    assert pair == []


async def test_many_turns_writes_dispatch_pair() -> None:
    mem = InMemoryMemoryProvider()
    scope = _sc()
    await _seed_task_conversation(mem, scope, n_assistant=5)  # >3 → pair

    result = await record_root_self_experience(mem, scope, _root_task(), "final out", "success", _ctx())

    assert result["mode"] == "dispatch"
    disp = await mem.recall_recent(_sc(task_id="t2"), [T.TASK_DISPATCH], 100, _ctx())
    res = await mem.recall_recent(_sc(task_id="t2"), [T.TASK_DISPATCH_RESULT], 100, _ctx())
    assert len(disp) == 1 and len(res) == 1
    tcid = disp[0].metadata["tool_call_id"]
    assert res[0].metadata["tool_call_id"] == tcid              # paired
    assert disp[0].metadata["arguments"]["task_prompt"] == "hello, who are you?"
    assert "final out" in res[0].content
    # 原始 user prompt 作为一条 user 回合补回（否则经验里看不到"用户问的是什么"）
    turns = await mem.recall_recent(_sc(task_id="t2"), [T.AGENT_CONVERSATION_TURN], 100, _ctx())
    assert len(turns) == 1 and turns[0].role == "user"
    assert "hello, who are you?" in turns[0].content


async def test_dispatch_pair_includes_original_user_prompt() -> None:
    """>3-turn root: the synthesized experience must surface the original user prompt as a
    user turn, not only buried inside the delegate_task arguments."""
    mem = InMemoryMemoryProvider()
    scope = _sc()
    await _seed_task_conversation(mem, scope, n_assistant=5)  # >3 → dispatch path

    result = await record_root_self_experience(mem, scope, _root_task(), "final out", "success", _ctx())
    assert result["mode"] == "dispatch"

    # the user turn must precede the synthesized dispatch pair (so it renders as [user][assistant][tool])
    user_turns = await mem.recall_recent(_sc(task_id="t2"), [T.AGENT_CONVERSATION_TURN], 100, _ctx())
    disp = await mem.recall_recent(_sc(task_id="t2"), [T.TASK_DISPATCH], 100, _ctx())
    assert len(user_turns) == 1 and user_turns[0].role == "user"
    assert "hello, who are you?" in user_turns[0].content
    assert user_turns[0].timestamp <= disp[0].timestamp


async def test_conversation_appends_clean_final_answer() -> None:
    """finish_task 收尾的 root：经验末尾须补一条 role=assistant、内容=task.outputs、不含 Process Report。

    答复经 finish_task 进 task.outputs（SILENT，不入 task 层），那轮 LLM_RESPONSE content 为空，
    所以 conversation 重建本来只剩 USER_PROMPT。修复后须补回最终答复，且只放纯答复（不带 report
    格式，避免带偏后续 assistant 回复风格）。
    """
    mem = InMemoryMemoryProvider()
    scope = _sc()
    # task layer: user prompt + one EMPTY finish turn (answer lives in task.outputs)
    await mem.ingest(MemoryEvent(type=T.USER_PROMPT, scope=scope, content="who are you?",
                                 timestamp=_BASE, role="user"), _ctx())
    await mem.ingest(MemoryEvent(type=T.LLM_RESPONSE, scope=scope, content="",
                                 timestamp=_BASE + timedelta(seconds=1), role="assistant",
                                 metadata={"tool_calls": []}), _ctx())
    task = _root_task()
    task.outputs = "I am your assistant."
    mem_content = "I am your assistant.\n\nProcess Report: greeted the user"

    result = await record_root_self_experience(mem, scope, task, mem_content, "success", _ctx())
    assert result["mode"] == "conversation"

    turns = await mem.recall_recent(_sc(task_id="t2"), [T.AGENT_CONVERSATION_TURN], 100, _ctx())
    answers = [r for r in turns if r.role == "assistant" and r.metadata.get("final_answer")]
    assert len(answers) == 1, "expected exactly one appended final-answer assistant turn"
    assert answers[0].content == "I am your assistant."
    assert "Process Report" not in answers[0].content
    # the final answer must be the chronologically last turn (latest timestamp)
    newest = max(turns, key=lambda r: r.timestamp)
    assert newest.metadata.get("final_answer") is True


async def test_conversation_no_final_answer_when_no_output() -> None:
    """没有 outputs（如 fail 收尾无产出）时不追加空答复回合。"""
    mem = InMemoryMemoryProvider()
    scope = _sc()
    await _seed_task_conversation(mem, scope, n_assistant=2)
    task = _root_task()  # outputs left as default (None)

    await record_root_self_experience(mem, scope, task, "summary only", "fail", _ctx())

    turns = await mem.recall_recent(_sc(task_id="t2"), [T.AGENT_CONVERSATION_TURN], 100, _ctx())
    assert not any(r.metadata.get("final_answer") for r in turns)


async def test_threshold_boundary() -> None:
    assert ROOT_SELF_EXPERIENCE_TURN_LIMIT == 3
    # exactly 3 → conversation
    mem = InMemoryMemoryProvider()
    await _seed_task_conversation(mem, _sc(), n_assistant=3)
    r3 = await record_root_self_experience(mem, _sc(), _root_task(), "out", "success", _ctx())
    assert r3["mode"] == "conversation"
    # exactly 4 → dispatch
    mem4 = InMemoryMemoryProvider()
    await _seed_task_conversation(mem4, _sc(), n_assistant=4)
    r4 = await record_root_self_experience(mem4, _sc(), _root_task(), "out", "success", _ctx())
    assert r4["mode"] == "dispatch"


async def test_conversation_turns_count_toward_agent_compact() -> None:
    """AGENT_CONVERSATION_TURN records count in the agent-compact trigger set."""
    mem = InMemoryMemoryProvider()
    await mem.ingest(_conv_ev("u", 0, "user"), _ctx())
    await mem.ingest(_conv_ev("a", 1, "assistant", tool_calls=[]), _ctx())

    n = await mem.count_recent(
        _sc(task_id="t2"),
        [T.TASK_DISPATCH_RESULT, T.AGENT_COMPACT_SUMMARY, T.AGENT_CONVERSATION_TURN],
        _ctx(),
    )
    assert n == 2
