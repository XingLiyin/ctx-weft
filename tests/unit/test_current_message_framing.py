"""current-message 框架：存 raw（driver）+ 渲染期只贴最近一条 user（composer）（§2.6）。"""
from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from ctx_weft.protocols import LLMMessage
from ctx_weft.core.assembler.composer import DefaultComposer


def _comp() -> DefaultComposer:
    # _frame_current_message 只依赖入参、不依赖实例状态 → 绕过 __init__
    return DefaultComposer.__new__(DefaultComposer)


def _u(c): return LLMMessage(role="user", content=c)
def _a(c): return LLMMessage(role="assistant", content=c)


def _task(in_mem=True, title="PPTX转PDF", desc="转 PDF", prompt="把这个 ppt 转 pdf"):
    return SimpleNamespace(user_prompt_in_memory=in_mem, title=title, description=desc,
                           user_prompt=prompt, process_report=None, id="t1")


def test_frame_only_latest_user_turn():
    """多轮：只有最近一条 USER_PROMPT user 被框，历史 user 裸。

    history_pairs 现在是 (msg, src, mem_type) 三元组；
    _frame_current_message 依据 mem_type=="user_prompt" 识别当前消息。
    """
    comp = _comp()
    history_pairs = [
        (_u("检查工作目录"), "agent_recall", "user_prompt"),
        (_a("好的"), "agent_recall", "llm_response"),
        (_u("把这个 ppt 转 pdf"), "agent_recall", "user_prompt"),
    ]
    messages = [m for m, _src, _mtype in history_pairs]
    comp._frame_current_message(messages, history_pairs, _task())
    assert messages[0].content == "检查工作目录"                    # 历史裸
    assert "## Current Message" in messages[2].content            # 最近被框
    assert "## Current Task" in messages[2].content
    assert "Reply in the same language" in messages[2].content
    assert "把这个 ppt 转 pdf" in messages[2].content


def test_frame_ignores_agent_experience_user():
    """agent_experience/agent_conversation_turn 来源的 user 回合不被当作当前消息。

    AGENT_CONVERSATION_TURN（从之前 root task 折叠进 agent 层的经验）的 mtype 不是
    "user_prompt"，因此不会被 _frame_current_message 误认为当前消息。
    """
    comp = _comp()
    history_pairs = [
        (_u("旧自经验"), "agent_recall", "agent_conversation_turn"),
        (_u("当前消息"), "agent_recall", "user_prompt"),
    ]
    messages = [m for m, _src, _mtype in history_pairs]
    comp._frame_current_message(messages, history_pairs, _task())
    assert messages[0].content == "旧自经验"
    assert "## Current Message" in messages[1].content


def test_frame_noop_when_no_task_conversation_user():
    """无 user_prompt 类型的 user 消息时，frame 为 noop。"""
    comp = _comp()
    history_pairs = [(_a("only assistant"), "agent_recall", "llm_response")]
    messages = [m for m, _src, _mtype in history_pairs]
    comp._frame_current_message(messages, history_pairs, _task())
    assert messages[0].content == "only assistant"


@pytest.mark.asyncio
async def test_driver_persists_raw_user_prompt():
    from ctx_weft.providers.memory_blackboard.in_memory import InMemoryMemoryProvider
    from ctx_weft.protocols import MemoryEventType, MemoryScope, ProviderContext
    mem = InMemoryMemoryProvider()
    pctx = ProviderContext(session_id="s1", tenant_id="default")
    scope = MemoryScope(session_id="s1", task_id="t1", agent_id="ag1")
    task = _task()
    task.user_prompt_in_memory = False
    state = SimpleNamespace(task=task, scope=scope)
    ctx = SimpleNamespace(memory=mem, provider_ctx=pctx)
    # 调用 driver 内联的持久化逻辑（提取为可测函数，见实现 (a)）
    from ctx_weft.core.loop.driver import _persist_user_prompt
    await _persist_user_prompt(state, ctx)
    recs = await mem.recall_recent(scope, [MemoryEventType.USER_PROMPT], 10, pctx)
    assert recs[0].content == "把这个 ppt 转 pdf"                  # raw，无 ## Current
    assert "## Current" not in recs[0].content
    assert task.user_prompt_in_memory is True
