"""TASK_FAILED.error_message 须携带真死因（task.error = observer 的 task_failure_reason）。

此前 payload 填的是 verdict.act_recap（过程复述）——host 把它当 session_notice.reason_text
展示成「死因」，而 observer 专门写的 task_failure_reason（→ task.error）从未出核。
error_message = task.error；无死因（如规则 observe 判死）置空——过程复述不冒充死因，
固定提示文案由 host 合成 notice 时按 error_code 补。
"""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from ctx_weft.core.events.types import EventType
from ctx_weft.core.loop.steps.finalize import FinalizeStep
from ctx_weft.core.loop.steps.observe import Verdict
from ctx_weft.core.state.models import NormalTaskSettings, Task
from ctx_weft.protocols import (
    MemoryEvent,
    MemoryEventType,
    MemoryScope,
    ProviderContext,
)
from ctx_weft.protocols.template import LoopConfig
from ctx_weft.providers.llm.tokenizer import HeuristicTokenizer
from ctx_weft.providers.memory_blackboard.in_memory import InMemoryMemoryProvider

pytestmark = pytest.mark.asyncio

T = MemoryEventType


def _ctx() -> ProviderContext:
    return ProviderContext(session_id="s1", tenant_id="default")


class _FakeTM:
    def children_of(self, task_id: str) -> set[str]:
        return set()


def _loop_ctx(mem):
    return SimpleNamespace(memory=mem, provider_ctx=_ctx(), task_manager=_FakeTM(),
                           llm=SimpleNamespace(tokenizer=HeuristicTokenizer()))


async def _failed_state(mem, *, task_error: str | None, act_recap: str):
    scope = MemoryScope(session_id="s1", task_id="c1", agent_id="ag2")
    await mem.ingest(MemoryEvent(type=T.USER_PROMPT, scope=scope, content="do it",
                                 timestamp=datetime(2026, 1, 1, tzinfo=UTC),
                                 role="user"), _ctx())
    task = Task(id="c1", session_id="s1", status="FAILED", tenant_id="default",
                assigned_agent_id="ag2", creator_agent_id="ag1", parent_task_id="p1",
                title="Doomed Task", user_prompt="do it", settings=NormalTaskSettings())
    task.error = task_error
    verdict = Verdict(task_outcome="fail", act_recap=act_recap, task_summary="")
    agent = SimpleNamespace(id="ag2", loop_config=LoopConfig())
    session = SimpleNamespace(id="s1", tenant_id="default")
    return SimpleNamespace(run_id="r1", sequence_counter=0, session=session,
                           scope=scope, task=task, agent=agent, verdict=verdict)


def _task_failed_payload(outcome_events) -> dict:
    failed = [e for e in outcome_events if e.type == EventType.TASK_FAILED]
    assert len(failed) == 1
    return failed[0].payload


async def _retry_exhausted_state(mem, *, task_error: str | None):
    """observer 判 retry 但 retry_count 已到上限——finalize 应降级 fail（程序熔断）。"""
    scope = MemoryScope(session_id="s1", task_id="c1", agent_id="ag2")
    await mem.ingest(MemoryEvent(type=T.USER_PROMPT, scope=scope, content="do it",
                                 timestamp=datetime(2026, 1, 1, tzinfo=UTC),
                                 role="user"), _ctx())
    task = Task(id="c1", session_id="s1", status="PENDING", tenant_id="default",
                assigned_agent_id="ag2", creator_agent_id="ag1", parent_task_id="p1",
                title="Doomed Task", user_prompt="do it", settings=NormalTaskSettings())
    task.retry_count = task.max_retries
    task.error = task_error
    verdict = Verdict(task_outcome="retry", act_recap="第 3 轮尝试仍未通过校验", task_summary="")
    agent = SimpleNamespace(id="ag2", loop_config=LoopConfig())
    session = SimpleNamespace(id="s1", tenant_id="default")
    return SimpleNamespace(run_id="r1", sequence_counter=0, session=session,
                           scope=scope, task=task, agent=agent, verdict=verdict)


async def test_retry_exhausted_downgrade_has_own_code_and_blocker() -> None:
    """retry 耗尽降级：error_code 专属 TASK_FAILED_RETRY_EXHAUSTED（非 BY_OBSERVER），
    error_message = 最后一轮 retry 判决写的受阻原因（task.error）。"""
    mem = InMemoryMemoryProvider()
    state = await _retry_exhausted_state(mem, task_error="登录页有人机校验，自动化被拦")
    outcome = await FinalizeStep().execute(state, _loop_ctx(mem))
    payload = _task_failed_payload(outcome.events)
    assert payload["error_code"] == "TASK_FAILED_RETRY_EXHAUSTED"
    assert payload["error_message"] == "登录页有人机校验，自动化被拦"
    assert payload["retry_count"] == state.task.max_retries
    assert state.task.error_code == "TASK_FAILED_RETRY_EXHAUSTED"


async def test_retry_exhausted_without_blocker_message_empty() -> None:
    """耗尽但无受阻原因（旧 prompt / 机械退出轮）→ error_message 置空，host 补固定文案。"""
    mem = InMemoryMemoryProvider()
    state = await _retry_exhausted_state(mem, task_error=None)
    outcome = await FinalizeStep().execute(state, _loop_ctx(mem))
    payload = _task_failed_payload(outcome.events)
    assert payload["error_code"] == "TASK_FAILED_RETRY_EXHAUSTED"
    assert payload["error_message"] == ""


async def test_error_message_prefers_task_error() -> None:
    """observer 判死写了 task_failure_reason（→ task.error）→ error_message 用它，不用过程复述。"""
    mem = InMemoryMemoryProvider()
    state = await _failed_state(
        mem, task_error="第 2 步 API 调用 403：凭据无权限", act_recap="我调了 A、B 两个工具")
    outcome = await FinalizeStep().execute(state, _loop_ctx(mem))
    assert _task_failed_payload(outcome.events)["error_message"] == "第 2 步 API 调用 403：凭据无权限"


async def test_error_message_empty_without_task_error() -> None:
    """无 task.error（如规则 observe 判死）→ error_message 置空，不拿过程复述冒充死因。"""
    mem = InMemoryMemoryProvider()
    state = await _failed_state(mem, task_error=None, act_recap="[No actor execution recorded]")
    outcome = await FinalizeStep().execute(state, _loop_ctx(mem))
    assert _task_failed_payload(outcome.events)["error_message"] == ""
