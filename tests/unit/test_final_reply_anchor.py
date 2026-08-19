"""close 折末段 raw 时补写「最终回复锚点」：

反转契约（spec 2026-07-01）下最终答复正文写在收尾回合的普通消息里，而 close 会把整个末段
raw 删掉、且不另产段摘要 —— 于是 task 层胶囊里只剩 USER_PROMPT + 段摘要 + finish 对（tool 槽
刻意「不掺 outputs」），最终答复无处安放。锚点即补位：与删 raw 同一次 fold 原子写入，
内容 = 提示词 + task.outputs。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

import ctx_weft.core.loop.steps.background_observe as bo
from ctx_weft.core.loop.steps.finalize import (
    FINAL_REPLY_NOTE, FINAL_REPLY_NOTE_UNTITLED, finalize_task_memory,
)
from ctx_weft.core.state.models import NormalTaskSettings, Task
from ctx_weft.protocols import (
    MemoryAddress, MemoryEvent, MemoryEventType, MemoryKind, MemoryScope, ProviderContext,
)
from ctx_weft.protocols.template import LoopConfig
from ctx_weft.providers.llm.tokenizer import HeuristicTokenizer
from ctx_weft.providers.memory_blackboard.in_memory import InMemoryMemoryProvider

pytestmark = pytest.mark.asyncio

T = MemoryEventType
_BASE = datetime(2026, 1, 1, tzinfo=UTC)
_REPLY = "已把 config 解析拆到 config/parse.py，入口保持 load_config() 不变。"


@pytest.fixture(autouse=True)
def _clear_bg_state():
    bo._close_report.clear()
    bo._close_synth.clear()
    yield
    bo._close_report.clear()
    bo._close_synth.clear()


def _pctx() -> ProviderContext:
    return ProviderContext(session_id="s1", tenant_id="default")


def _sc(task_id: str | None, agent_id: str = "ag1") -> MemoryAddress:
    return MemoryAddress(session_id="s1", task_id=task_id, agent_id=agent_id)


class _FakeTM:
    def children_of(self, task_id: str) -> set[str]:
        return set()


def _state(task: Task, scope: MemoryAddress):
    agent = SimpleNamespace(id=scope.agent_id, loop_config=LoopConfig())
    session = SimpleNamespace(id="s1", tenant_id="default")
    return SimpleNamespace(run_id="run1", sequence_counter=0, session=session,
                           scope=scope, task=task, agent=agent)


def _loop_ctx(mem):
    return SimpleNamespace(memory=mem, provider_ctx=_pctx(), task_manager=_FakeTM(),
                           llm=SimpleNamespace(tokenizer=HeuristicTokenizer()))


def _ev(type_, scope, content, t, role=None, **meta) -> MemoryEvent:
    return MemoryEvent(type=type_, address=scope, content=content,
                       timestamp=_BASE + timedelta(seconds=t), role=role, metadata=meta)


async def _seed_long_conv(mem, scope) -> None:
    """user 锚点 + 5 轮大体量 assistant（> turn_cap 且 > token 阈值 → 非 short）。"""
    await mem.ingest(_ev(T.USER_PROMPT, scope, "hello", 0, role="user"), _pctx())
    for i in range(5):
        await mem.ingest(
            _ev(T.LLM_RESPONSE, scope, "x " * 4000, i + 1, role="assistant"), _pctx())


async def _seed_short_conv(mem, scope) -> None:
    """user 锚点 + 1 轮短 assistant（short leaf → 不折 raw）。"""
    await mem.ingest(_ev(T.USER_PROMPT, scope, "hi", 0, role="user"), _pctx())
    await mem.ingest(_ev(T.LLM_RESPONSE, scope, "done", 1, role="assistant"), _pctx())


def _task(task_id="t1", title="抽取配置解析", outputs=_REPLY, parent=None) -> Task:
    return Task(id=task_id, session_id="s1", status="FINISHED", tenant_id="default",
                assigned_agent_id="ag1", creator_agent_id="ag1", parent_task_id=parent,
                title=title, user_prompt="hello", outputs=outputs,
                settings=NormalTaskSettings())


async def _anchors(mem, scope) -> list:
    """task 层里以最终回复提示词开头的 assistant 记录。"""
    view = await mem.load_view(scope, MemoryScope.TASK, _pctx(),
                               kinds=[MemoryKind.CONVERSATION_TURN])
    return [r for r in view
            if r.role == "assistant" and str(r.content).startswith("[Final reply")]


async def _close(mem, task, scope, *, has_llm_summary=True):
    await finalize_task_memory(mem, _state(task, scope), task, "out", "success",
                               _loop_ctx(mem), act_recap="recap", task_summary="总结",
                               has_llm_summary=has_llm_summary)


# ─── 1) 长任务 close：锚点带提示词 + 正文，且留在被删末段的位置 ───────────────

async def test_close_writes_final_reply_anchor_with_note() -> None:
    mem = InMemoryMemoryProvider()
    scope = _sc("t1")
    await _seed_long_conv(mem, scope)
    task = _task()

    await _close(mem, task, scope)

    found = await _anchors(mem, scope)
    assert len(found) == 1, f"末段 raw 折掉后应留一条最终回复锚点；实得 {len(found)}"
    anchor = found[0]
    assert anchor.content == f"{FINAL_REPLY_NOTE.format(title='抽取配置解析')}\n\n{_REPLY}", \
        f"锚点 = 提示词 + 空行 + outputs 正文；实得 {anchor.content!r}"
    assert anchor.timestamp == _BASE + timedelta(seconds=5), \
        "锚点须锚在被删末段最后一条 raw 的时刻（段内、早于 finish 对）"
    assert anchor.metadata.get("task_id") == "t1"


async def test_anchor_is_conversation_turn_not_compact_summary() -> None:
    """锚点走 CONVERSATION_TURN/assistant（渲染成普通 assistant 消息）；若写成
    TASK_COMPACT_SUMMARY 会在渲染期被追加「以上为压缩摘要」尾注，与提示词打架。"""
    mem = InMemoryMemoryProvider()
    scope = _sc("t1")
    await _seed_long_conv(mem, scope)

    await _close(mem, _task(), scope)

    anchor = (await _anchors(mem, scope))[0]
    assert anchor.kind is MemoryKind.CONVERSATION_TURN
    assert anchor.type is not T.TASK_COMPACT_SUMMARY


async def test_root_task_without_title_uses_untitled_note() -> None:
    mem = InMemoryMemoryProvider()
    scope = _sc("t1")
    await _seed_long_conv(mem, scope)

    await _close(mem, _task(title=""), scope)

    anchor = (await _anchors(mem, scope))[0]
    assert anchor.content.startswith(FINAL_REPLY_NOTE_UNTITLED), \
        f"无 title 时用不带任务名的提示词；实得 {anchor.content!r}"


# ─── 2) 无产出 / 短任务：不写锚点 ────────────────────────────────────────────

async def test_no_anchor_when_outputs_empty() -> None:
    """observer 护栏兜底等情况下 outputs 为空：只删 raw，不写空锚点。"""
    mem = InMemoryMemoryProvider()
    scope = _sc("t1")
    await _seed_long_conv(mem, scope)

    await _close(mem, _task(outputs=None), scope)

    assert await _anchors(mem, scope) == []


async def test_no_anchor_for_short_leaf() -> None:
    """short leaf 不折末段 raw（原文即胶囊），锚点会与原文重复。"""
    mem = InMemoryMemoryProvider()
    scope = _sc("t1")
    await _seed_short_conv(mem, scope)

    await _close(mem, _task(), scope)

    assert await _anchors(mem, scope) == []
    raw = await mem.recall_recent(scope, [T.LLM_RESPONSE], 100, _pctx())
    assert raw, "short leaf 的 raw 本来就该全留（回归）"


# ─── 3) 幂等：raw 已删则不再补写第二条 ───────────────────────────────────────

async def test_second_supersede_does_not_duplicate_anchor() -> None:
    mem = InMemoryMemoryProvider()
    scope = _sc("t1")
    await _seed_long_conv(mem, scope)
    task = _task()

    await _close(mem, task, scope)
    from ctx_weft.core.loop.steps.finalize import _supersede_final_raw_segment
    await _supersede_final_raw_segment(mem, scope, _pctx(), task=task)

    assert len(await _anchors(mem, scope)) == 1, "重入不得叠第二条锚点"


# ─── 4) 延迟折叠路径（bg 真摘要落地后补删）同样写锚点 ────────────────────────

async def test_deferred_fold_writes_anchor_on_slot_hit() -> None:
    """has_llm_summary=False + slot 命中：补删末段 raw 的同时写锚点。"""
    mem = InMemoryMemoryProvider()
    scope = _sc("t1")
    await _seed_long_conv(mem, scope)
    bo._close_report["t1"] = ("bg_act", "bg_sum")

    await _close(mem, _task(), scope, has_llm_summary=False)

    found = await _anchors(mem, scope)
    assert len(found) == 1, "延迟折叠补删时也须补锚点"
    assert _REPLY in found[0].content


# ─── 5) 渲染：普通 assistant 消息，不被贴「以上为压缩摘要」尾注 ────────────────

async def test_anchor_renders_as_plain_assistant_message() -> None:
    """回归护栏：锚点与段摘要相邻出现，两句注解各自只描述自己那条消息的正文
    （摘要说「以上」、锚点说「以下」），锚点不得被追加摘要尾注。"""
    from ctx_weft.core.assembler.sources._history import (
        ASSISTANT_SUMMARY_NOTE, record_to_history_block,
    )
    mem = InMemoryMemoryProvider()
    scope = _sc("t1")
    await _seed_long_conv(mem, scope)
    await _close(mem, _task(), scope)
    anchor = (await _anchors(mem, scope))[0]

    request = SimpleNamespace(scope=scope, token_counter=lambda t: len(t))
    block = record_to_history_block(anchor, source="agent_recall", idx=0,
                                    request=request, current_task_id="t1")

    assert block.metadata["role"] == "assistant"
    assert ASSISTANT_SUMMARY_NOTE not in block.content, "锚点不是摘要，不得贴摘要尾注"
    assert block.content == anchor.content, "锚点原样渲染，不加任何包装"
    assert block.priority == 6, "与 task 层胶囊同档"
