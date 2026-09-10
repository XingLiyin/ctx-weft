"""端到端 · D 类：回答了 wait 气泡、LLM 还没开口时按暂停 → 整轮撤销（spec 2026-09-09）。

这是交互式会话里**最常走的那条路**：root task 是 `interactive`，每说完一段纯文本就
park 在一个 wait 气泡上，用户的下一句话经 `reply_to_hitl` 回答那个气泡（前端在
`status === 'PAUSED'` 时走的正是这条，见 ChatPanel 的 `waitReplyMut`）。

A 类（`send_message` 新建 task）那条在 `test_round_discarded_before_first_chunk_e2e.py`。
两者的差别是这份文件存在的理由：

- A 类的 task 是这条消息开出来的 → 丢弃时连 task 一起摘掉，agent 回 `idle`；
- D 类的 task 早就存在、是一段正在进行的对话 → 丢弃只把它退回开窗前的样子，
  agent 回 `waiting_human`，**而那个被回答掉的气泡要回到 pending**。

最后那一条是 (a) 方案的全部意义：`reply_to_hitl` 的终局被推迟成两阶段
（`pending_decision` → 提交点才 `HitlResolved`），所以撤销之后 RAM 与事件日志**都**
停在「人还没回答」，会话干干净净地回到 `PAUSED`，用户重答一次即可。
"""

from __future__ import annotations

import asyncio

import pytest

from ctx_weft.core.models.config import RuntimeConfig
from ctx_weft.core.runtime import SessionStartParams
from ctx_weft.protocols import LLMChunk, MemoryAddress, MemoryScope, ProviderContext
from ctx_weft.protocols.events import EventType
from ctx_weft.protocols.hitl import HitlReply
from ctx_weft.providers.llm.mock import MockLLMAdapter, MockResponse
from ctx_weft.providers.memory.in_memory import InMemoryMemoryProvider
from tests.integration.test_minimal_loop import (
    InlineAgentTemplateProvider,
    make_echo_template,
    make_runtime,
)

pytestmark = pytest.mark.asyncio

_MOCK_CONTEXT_LIMIT = 100_000
_RETRACTED = "actually never mind, forget I asked"


class _StallsBeforeFirstChunkLLM(MockLLMAdapter):
    """`stalling` 打开后卡在首个 chunk 之前不动（模拟 TTFT）。旁路调用不受影响。"""

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.stalling = False
        self.stalled = asyncio.Event()

    @staticmethod
    def _is_sidecar(request) -> bool:
        tools = getattr(request, "tools", None) or []
        return any(getattr(t, "name", "") == "control__update_task_metadata" for t in tools)

    def complete(self, request, stream=True):
        self.last_request = request
        if not self.stalling or self._is_sidecar(request):
            return super().complete(request, stream=stream)

        async def _gen():
            self.stalled.set()
            await asyncio.Event().wait()
            yield LLMChunk(kind="token", text="unreachable")  # pragma: no cover

        return _gen()


def _runtime(llm):
    resolver = InlineAgentTemplateProvider()
    resolver.register(make_echo_template())
    rt = make_runtime(llm=llm, agent_provider=resolver,
                      config=RuntimeConfig(llm_self_heal_max_attempts=1))
    rt.providers.register_memory(InMemoryMemoryProvider())
    return rt


async def _stored_types(rt, session_id: str) -> list[str]:
    return [e.type for e in await rt.event_store.read_by_session(session_id)]


async def _user_texts(rt, session_id: str, agent_id: str, task_id: str) -> list[str]:
    from ctx_weft.core.utils.content import content_to_text
    view = await rt.providers.get_memory().load_view(
        MemoryAddress(session_id=session_id, task_id=task_id, agent_id=agent_id),
        MemoryScope.TASK,
        ProviderContext(session_id=session_id, tenant_id="default",
                        task_id=task_id, agent_id=agent_id),
    )
    return [content_to_text(r.content) for r in view if r.role == "user"]


async def _wait_for_wait_bubble(rt, session_id: str, timeout: float = 5.0):
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        pending = [v for v in rt.list_pending_hitl(session_id=session_id) if v.form == "wait"]
        if pending:
            return pending[0]
        await asyncio.sleep(0.02)
    raise TimeoutError("wait bubble never appeared")


