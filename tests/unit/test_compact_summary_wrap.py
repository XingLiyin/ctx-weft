"""compaction summary 渲染期包装（§2.4）：渲染带前缀，存储不含。"""
from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from ctx_weft.core.assembler.sources._history import (
    ASSISTANT_SUMMARY_NOTE, COMPACT_SUMMARY_WRAPPER_PREFIX, PROGRESS_SO_FAR_HEADING,
    record_to_history_block, wrap_compact_summary,
)
from ctx_weft.core.utils.estimate import estimate_tokens
from ctx_weft.protocols import MemoryEventType, MemoryRecord

T = MemoryEventType


def _req() -> SimpleNamespace:
    return SimpleNamespace(token_counter=estimate_tokens)


def _rec(type_, content, role="user"):
    return MemoryRecord(id="m1", type=type_, content=content,
                        timestamp=datetime(2026, 1, 1, tzinfo=UTC), role=role,
                        topic=None, metadata={"seq_no": 1})


def test_wrap_helper_prefixes():
    out = wrap_compact_summary("### 会话目标\nX")
    assert out.startswith(COMPACT_SUMMARY_WRAPPER_PREFIX)
    assert "### 会话目标" in out


def test_task_compact_summary_block_wrapped():
    blk = record_to_history_block(_rec(T.TASK_COMPACT_SUMMARY, "### 会话目标\nX"), "task_conversation", 0, request=_req())
    assert blk.content.startswith(COMPACT_SUMMARY_WRAPPER_PREFIX)


def test_task_compact_summary_assistant_gets_progress_heading():
    """role=assistant 的段摘要 = 当前 task 上一段执行复述：不套「并非用户新指令」包装，
    而是冠以 PROGRESS_SO_FAR_HEADING（当 record 归属当前 task 时）。判据按 task_id 匹配。"""
    rec = MemoryRecord(id="m1", type=T.TASK_COMPACT_SUMMARY, content="### 会话目标\nX",
                       timestamp=datetime(2026, 1, 1, tzinfo=UTC), role="assistant",
                       topic=None, metadata={"seq_no": 1, "task_id": "t_cur"})
    blk = record_to_history_block(rec, "agent_recall", 0, request=_req(), current_task_id="t_cur")
    assert not blk.content.startswith(COMPACT_SUMMARY_WRAPPER_PREFIX)
    assert blk.content == f"{PROGRESS_SO_FAR_HEADING}\n### 会话目标\nX\n\n{ASSISTANT_SUMMARY_NOTE}"
    assert blk.metadata["role"] == "assistant"


def test_task_compact_summary_assistant_no_heading_for_cross_task_capsule():
    """跨 task 胶囊（record 的 task_id != 当前 task）不冠 Progress So Far 标题——
    标题只用于当前任务的上一段复述，不改跨任务重建形态。"""
    rec = MemoryRecord(id="m1", type=T.TASK_COMPACT_SUMMARY, content="### 会话目标\nX",
                       timestamp=datetime(2026, 1, 1, tzinfo=UTC), role="assistant",
                       topic=None, metadata={"seq_no": 1, "task_id": "t_other"})
    blk = record_to_history_block(rec, "agent_recall", 0, request=_req(), current_task_id="t_cur")
    assert blk.content == f"### 会话目标\nX\n\n{ASSISTANT_SUMMARY_NOTE}"
    assert PROGRESS_SO_FAR_HEADING not in blk.content


def test_assistant_summary_gets_trailing_note():
    """assistant 身份的段摘要须带尾注：说明其为系统压缩产物、不应模仿该形式作答。
    尾注在末尾（离下一条 user 消息最近），且不落库（只在渲染块上）。"""
    rec = MemoryRecord(id="m1", type=T.TASK_COMPACT_SUMMARY, content="### 会话目标\nX",
                       timestamp=datetime(2026, 1, 1, tzinfo=UTC), role="assistant",
                       topic=None, metadata={"seq_no": 1, "task_id": "t_cur"})
    blk = record_to_history_block(rec, "agent_recall", 0, request=_req(), current_task_id="t_cur")
    assert blk.content.endswith(ASSISTANT_SUMMARY_NOTE)
    assert rec.content == "### 会话目标\nX", "尾注只在渲染期，不得改记录本身"


def test_user_role_summary_has_no_assistant_note():
    """role=user 的旧摘要走前缀包装，不叠加 assistant 尾注（两套消歧义不重复）。"""
    blk = record_to_history_block(_rec(T.TASK_COMPACT_SUMMARY, "### 会话目标\nX"),
                                  "task_conversation", 0, request=_req())
    assert ASSISTANT_SUMMARY_NOTE not in blk.content


def test_plain_user_prompt_not_wrapped():
    blk = record_to_history_block(_rec(T.USER_PROMPT, "你好"), "task_conversation", 0, request=_req())
    assert blk.content == "你好"


