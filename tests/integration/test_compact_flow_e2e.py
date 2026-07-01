"""Task 9: 全流程集成测试——覆盖机械退出 retry 段折 / 正常收尾回归 / 预算升级折真实记忆。

用真实 runtime（`run_single_task`）与真实 memory 驱动整条 prepare→act→observe→finalize。
"""

from types import SimpleNamespace
from datetime import datetime, timedelta, UTC
import pytest

from ctx_weft.core import CtxWeftRuntime
from ctx_weft.providers.llm.mock import MockLLMAdapter, MockResponse
from ctx_weft.providers.memory_blackboard import InMemoryMemoryProvider
from ctx_weft.protocols import (
    AgentTemplate, IdentityFacet, LoopConfig, MemoryConfig,
    MemoryEvent, MemoryEventType as T, MemoryScope, ProviderContext,
)
from ctx_weft.core.loop.steps import compact as cm
from tests.integration.test_minimal_loop import InMemoryTemplateResolver

pytestmark = pytest.mark.asyncio
_BASE = datetime(2026, 7, 1, tzinfo=UTC)


def _act_only_template() -> AgentTemplate:
    """act facet only（无 observe）→ 机械退出走规则 observe，不需脚本化 observe LLM。"""
    return AgentTemplate(
        id="tpl_actonly", name="actonly", version="0.1.0",
        identity={"act": IdentityFacet(text="You are a worker. Keep working on the task.")},
        description="e2e", capability_refs=[],
        memory_config=MemoryConfig(), loop_config=LoopConfig(),
    )


async def test_context_limit_retry_folds_segment_e2e():
    resolver = InMemoryTemplateResolver()
    resolver.register(_act_only_template())
    # context_limit=20 → 0.8*20=16 tokens 阈值，真实 prompt 必超 → act turn1 context_limit 命中
    llm = MockLLMAdapter(responses=[MockResponse(text="partial work, not done yet")],
                         context_limit=20)
    runtime = CtxWeftRuntime(llm=llm, template_resolver=resolver)
    runtime.providers.register_memory(InMemoryMemoryProvider())

    handle, state = await runtime.run_single_task(
        template_id="tpl_actonly", user_prompt="do a long task")

    # 机械退出 → retry（非终态），本轮 attempt 折成段摘要、raw 删除、USER_PROMPT 保留
    assert state.verdict is not None and state.verdict.task_outcome == "retry"
    assert state.task.status == "PENDING"
    assert not getattr(state.task, "process_report", None)  # retry 不写 process_report

    mem = runtime.providers.get_memory()
    pctx = ProviderContext(session_id=state.session.id, agent_id=state.agent.id)
    n_summary = await mem.count_recent(state.scope, [T.TASK_COMPACT_SUMMARY], pctx)
    n_user = await mem.count_recent(state.scope, [T.USER_PROMPT], pctx)
    n_raw = await mem.count_recent(state.scope, [T.LLM_RESPONSE], pctx)
    assert n_summary >= 1, "retry 应写至少一条 TASK_COMPACT_SUMMARY 段摘要"
    assert n_user >= 1, "USER_PROMPT 锚必须保留"
    assert n_raw == 0, "本轮 attempt 的 LLM_RESPONSE raw 应被折叠删除"


async def test_normal_finish_still_works_e2e():
    """正常收尾回归：整条 loop 仍跑通到 FINISHED（Task 3 去 process_report 渲染不破主流程）。"""
    resolver = InMemoryTemplateResolver()
    resolver.register(_act_only_template())
    llm = MockLLMAdapter(responses=[MockResponse(text="Here is the final answer.")])  # 正常 context_limit
    runtime = CtxWeftRuntime(llm=llm, template_resolver=resolver)
    runtime.providers.register_memory(InMemoryMemoryProvider())

    handle, state = await runtime.run_single_task(
        template_id="tpl_actonly", user_prompt="say hi")

    assert state.task.status == "FINISHED"
    assert state.verdict.task_outcome == "success"


async def test_escalating_compact_shrinks_real_memory(monkeypatch):
    """预算升级 compact 对真实 memory：seed 多个结束胶囊 → escalating_compact 至少折 agent 层，
    活跃记忆 token 下降；summarize_for_compact 打桩免真实 LLM。"""
    async def _fake_summ(state, ctx, *, scope="task"):
        return f"[summary-{scope}]"
    monkeypatch.setattr(cm, "summarize_for_compact", _fake_summ)

    mem = InMemoryMemoryProvider()
    scope = MemoryScope(session_id="s", task_id="root", agent_id="a")
    pctx = ProviderContext(session_id="s", tenant_id="tn")

    # seed 5 个结束顶层单元（finish 对：assistant + tool），parent=None → 顶层
    for i in range(5):
        oid = f"c{i}"
        await mem.ingest(MemoryEvent(type=T.AGENT_CONVERSATION_TURN, scope=scope,
            content="act_recap " + "x" * 200, timestamp=_BASE + timedelta(seconds=2 * i),
            role="assistant", metadata={"origin_task_id": oid, "parent_task_id": None,
            "tool_calls": [{"id": f"tc{i}", "name": "control:finish_task"}]}), pctx)
        await mem.ingest(MemoryEvent(type=T.AGENT_CONVERSATION_TURN, scope=scope,
            content="summary " + "y" * 200, timestamp=_BASE + timedelta(seconds=2 * i + 1),
            role="tool", metadata={"origin_task_id": oid, "parent_task_id": None,
            "tool_call_id": f"tc{i}"}), pctx)

    agent = SimpleNamespace(id="a", loop_config=SimpleNamespace(
        compact_keep_last=2, collapse_keep_last=3,
        compact_token_ratio=0.8, compact_target_ratio=0.01),  # target 极低 → 尽量升级
        loop_guard=SimpleNamespace(context_limit=100))
    session = SimpleNamespace(id="s", tenant_id="default")
    state = SimpleNamespace(run_id="run1", sequence_counter=0, scope=scope,
                            task=SimpleNamespace(id="root"), agent=agent,
                            session=session, extra={})
    ctx = SimpleNamespace(memory=mem, provider_ctx=pctx, event_bus=None, task_manager=None)

    before = await cm._active_memory_tokens(state, ctx)
    events = await cm.escalating_compact(state, ctx, token_estimate=before, trigger="compact")
    after = await cm._active_memory_tokens(state, ctx)

    # L1 agent 折触发（5 个顶层 > compact_keep_last=2）→ 活跃 token 下降
    assert after < before
    assert any(e.payload.get("layer") == "agent" and e.payload.get("source") == "root_experience"
               for e in events)
