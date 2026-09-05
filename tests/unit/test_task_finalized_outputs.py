"""TaskFinalized 带交付物，且「答复正文」与「交付物小结」分开出核。

背景：收尾时有两样东西一直被合成一个字符串塞进 `task.outputs`——
① 收尾回合的 assistant 正文（**答复本身**），② finish_task 的 `deliverables_summary`
（agent 给 reviewer 的自评清单）。而 `TaskFinalized` 的 payload 只有 `task_id/outcome`，
交付物根本没出核，host 的 `tasks.outputs_json` / `error` 两列一直是空的。就算送，送的
是拼好的单串——host 打印「最终答复」时会把自评清单一起打出来。

本文件锁两件事：
1. `_compose_final_outputs` 返回 `(body, summary)` 两段，拼接留给调用方；
   **`task.outputs` 仍是拼好的单串**——它有 6 处读取方，既有契约一个字不变。
2. `TASK_FINALIZED.payload.outputs` 两段分开，判据以 `task.outputs` 为准绳
   （retry 驳回把 outputs 置空 → 两段一起归空；extra 空而 outputs 有货 → 回落）。
"""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

from ctx_weft.core.capabilities.control_tools import FINISH_TASK_NAME
from ctx_weft.core.loop.steps import act as act_mod
from ctx_weft.core.loop.steps.act import ActStep, TurnRecord, _compose_final_outputs
from ctx_weft.core.loop.steps.finalize import FinalizeStep
from ctx_weft.core.loop.steps.observe import Verdict
from ctx_weft.core.models.task import NormalTaskSettings, Task
from ctx_weft.protocols import (
    MemoryAddress,
    MemoryEvent,
    MemoryEventType,
    ProviderContext,
    ToolCall,
)
from ctx_weft.protocols.events import EventType
from ctx_weft.protocols.template import LoopConfig
from ctx_weft.providers.llm.mock import MockLLMAdapter, MockResponse
from ctx_weft.providers.llm.tokenizer import HeuristicTokenizer
from ctx_weft.providers.memory.in_memory import InMemoryMemoryProvider
from tests.integration.test_interactive_task import _act_state_ctx

# asyncio_mode = "auto"（pyproject）→ async 测试无需显式 mark。


# ── 1) _compose_final_outputs：两段各自独立 ───────────────────────────────────


def _turn(n: int, text: str, *, summary: str | None = None) -> TurnRecord:
    calls = []
    if summary is not None:
        calls.append(ToolCall(id=f"tc{n}", name=FINISH_TASK_NAME,
                              arguments={"deliverables_summary": summary}))
    return TurnRecord(turn=n, messages_sent=[], assistant_text=text, tool_calls=calls)


def test_compose_returns_body_and_summary_separately() -> None:
    body, summary = _compose_final_outputs([_turn(1, "the answer", summary="wrote a.py, b.py")])
    assert body == "the answer"
    assert summary == "wrote a.py, b.py"


def test_compose_body_only() -> None:
    assert _compose_final_outputs([_turn(1, "just prose")]) == ("just prose", "")


def test_compose_summary_only() -> None:
    assert _compose_final_outputs([_turn(1, "", summary="only a checklist")]) == (
        "", "only a checklist")


def test_compose_both_empty() -> None:
    assert _compose_final_outputs([_turn(1, "   ", summary="  ")]) == ("", "")


def test_compose_body_backtracks_to_last_nonempty_text() -> None:
    """收尾回合只调 finish_task、正文为空 → 回溯本段最近一段非空 assistant_text。"""
    body, summary = _compose_final_outputs([
        _turn(1, "early noise"),
        _turn(2, "the real answer"),
        _turn(3, "", summary="checklist"),
    ])
    assert body == "the real answer"
    assert summary == "checklist"


# ── 2) 调用方：task.outputs 仍是拼好的单串（既有契约回归）───────────────────


async def test_act_outputs_stays_joined_string_for_plain_text() -> None:
    """纯文本收尾：outputs = 正文（无 summary），逐字不变。"""
    llm = MockLLMAdapter(responses=[MockResponse(text="Hi! Anything else?")])
    state, ctx, task, _hitl, _mem = _act_state_ctx("auto", llm)

    await ActStep().execute(state, ctx)

    assert task.outputs == "Hi! Anything else?"
    assert state.extra["final_body"] == "Hi! Anything else?"
    assert state.extra["final_summary"] == ""


async def test_act_outputs_stays_joined_string_with_summary(monkeypatch) -> None:
    """finish_task 收尾：outputs 仍是 `body\\n\\nsummary` 单串；两段另存 state.extra。"""
    llm = MockLLMAdapter(responses=[MockResponse(
        text="computed: 42",
        tool_calls=[ToolCall(id="tc1", name=FINISH_TASK_NAME,
                             arguments={"deliverables_summary": "checked the math twice"})],
    )])
    state, ctx, task, _hitl, _mem = _act_state_ctx("auto", llm)

    async def _fake_exec(st, _c, tool_calls):
        # 只替代 gateway 那一段：finish_task 的真实副作用就是置 actor_done。
        st.task.actor_done = True
        return [{"tool_call_id": tc.id, "result": "ok"} for tc in tool_calls]

    monkeypatch.setattr(act_mod, "_execute_tool_calls", _fake_exec)

    await ActStep().execute(state, ctx)

    assert task.outputs == "computed: 42\n\nchecked the math twice"
    assert state.extra["final_body"] == "computed: 42"
    assert state.extra["final_summary"] == "checked the math twice"