def test_agent_conversation_turn_not_wrapped():
    """胶囊里的 assistant summary 是 AGENT_CONVERSATION_TURN，不应被包装。"""
    blk = record_to_history_block(_rec(T.AGENT_CONVERSATION_TURN, "### 会话目标\nX", role="assistant"),
                                  "agent_experience", 0, request=_req())
    assert blk.content == "### 会话目标\nX"


from ctx_weft.core.assembler.sources.agent_recall import AgentRecallSource
from ctx_weft.protocols import MemoryAddress, ProviderContext


def _rec_task(type_, content, task_id, role="assistant"):
    return MemoryRecord(id=f"m-{task_id}", type=type_, content=content,
                        timestamp=datetime(2026, 1, 1, tzinfo=UTC), role=role,
                        topic=None, metadata={"seq_no": 1, "task_id": task_id})


@pytest.mark.asyncio
async def test_agent_recall_heading_only_for_current_task_summary():
    """真实装配回归（526859f）：AgentRecallSource 统一召回后，当前 task 的段摘要须冠
    ## Progress So Far，跨 task 胶囊不冠。彼时 source 由 task_conversation 改为 agent_recall，
    _history 的标题条件成死码——单靠 source 名区分不出「当前 task 自己的段摘要」。"""
    cur = _rec_task(T.TASK_COMPACT_SUMMARY, "本段进度X", task_id="t_cur")
    other = _rec_task(T.TASK_COMPACT_SUMMARY, "别的task进度Y", task_id="t_other")

    class _M:
        async def load_view(self, address, scope, ctx, kinds=None):
            from ctx_weft.protocols import MemoryScope
            return [cur, other] if scope is MemoryScope.TASK else []

    deps = SimpleNamespace(memory=_M(),
                           provider_ctx=ProviderContext(session_id="s1", tenant_id="default"))
    req = SimpleNamespace(scope=MemoryAddress(session_id="s1", task_id="t_cur", agent_id="a1"),
                          token_counter=estimate_tokens)
    contents = {b.content for b in [x async for x in AgentRecallSource().fetch(req, deps)]}

    # 两条都带 assistant 尾注（见 test_assistant_summary_gets_trailing_note），此处只看标题
    assert f"{PROGRESS_SO_FAR_HEADING}\n本段进度X" in "\n".join(contents), "当前 task 段摘要须冠 Progress So Far"
    assert any(c.startswith("别的task进度Y") for c in contents), "跨 task 胶囊仍应渲染"
    assert f"{PROGRESS_SO_FAR_HEADING}\n别的task进度Y" not in "\n".join(contents), "跨 task 胶囊不应冠标题"


class _Mem:
    """v2 fake：load_view 按 provider 契约返回 kind 已重打的记录。"""
    def __init__(self, agent_recs): self._agent_recs = agent_recs
    async def load_view(self, address, scope, ctx, kinds=None):
        from ctx_weft.protocols import MemoryScope
        return self._agent_recs if scope is MemoryScope.AGENT else []


@pytest.mark.asyncio
async def test_agent_compact_summary_rendered_wrapped():
    from ctx_weft.protocols import MemoryKind, MemoryScope
    rec = _rec(T.AGENT_COMPACT_SUMMARY, "### 既往派发摘要\nY")
    rec.kind, rec.layer = MemoryKind.SUMMARY, MemoryScope.AGENT  # provider 契约：kind 已重打
    deps = SimpleNamespace(memory=_Mem([rec]), provider_ctx=ProviderContext(session_id="s1", tenant_id="default"))
    req = SimpleNamespace(scope=MemoryAddress(session_id="s1", agent_id="a1"),
                          token_counter=estimate_tokens)
    blocks = [b async for b in AgentRecallSource().fetch(req, deps)]
    summ = [b for b in blocks if b.metadata.get("type") == T.AGENT_COMPACT_SUMMARY]
    assert summ and summ[0].content.startswith(COMPACT_SUMMARY_WRAPPER_PREFIX)


@pytest.mark.asyncio
async def test_agent_layer_recall_uses_uncapped_load_view():
    """agent 层召回走 load_view 全量幸存视图（协议无 limit 参数，v2 §4）：体量交给 token
    守卫，不按条数截断——否则滚动 AGENT_COMPACT_SUMMARY（锚在最早）会被截出窗、丢经验。"""
    calls = []

    class _SpyMem:
        async def load_view(self, address, scope, ctx, kinds=None):
            from ctx_weft.protocols import MemoryScope
            if scope is MemoryScope.AGENT:
                calls.append({"address": address, "kinds": kinds})
            return []

    deps = SimpleNamespace(memory=_SpyMem(),
                           provider_ctx=ProviderContext(session_id="s1", tenant_id="default"))
    req = SimpleNamespace(scope=MemoryAddress(session_id="s1", agent_id="a1"),
                          token_counter=estimate_tokens)
    _ = [b async for b in AgentRecallSource().fetch(req, deps)]
    assert calls, "agent 层召回须经 load_view（全量幸存、无条数上限）"
    assert calls[0]["kinds"] is None, "默认 kinds（CONVERSATION_TURN+SUMMARY）——不得窄化漏摘要"
