"""Phase B integration test: input-kind COLD reconcile path end-to-end.

Proves that a pre-resolved (answered) HITL keyed by tool_call_id, when its
dangling tool_call is re-invoked by ReconcileStep through a REAL CapabilityGateway
+ real ControlCapabilityProvider, causes the human's answer text to be written
as a TOOL_RESULT into memory.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from ctx_weft.core.events.bus import InProcessEventBus
from ctx_weft.core.loop.capability_gateway import CapabilityGateway
from ctx_weft.core.loop.driver import LoopContext, LoopState
from ctx_weft.core.loop.steps.reconcile import ReconcileStep
from ctx_weft.core.orchestrator.capability_cache import CapabilityCache
from ctx_weft.core.orchestrator.control_capability import (
    PROVIDER_NAME,
    ControlCapabilityProvider,
)
from ctx_weft.core.orchestrator.hitl_manager import HitlManager
from ctx_weft.core.state.models import Session, Task
from ctx_weft.core.utils import now_utc
from ctx_weft.protocols import MemoryScope, ProviderContext
from ctx_weft.protocols.capability import ToolCapability
from ctx_weft.protocols.memory import MemoryEvent, MemoryEventType
from ctx_weft.providers.memory_blackboard.in_memory import InMemoryMemoryProvider


@pytest.mark.asyncio
async def test_cold_input_reconcile_writes_tool_result() -> None:
    """Cold reconcile: pre-answered HITL → ReconcileStep short-circuits → TOOL_RESULT in memory."""

    # ── IDs ─────────────────────────────────────────────────────────────────────
    session_id = "s1"
    task_id = "tsk_1"
    agent_id = "agt_1"
    tool_call_id = "tcA"

    # ── Shared memory + event bus ────────────────────────────────────────────────
    mem = InMemoryMemoryProvider()
    bus = InProcessEventBus()

    # ── CapabilityCache: seed the control tool capability ────────────────────────
    # The gateway looks up capability by tool name → ToolCapability → finds provider by cap.id prefix
    cap = ToolCapability(
        id=f"{PROVIDER_NAME}:ask_user",
        name="ask_user",
        kind="tool",
        description="Pause execution and request input or approval from a human.",
    )
    cache = CapabilityCache()
    cache.put(agent_id, [cap])

    # ── HitlManager + pre-answered HITL ─────────────────────────────────────────
    mgr = HitlManager(event_bus=bus)
    rid = await mgr.request(
        kind="input",
        session_id=session_id,
        task_id=task_id,
        tool_call_id=tool_call_id,
        question="Which DB?",
    )
    await mgr.answer(rid, "use postgres")
    # Confirm it's resolved already
    assert mgr.get(rid).status == "accepted"
    assert mgr.get(rid).message == "use postgres"

    # ── ControlCapabilityProvider: register the session ─────────────────────────
    provider = ControlCapabilityProvider(hitl_manager=mgr)
    session = Session(id=session_id, tenant_id="default", user_prompt="do it", status="RUNNING")
    task = Task(id=task_id, session_id=session_id, status="ACTIVE", title="T1")
    tm = SimpleNamespace(get_task=lambda tid: task, reopen_chain=None)
    provider.register_session(session_id, tm, session)

    # ── CapabilityGateway: use the SAME mem instance as LoopContext ──────────────
    gateway = CapabilityGateway(
        capability_cache=cache,
        capability_providers=[provider],
        memory=mem,          # <-- SAME instance as ctx.memory below
        event_bus=bus,
    )

    # ── LoopState + LoopContext ──────────────────────────────────────────────────
    # template 必须在 extra：reconcile 现会先 resolve_and_bind（spec/07 §6 修复）——control provider
    # 据此把 ask_user 等控制工具绑入 cache,gateway.invoke 才找得到。
    from tests.integration.test_minimal_loop import make_echo_template

    scope = MemoryScope(session_id=session_id, task_id=task_id, agent_id=agent_id)
    agent = SimpleNamespace(id=agent_id, template_id="tmpl_a", session_id=session_id)
    state = LoopState(run_id="run_1", session=session, task=task, agent=agent, scope=scope,
                      extra={"template": make_echo_template()})

    pctx = ProviderContext(
        session_id=session_id,
        tenant_id="default",
        task_id=task_id,
        agent_id=agent_id,
    )
    ctx = LoopContext(
        assembler=None,
        llm=None,
        memory=mem,          # <-- SAME instance as gateway._memory above
        event_bus=bus,
        provider_ctx=pctx,
        capability_gateway=gateway,
        capability_cache=cache,
        capability_providers=[provider],
    )

    # ── Pre-ingest the assistant LLM_RESPONSE with the dangling tool_call ───────
    # ReconcileStep scans for LLM_RESPONSE events, finds tool_calls without matching TOOL_RESULT.
    await mem.ingest(
        MemoryEvent(
            type=MemoryEventType.LLM_RESPONSE,
            scope=scope,
            content="",
            timestamp=now_utc(),
            role="assistant",
            metadata={
                "tool_calls": [
                    {
                        "id": tool_call_id,
                        "name": "control__ask_user",
                        "input": {"questions": [{"question": "Which DB?"}]},
                    }
                ]
            },
        ),
        pctx,
    )

    # ── Run ReconcileStep ────────────────────────────────────────────────────────
    step = ReconcileStep()
    outcome = await step.execute(state, ctx)

    # 填完 dangling → prepare 重装 prompt（再 act 调 LLM）
    assert outcome.next_step == "prepare"

    # ── Assert: TOOL_RESULT written with the human's answer ──────────────────────
    results = await mem.recall_recent(
        scope=scope,
        types=[MemoryEventType.TOOL_RESULT],
        limit=10,
        ctx=pctx,
    )
    assert any(
        r.metadata.get("tool_call_id") == tool_call_id and "use postgres" in r.content
        for r in results
    ), f"Expected TOOL_RESULT for {tool_call_id!r} with 'use postgres'; got: {results}"

    # ── Assert: no NEW pending HITL was created (cold short-circuit worked) ──────
    assert mgr.list_pending() == [], f"Expected no pending HITL; got: {mgr.list_pending()}"
