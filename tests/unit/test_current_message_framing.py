"""current-message 框架：存 raw（driver）+ 渲染期分两处装饰（composer）。

- ## Current Task 框钉当前 task 的**首条** user_prompt（cache 前缀稳定，
  directive/capabilities 也随它）；
- ## Current Message 框 + 同语言提示跟随该 task 的**最新一条** user_prompt——
  「当前消息」在语义上就是最新一条，钉首条会把旧消息冒充成当前消息；
- 首条即最新（单条）时两框合并在同一回合（A 形态）。
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


def test_task_frame_on_first_current_message_frame_on_latest():
    """多轮（interactive 同 task 累积多条 user_prompt）：任务框钉首条，当前消息框跟最新一条。

    ## Current Task 框（连同 directive/capabilities）钉首条保 cache 前缀稳定；
    ## Current Message 语义上就是「最新一条用户消息」，钉首条会把旧消息冒充成当前消息。
    """
    comp = _comp()
    history_pairs = [
        (_u("检查工作目录"), "agent_recall", "user_prompt", "t1"),
        (_a("好的"), "agent_recall", "llm_response", "t1"),
        (_u("把这个 ppt 转 pdf"), "agent_recall", "user_prompt", "t1"),
    ]
    messages = [m for m, *_ in history_pairs]
    comp._frame_current_message(messages, history_pairs, _task())
    # 首条：任务框 + ## Opening Message 标注原文，不再冒充当前消息
    first = messages[0].content
    assert "## Current Task" in first
    assert "## Opening Message" in first
    assert "检查工作目录" in first
    assert first.index("## Current Task") < first.index("## Opening Message") < first.index("检查工作目录")
    assert "## Current Message" not in first
    assert "Reply in the same language" not in first
    # 最新一条：当前消息框 + 同语言提示
    assert "## Current Message" in messages[2].content
    assert "把这个 ppt 转 pdf" in messages[2].content
    assert "Reply in the same language" in messages[2].content
    assert "## Current Task" not in messages[2].content


def test_middle_follow_ups_stay_bare():
    """三条以上 user_prompt：中间的追问既不带任务框也不带当前消息框，保持原文。"""
    comp = _comp()
    history_pairs = [
        (_u("第一条"), "agent_recall", "user_prompt", "t1"),
        (_a("好的"), "agent_recall", "llm_response", "t1"),
        (_u("中间追问"), "agent_recall", "user_prompt", "t1"),
        (_a("嗯"), "agent_recall", "llm_response", "t1"),
        (_u("最新追问"), "agent_recall", "user_prompt", "t1"),
    ]
    messages = [m for m, *_ in history_pairs]
    comp._frame_current_message(messages, history_pairs, _task())
    assert "## Current Task" in messages[0].content
    assert messages[2].content == "中间追问"
    assert "## Current Message" in messages[4].content


def test_single_user_prompt_gets_combined_frame():
    """首条即最新（单条 user_prompt）：两框合并在同一回合（A 形态不变），不出 Opening Message。"""
    comp = _comp()
    history_pairs = [(_u("把这个 ppt 转 pdf"), "agent_recall", "user_prompt", "t1")]
    messages = [m for m, *_ in history_pairs]
    comp._frame_current_message(messages, history_pairs, _task())
    c = messages[0].content
    assert "## Current Task" in c and "## Current Message" in c
    assert "Reply in the same language" in c
    assert c.index("## Current Task") < c.index("## Current Message")
    assert "## Opening Message" not in c


def test_opening_message_heading_present_even_without_task_title():
    """无 title/description（无任务框可加）时，多轮首条仍冠 ## Opening Message——
    directive/capabilities 会追加到该回合，标题把原文和它们分隔开。"""
    comp = _comp()
    history_pairs = [
        (_u("开题"), "agent_recall", "user_prompt", "t1"),
        (_u("追问"), "agent_recall", "user_prompt", "t1"),
    ]
    messages = [m for m, *_ in history_pairs]
    comp._frame_current_message(messages, history_pairs, _task(title="", desc=""))
    assert "## Opening Message" in messages[0].content
    assert "## Current Task" not in messages[0].content
    assert "## Current Message" in messages[1].content


def test_latest_selection_filters_by_task_id():
    """最新一条按 task_id 过滤：召回里更晚的其它 task（子 body）user_prompt 不被当作当前消息。"""
    comp = _comp()
    history_pairs = [
        (_u("开题"), "agent_recall", "user_prompt", "t1"),
        (_a("好的"), "agent_recall", "llm_response", "t1"),
        (_u("t1 的追问"), "agent_recall", "user_prompt", "t1"),
        (_u("子任务的消息"), "agent_recall", "user_prompt", "c1"),
    ]
    messages = [m for m, *_ in history_pairs]
    comp._frame_current_message(messages, history_pairs, _task())
    assert "## Current Message" in messages[2].content
    assert "t1 的追问" in messages[2].content
    assert messages[3].content == "子任务的消息"


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