# ── 3) TASK_FINALIZED payload ────────────────────────────────────────────────


def _ctx_obj() -> ProviderContext:
    return ProviderContext(session_id="s1", tenant_id="default")


class _FakeTM:
    def children_of(self, task_id: str) -> set[str]:
        return set()


def _loop_ctx(mem):
    return SimpleNamespace(memory=mem, provider_ctx=_ctx_obj(), task_manager=_FakeTM(),
                           llm=SimpleNamespace(tokenizer=HeuristicTokenizer()))


async def _finalize_state(mem, *, outputs, extra: dict | None, outcome: str = "success",
                          error: str | None = None):
    scope = MemoryAddress(session_id="s1", task_id="c1", agent_id="ag2")
    await mem.ingest(MemoryEvent(type=MemoryEventType.USER_PROMPT, address=scope,
                                 content="do it", role="user",
                                 timestamp=datetime(2026, 1, 1, tzinfo=UTC)), _ctx_obj())
    task = Task(id="c1", session_id="s1", status="ACTIVE", tenant_id="default",
                assigned_agent_id="ag2", creator_agent_id="ag1", parent_task_id="p1",
                title="T", user_prompt="do it", settings=NormalTaskSettings())
    task.outputs = outputs
    task.error = error
    verdict = Verdict(task_outcome=outcome, act_recap="did stuff", task_summary="report")
    state = SimpleNamespace(
        run_id="r1", sequence_counter=0,
        session=SimpleNamespace(id="s1", tenant_id="default"),
        scope=scope, task=task, agent=SimpleNamespace(id="ag2", loop_config=LoopConfig()),
        verdict=verdict,
    )
    if extra is not None:
        state.extra = extra
    return state


def _finalized_payload(step_outcome) -> dict:
    evs = [e for e in step_outcome.events if e.type == EventType.TASK_FINALIZED]
    assert len(evs) == 1, f"expected exactly one TaskFinalized, got {[e.type for e in step_outcome.events]}"
    return evs[0].payload


async def test_finalized_payload_splits_body_and_summary() -> None:
    mem = InMemoryMemoryProvider()
    state = await _finalize_state(
        mem, outputs="the answer\n\nwrote a.py",
        extra={"final_body": "the answer", "final_summary": "wrote a.py"})

    payload = _finalized_payload(await FinalizeStep().execute(state, _loop_ctx(mem)))

    assert payload["task_id"] == "c1"
    assert payload["outcome"] == "success"
    assert payload["outputs"] == {"output": "the answer", "summary": "wrote a.py"}
    assert payload["error"] == ""


async def test_finalized_payload_carries_error() -> None:
    mem = InMemoryMemoryProvider()
    state = await _finalize_state(mem, outputs="partial", extra={}, outcome="fail",
                                  error="tool crashed")

    payload = _finalized_payload(await FinalizeStep().execute(state, _loop_ctx(mem)))

    assert payload["error"] == "tool crashed"


async def test_finalized_payload_empty_when_retry_rejects_outputs() -> None:
    """retry 驳回边界：observer 判本段不合格 → task.outputs 被置空，而 state.extra 里
    还留着上一轮的废稿。事件必须以 task.outputs 为准绳，两段一起归空——否则 host 会把
    一份已被驳回的稿子写进 tasks 表。"""
    mem = InMemoryMemoryProvider()
    state = await _finalize_state(
        mem, outputs="rejected draft", outcome="retry",
        extra={"final_body": "rejected draft", "final_summary": "stale checklist"})

    payload = _finalized_payload(await FinalizeStep().execute(state, _loop_ctx(mem)))

    assert state.task.outputs is None                      # FinalizeStep 的 retry 分支置空
    assert payload["outputs"] == {"output": "", "summary": ""}


async def test_finalized_payload_falls_back_to_task_outputs() -> None:
    """回落边界：recap 重启路径根本没跑过 ActStep，state.extra 是空的——
    这时 task.outputs 才是真答复。"""
    mem = InMemoryMemoryProvider()
    state = await _finalize_state(mem, outputs="restored answer", extra={})

    payload = _finalized_payload(await FinalizeStep().execute(state, _loop_ctx(mem)))

    assert payload["outputs"] == {"output": "restored answer", "summary": ""}


async def test_finalized_payload_tolerates_missing_extra() -> None:
    """SimpleNamespace 替身（既有单测：test_finalize_fail_reason / test_subtask_nesting）
    没有 extra 字段——不能因此炸。"""
    mem = InMemoryMemoryProvider()
    state = await _finalize_state(mem, outputs="answer", extra=None)
    assert not hasattr(state, "extra")

    payload = _finalized_payload(await FinalizeStep().execute(state, _loop_ctx(mem)))

    assert payload["outputs"] == {"output": "answer", "summary": ""}


async def test_finalized_payload_extracts_text_from_content_parts() -> None:
    """多模态形态：task.outputs 是 list[ContentPart] → output 送提取出来的纯文本。"""
    mem = InMemoryMemoryProvider()
    state = await _finalize_state(mem, extra={}, outputs=[
        {"type": "text", "text": "the multimodal answer"},
        {"type": "image", "source": {"type": "blob_ref", "ref": "blob_1"}},
    ])

    payload = _finalized_payload(await FinalizeStep().execute(state, _loop_ctx(mem)))

    assert payload["outputs"]["output"] == "the multimodal answer"
