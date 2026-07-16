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
import dataclasses as _dc
from ctx_weft.core.loop.steps import compact as cm
from ctx_weft.core.loop.steps.compact import COLLAPSE_DELIM
from ctx_weft.core.orchestrator.lifecycle_manager import LifecycleManager
from ctx_weft.core.orchestrator.task_manager import TaskManager
from ctx_weft.core.orchestrator.control_capability import ControlCapabilityProvider
from ctx_weft.core.state.models import LoopGuard, Session, Task
from ctx_weft.core.utils import now_utc
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
    # 本测试钉「retry 段折」路径本体：关短段免折门（mock 段仅几 token，
    # 默认阈值 400 下会免折保 raw——那是另一条已单测的路径）。
    tpl = _dc.replace(_act_only_template(),
                      loop_config=LoopConfig(short_segment_token_threshold=0))
    resolver.register(tpl)
    # context_limit=20 → 0.8*20=16 tokens 阈值，真实 prompt 必超 → act turn1 context_limit 命中
    # output_reserve=0：effective_limit 不为 reserved_output_tokens 吞光（Task 4 引入）
    llm = MockLLMAdapter(responses=[MockResponse(text="partial work, not done yet")],
                         context_limit=20, output_reserve=0)
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


async def test_multiround_retry_accumulates_then_l3_collapses_e2e(monkeypatch):
    """真 runtime 多轮 retry：每轮 context_limit 命中 → observe 折段（累积，不替换）→ 下一轮
    prepare 预算门开 → 段摘要攒够(> collapse_keep_last) → L3 坍缩出带 COLLAPSE_DELIM 的 USER_PROMPT。
    钉住「已压缩会话中再压缩」整链：坍缩摘要打桩免真实 LLM，collapse/accumulate 全走真实路径。
    旧行为（段摘要替换 / L3 守卫只数 raw）下 L3 永不触发 → 无 COLLAPSE_DELIM → 本测试失败。"""
    async def _fake_summ(state, ctx, *, scope="task"):
        return "坍缩执行摘要"
    monkeypatch.setattr(cm, "summarize_for_compact", _fake_summ)

    resolver = InMemoryTemplateResolver()
    tpl = _act_only_template()
    tpl = _dc.replace(tpl, id="tpl_mr", loop_config=LoopConfig(
        collapse_keep_last=2, compact_target_ratio=0.01,
        short_segment_token_threshold=0))  # 关短段免折门：mock 段极小，留门则段摘要永不累积
    resolver.register(tpl)
    llm = MockLLMAdapter(responses=[MockResponse(text="partial work, not done yet")] * 30,
                         context_limit=20, output_reserve=0)
    runtime = CtxWeftRuntime(llm=llm, template_resolver=resolver)
    mem = InMemoryMemoryProvider()
    runtime.providers.register_memory(mem)

    # 复刻 run_single_task 脚手架，但在同一 task 上循环 _execute_task（同 scope → 段摘要累积）
    sid = "ses_mr"
    pctx = ProviderContext(session_id=sid, tenant_id="default")
    lm = LifecycleManager(template_resolver=resolver)
    agent, template = await lm.instantiate_agent(
        template_id="tpl_mr", session_id=sid, tenant_id="default", ctx=pctx)
    session = Session(id=sid, user_prompt="do a long task", status="RUNNING",
                      tenant_id="default", root_agent_id=agent.id, llm_provider="",
                      created_at=now_utc())
    session.context_limit = llm.context_limit
    session.reserved_output_tokens = llm.output_reserve
    agent = _dc.replace(agent, loop_guard=LoopGuard(
        context_limit=session.context_limit,
        reserved_output_tokens=session.reserved_output_tokens))
    task = Task(id="tsk_mr", session_id=sid, status="ACTIVE", tenant_id="default",
                assigned_agent_id=agent.id, creator_agent_id=agent.id, title="User Request",
                description="do a long task", user_prompt="do a long task", created_at=now_utc())
    tm = TaskManager(session_id=sid, event_bus=runtime._event_bus,
                     max_concurrent=1, task_max_retries=99)
    tm.set_session(session)
    tm.register_task(task)
    for p in runtime.providers.get_capability_providers():
        if isinstance(p, ControlCapabilityProvider):
            p.register_session(sid, tm, session)
            break

    state = None
    for r in range(4):
        task.status = "ACTIVE"       # 复位（finalize 上一轮把它标成 PENDING/retry）
        task.retry_count = 0         # 由本循环掌控轮数，不让 max_retries 提前收尾
        state, _ = await runtime._execute_task(
            session=session, task=task, agent=agent, template=template,
            run_id=f"run{r}", memory=mem, task_manager=tm)
        assert state.verdict is not None and state.verdict.task_outcome == "retry"

    ups = await mem.recall_recent(state.scope, [T.USER_PROMPT], 100, pctx)
    collapsed = [u.content for u in ups if COLLAPSE_DELIM in u.content]
    assert collapsed, "多轮累积段摘要后 L3 应坍缩出带 COLLAPSE_DELIM 的 USER_PROMPT"
    assert all(c.count("do a long task") <= 1 for c in collapsed), "原始节有界，不嵌套膨胀"
    segs = await mem.recall_recent(state.scope, [T.TASK_COMPACT_SUMMARY], 100, pctx)
    assert 1 <= len(segs) <= 3, "L3 把累积段摘要控在 collapse_keep_last 量级"


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
