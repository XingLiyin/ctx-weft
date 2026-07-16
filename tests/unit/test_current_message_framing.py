"""current-message 框架：存 raw（driver）+ 渲染期只框当前 task 的**首条** user_prompt（composer）。

（2026-06-26 spec §2.6 曾定为「贴最近一条」；interactive 多轮下框随新消息漂移、
每轮打穿 cache 前缀，已改为钉在开启该 task 的首条消息上——追问回合保持原文，
生成点附近的任务锚由 act guidance 锚定行承担。）
"""
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


def test_frame_only_first_user_prompt_of_task():
    """多轮（interactive 同 task 累积多条 user_prompt）：只有**首条**被框，追问回合保持原文。

    框随新消息漂移会让上一轮被装饰的回合在下一轮重建时恢复原文，cache 前缀每轮被打穿；
    追问回合的就近任务锚由 act guidance 锚定行承担。
    """
    comp = _comp()
    history_pairs = [
        (_u("检查工作目录"), "agent_recall", "user_prompt", "t1"),
        (_a("好的"), "agent_recall", "llm_response", "t1"),
        (_u("把这个 ppt 转 pdf"), "agent_recall", "user_prompt", "t1"),
    ]
    messages = [m for m, *_ in history_pairs]
    comp._frame_current_message(messages, history_pairs, _task())
    assert "## Current Message" in messages[0].content            # 首条被框
    assert "## Current Task" in messages[0].content
    assert "Reply in the same language" in messages[0].content
    assert "检查工作目录" in messages[0].content
    assert messages[2].content == "把这个 ppt 转 pdf"              # 追问裸


def test_frame_targets_current_task_by_id_not_latest():
    """parent resume 后装配：召回里有 parent 自己的 user_prompt（task_id=t1）+ 更新的同 agent
    子 body user_prompt（task_id=c1）。框架须按 task_id 贴到 **当前 task（t1）**，而非最后一条
    （子 body），否则子 body 会顶着 parent 的 ## Current Task 头。tuple 第 4 元 = task_id。"""
    comp = _comp()
    history_pairs = [
        (_u("现在提交一个plan"), "agent_recall", "user_prompt", "t1"),   # parent 自己（当前 task）
        (_a("好的"), "agent_recall", "llm_response", "t1"),
        (_u("请向 Lily 打个招呼"), "agent_recall", "user_prompt", "c1"),  # 同 agent 子 body（更新）
        (_a("你好 Lily"), "agent_recall", "llm_response", "c1"),
    ]
    messages = [m for m, *_ in history_pairs]
    comp._frame_current_message(messages, history_pairs, _task())  # _task().id == "t1"
    # 当前 task（t1）自己的消息被框
    assert "## Current Message" in messages[0].content
    assert "现在提交一个plan" in messages[0].content
    # 子 body（c1）保持裸——不被误当作当前消息
    assert messages[2].content == "请向 Lily 打个招呼"
    assert "## Current" not in messages[2].content


def test_frame_ignores_agent_experience_user():
    """agent_experience/agent_conversation_turn 来源的 user 回合不被当作当前消息。

    AGENT_CONVERSATION_TURN（从之前 root task 折叠进 agent 层的经验）的 mtype 不是
    "user_prompt"，因此不会被 _frame_current_message 误认为当前消息。
    """
    comp = _comp()
    history_pairs = [
        (_u("旧自经验"), "agent_recall", "agent_conversation_turn", "old"),
        (_u("当前消息"), "agent_recall", "user_prompt", "t1"),
    ]
    messages = [m for m, *_ in history_pairs]
    comp._frame_current_message(messages, history_pairs, _task())
    assert messages[0].content == "旧自经验"
    assert "## Current Message" in messages[1].content


def test_frame_noop_when_no_task_conversation_user():
    """无 user_prompt 类型的 user 消息时，frame 为 noop。"""
    comp = _comp()
    history_pairs = [(_a("only assistant"), "agent_recall", "llm_response", "t1")]
    messages = [m for m, *_ in history_pairs]
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