async def test_pause_before_first_chunk_puts_the_answered_bubble_back() -> None:
    llm = _StallsBeforeFirstChunkLLM(
        responses=[MockResponse(text=f"answer {i}") for i in range(8)],
        context_limit=_MOCK_CONTEXT_LIMIT)
    rt = _runtime(llm)

    handle = await rt.start_session(SessionStartParams.create(
        template_id="agent:tpl_echo", user_prompt="hi",
        context_limit=_MOCK_CONTEXT_LIMIT))
    sid, aid = handle.session_id, handle.agent_id

    # interactive 的 root task 说完一段纯文本就 park 出一个 wait 气泡 —— 这就是 D 类的起点。
    bubble = await _wait_for_wait_bubble(rt, sid)
    task_id = bubble.task_id
    types_before = await _stored_types(rt, sid)

    # ── 用户回答那个气泡，LLM 卡在首个 chunk 之前 ────────────────────────────
    llm.stalling = True
    view = await rt.reply_to_hitl(HitlReply(
        hitl_id=bubble.id, outcome="accepted", agent_id=bubble.agent_id,
        message=_RETRACTED))
    # 调用方视角：这次应答**被收下了**（两阶段之下「收下」与「落盘」不再同刻）。
    assert view is not None and view.outcome == "accepted"
    assert rt.hitl_registry.get(bubble.id).claim_pending is True
    assert rt.hitl_registry.get(bubble.id).resolved is False
    # 收下之后它就不该再出现在未决列表里 —— 否则 host 会让人对着刚答过的问题再答一次。
    assert bubble.id not in [v.id for v in rt.list_pending_hitl(session_id=sid)]

    await asyncio.wait_for(llm.stalled.wait(), timeout=5.0)
    assert rt._agent_lifecycle_manager.record_of(aid).status == "running"
    assert await _stored_types(rt, sid) == types_before, (
        "首个 chunk 之前，这一轮不得往事件日志里写任何东西（HitlResolved 也不许）"
    )

    # ── 按下暂停 ────────────────────────────────────────────────────────────
    assert await rt.pause_session(sid) is True
    await asyncio.sleep(0.5)

    # ① 日志逐条不变：`HitlResolved` 从来没发过，`HitlOpened` 还在原地
    types_after = await _stored_types(rt, sid)
    assert types_after == types_before, (
        f"这一轮当作没发生过；多出来的是 {types_after[len(types_before):]}"
    )

    # ② 气泡回到 pending —— (a) 方案的核心：撤销之后 RAM 与日志都停在「人还没回答」
    req = rt.hitl_registry.get(bubble.id)
    assert req.claim_pending is False and req.resolved is False
    assert bubble.id in [v.id for v in rt.list_pending_hitl(session_id=sid)], (
        "被撤销的那次应答之后，气泡必须重新出现在未决列表里，人才能重答"
    )

    # ③ 用户那句话被 fold 掉了
    texts = await _user_texts(rt, sid, aid, task_id)
    assert all(_RETRACTED not in t for t in texts), f"撤销的话仍在 memory 视图里：{texts}"

    # ④ agent 回 waiting_human（**不是** idle —— task 还在，只是重新等人）
    assert rt._agent_lifecycle_manager.record_of(aid).status == "waiting_human"

    # ⑤ task 还在，且退回了 AWAITING_HUMAN
    tm = rt._task_managers[sid]
    task = tm.get_task(task_id)
    assert task is not None and task.status == "AWAITING_HUMAN"

    # ⑥ 重答一次照常跑起来（会话没被这次撤销搞坏）
    llm.stalling = False
    view2 = await rt.reply_to_hitl(HitlReply(
        hitl_id=bubble.id, outcome="accepted", agent_id=bubble.agent_id,
        message="ok, a different question then"))
    assert view2 is not None
    await asyncio.sleep(1.5)
    assert EventType.HITL_RESOLVED in await _stored_types(rt, sid), (
        "重答之后这一轮真的跑起来了，HitlResolved 应当在提交点发出"
    )
