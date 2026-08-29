"""act 纯文本暂停（wait_for_user）的冷路径。

热路径：actor 纯文本 → HITL park → 用户回复经冷 resume 注入 task 层 → 重入 act。
冷路径：超时驱逐 / 重启使 reconcile 覆盖不到（无 dangling tool_call）→ recover_session 收到
user_reply 时在 drain 前注入回复。本测试钉死注入逻辑与「act ask_user/approval 仍走 reconcile」的分流。
"""

from __future__ import annotations

import pytest

from ctx_weft.core import CtxWeftRuntime
from ctx_weft.core.orchestrator.hitl_manager import HitlRequest
from ctx_weft.core.orchestrator.task_manager import TaskManager
from ctx_weft.core.state.models import Session, Task
from ctx_weft.protocols import MemoryEventType, MemoryAddress, ProviderContext
from ctx_weft.providers.memory.in_memory import InMemoryMemoryProvider
from tests.integration.test_minimal_loop import InlineAgentTemplateProvider, make_runtime

pytestmark = pytest.mark.asyncio


def _runtime_with_memory() -> tuple[CtxWeftRuntime, InMemoryMemoryProvider]:
    rt = make_runtime(agent_provider=InlineAgentTemplateProvider())
    mem = InMemoryMemoryProvider()
    rt.providers.register_memory(mem)
    return rt, mem


def _tm_with_task(status: str = "SUSPENDED") -> tuple[TaskManager, Task]:
    tm = TaskManager(session_id="s1")
    task = Task(id="t1", session_id="s1", status=status, title="T1")
    tm._tasks["t1"] = task                    # restore 在真实路径已登记;这里直接放
    return tm, task


async def _recall_user_prompts(mem: InMemoryMemoryProvider) -> list:
    return await mem.recall_recent(
        scope=MemoryAddress(session_id="s1", task_id="t1", agent_id="ag1"),
        types=[MemoryEventType.USER_PROMPT],
        limit=10,
        ctx=ProviderContext(session_id="s1", tenant_id="default", task_id="t1"),
    )


def _wait_for_user_req(status: str = "accepted", message: str = "ship it") -> HitlRequest:
    return HitlRequest(
        id="hit1", form="wait", session_id="s1", task_id="t1", agent_id="ag1",
        capability_id="control:wait_for_user", status=status, message=message,
    )


async def test_inject_writes_user_prompt_and_requeues() -> None:
    rt, mem = _runtime_with_memory()
    tm, task = _tm_with_task()
    session = Session(id="s1", tenant_id="default", user_prompt="x", status="RUNNING")

    await rt._inject_user_reply(_wait_for_user_req(message="ship it"), session, tm)

    assert task.status == "PENDING"
    assert task.process_report is None
    recs = await _recall_user_prompts(mem)
    assert any(r.content == "ship it" and r.metadata.get("source") == "hitl_reply" for r in recs)


async def test_inject_reject_writes_declined() -> None:
    rt, mem = _runtime_with_memory()
    tm, _ = _tm_with_task()
    session = Session(id="s1", tenant_id="default", user_prompt="x", status="RUNNING")

    await rt._inject_user_reply(_wait_for_user_req(status="rejected", message="not now"), session, tm)

    recs = await _recall_user_prompts(mem)
    assert any("Human declined: not now" in r.content for r in recs)


async def test_inject_missing_task_is_noop() -> None:
    rt, mem = _runtime_with_memory()
    tm = TaskManager(session_id="s1")             # 无 t1
    session = Session(id="s1", tenant_id="default", user_prompt="x", status="RUNNING")

    await rt._inject_user_reply(_wait_for_user_req(), session, tm)   # 不抛

    assert await _recall_user_prompts(mem) == []


async def test_resume_routes_wait_for_user_to_injection(monkeypatch) -> None:
    rt, _ = _runtime_with_memory()
    captured: dict = {}

    async def fake_recover(session_id, *, user_reply=None, llm_account=None, llm_model=None, resumed_task_id=None):
        captured["resumed_task_id"] = resumed_task_id
        captured["session_id"] = session_id
        captured["user_reply"] = user_reply

    monkeypatch.setattr(rt, "recover_session", fake_recover)
    req = _wait_for_user_req()
    await rt._resume_after_cold_hitl(req)

    assert captured["session_id"] == "s1"
    assert captured["user_reply"] is req           # act 纯文本暂停 → 注入
    assert captured["resumed_task_id"] == "t1"     # 被应答的 task 透传 → 复用活 owner 就地重驱


async def test_resume_act_ask_user_uses_reconcile(monkeypatch) -> None:
    rt, _ = _runtime_with_memory()
    captured: dict = {}

    async def fake_recover(session_id, *, user_reply=None, llm_account=None, llm_model=None, resumed_task_id=None):
        captured["resumed_task_id"] = resumed_task_id
        captured["user_reply"] = user_reply

    monkeypatch.setattr(rt, "recover_session", fake_recover)
    req = HitlRequest(
        id="h2", form="question", session_id="s1", task_id="t1",
        capability_id="control:ask_user", status="accepted", message="postgres",
    )
    await rt._resume_after_cold_hitl(req)

    assert captured["user_reply"] is None          # act 的 ask_user → reconcile,不注入


async def test_resume_approval_uses_reconcile(monkeypatch) -> None:
    rt, _ = _runtime_with_memory()
    captured: dict = {}

    async def fake_recover(session_id, *, user_reply=None, llm_account=None, llm_model=None, resumed_task_id=None):
        captured["resumed_task_id"] = resumed_task_id
        captured["user_reply"] = user_reply

    monkeypatch.setattr(rt, "recover_session", fake_recover)
    req = HitlRequest(
        id="h3", form="approval", session_id="s1", task_id="t1",
        capability_id="bash:run", status="accepted",
    )
    await rt._resume_after_cold_hitl(req)

    assert captured["user_reply"] is None          # approval 门控 → reconcile,不注入
