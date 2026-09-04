"""observe 三态机 + report_task_outcome 枚举。

锁定：
- report_task_outcome 接受 success/retry/fail，写 task.observer_outcome（**不写
  task.status**：判决归 loop、状态归 TaskManager，见 task_disposition.disposition_for）
- 护栏：success 但无 outputs → 降级 retry
- 非法值（含旧 active/ask_human）→ retry
- ObserveStep 规则降级：机械退出(max_turns/context_limit) → retry；正常退出 → success
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from ctx_weft.core.loop.steps.observe import ObserveStep
from ctx_weft.core.capabilities.control_tools import ControlContext, report_task_outcome
from ctx_weft.core.models.task import Task
from ctx_weft.protocols.events import EventType


def _task(**kw) -> Task:
    return Task(id="t1", session_id="s1", status="ACTIVE", **kw)


def _ctx(task: Task) -> ControlContext:
    return ControlContext(
        session_id="s1", task_id="t1", agent_id="a1", task=task,
        task_manager=None, session=None,
    )


# ── report_task_outcome 五态 ──────────────────────────────────────────────────


def test_assessment_success() -> None:
    t = _task(outputs="done")
    report_task_outcome(task_status="success", act_recap="ok", ctx=_ctx(t))
    assert t.observer_outcome == "success"
    assert t.status == "ACTIVE"  # 判决不写状态（Task 4：状态归 TM）


def test_assessment_fail() -> None:
    t = _task(outputs="x")
    report_task_outcome(
        task_status="fail", act_recap="bad", task_failure_reason="root cause", ctx=_ctx(t)
    )
    assert t.observer_outcome == "fail"
    assert t.status == "ACTIVE"  # 判决不写状态（Task 4：状态归 TM）
    assert t.error == "root cause"


def test_assessment_retry_records_blocker() -> None:
    """retry 判决的 task_failure_reason（本轮受阻原因）落 task.error——耗尽降级时即真死因。"""
    t = _task(outputs="partial")
    report_task_outcome(
        task_status="retry", act_recap="more needed",
        task_failure_reason="登录页有人机校验，自动化被拦", ctx=_ctx(t),
    )
    assert t.error == "登录页有人机校验，自动化被拦"
    assert t.status == "ACTIVE"  # 判决不写状态（Task 4：状态归 TM）


def test_assessment_success_clears_stale_blocker() -> None:
    """上一轮 retry 留下的受阻原因不得残留在 FINISHED 任务上。"""
    t = _task(outputs="done")
    t.error = "旧受阻原因"
    report_task_outcome(task_status="success", act_recap="ok", ctx=_ctx(t))
    assert t.error is None
    assert t.status == "ACTIVE"  # 判决不写状态（Task 4：状态归 TM）


def test_assessment_retry() -> None:
    t = _task(outputs="partial")
    report_task_outcome(
        task_status="retry", act_recap="more needed", next_step_hint="do X", ctx=_ctx(t)
    )
    assert t.observer_outcome == "retry"
    assert t.status == "ACTIVE"  # 判决不写状态（Task 4：状态归 TM）
    # 生命周期分离：act_recap 是永久记录（→ 段摘要 / finish 对），恒为纯复述；
    # next_step_hint 是只对下一次 attempt 有效的一次性转向 → 单独字段 → guidance（不入 memory）。
    assert t.process_report == "more needed", (
        f"process_report must stay a pure recap, no one-shot hint mixed in; got {t.process_report!r}"
    )
    # 保留 "Next Step Hint: " 标签：与护栏文案并存时同处一个 guidance 标题下，靠标签区分来源。
    assert t.next_step_hint == "Next Step Hint: do X"


def test_assessment_success_without_outputs_downgrades_to_retry() -> None:
    t = _task(outputs=None)
    report_task_outcome(task_status="success", act_recap="claims done", ctx=_ctx(t))
    assert t.observer_outcome == "retry"  # 护栏：无终稿 → 重试
    assert t.status == "ACTIVE"  # 判决不写状态（Task 4：状态归 TM）


def test_guardrail_hint_goes_to_next_step_hint_not_process_report() -> None:
    """success-without-outputs 护栏文案是给下一轮的一次性指令 → 落 next_step_hint，
    不得混进 act_recap（否则会随段摘要永久留在已完成任务的历史里）。"""
    t = _task(outputs=None)
    report_task_outcome(task_status="success", act_recap="claims done", ctx=_ctx(t))
    assert t.process_report == "claims done", (
        f"guardrail text must not pollute the permanent recap; got {t.process_report!r}"
    )
    assert t.next_step_hint and "final output" in t.next_step_hint


def test_next_step_hint_and_guardrail_combine_in_hint_field() -> None:
    """两处一次性文本（observer 的 hint + 护栏）并存时都落 next_step_hint，act_recap 仍纯净。"""
    t = _task(outputs=None)
    report_task_outcome(
        task_status="success", act_recap="claims done", next_step_hint="do X", ctx=_ctx(t)
    )
    assert t.process_report == "claims done"
    assert "do X" in t.next_step_hint and "final output" in t.next_step_hint


def test_no_hint_leaves_field_empty() -> None:
    """无一次性转向时 next_step_hint 为空——guidance 不出该段。"""
    t = _task(outputs="done")
    report_task_outcome(task_status="success", act_recap="ok", ctx=_ctx(t))
    assert not t.next_step_hint


def test_assessment_invalid_defaults_to_retry() -> None:
    t = _task(outputs="x")
    report_task_outcome(task_status="active", act_recap="r", ctx=_ctx(t))
    assert t.observer_outcome == "retry"  # 'active' 不在工具允许集


# ── ObserveStep 机械判决（原 _rule_observe，Task 4 起不产摘要）─────────────────


def _state(exit_reason: str, task: Task):
    return SimpleNamespace(
        transcript=[SimpleNamespace(tool_calls=[])],
        act_exit_reason=exit_reason,
        task=task,
    )


def test_mechanical_verdict_mechanical_exit_is_retry() -> None:
    for reason in ("max_turns", "context_limit"):
        t = _task()
        v = ObserveStep()._mechanical_verdict(_state(reason, t))
        assert v.task_outcome == "retry", reason
        assert v.act_recap == ""  # 不产合成摘要（用户裁定）
        assert t.status == "ACTIVE"  # 判决不写状态（Task 4：状态归 TM）
        # 机械判决只定结局、不写 task：原 _rule_observe 的 _apply_assessment 副作用已删。
        assert t.observer_outcome is None
        assert t.actor_done is False


def test_mechanical_verdict_normal_exit_is_success() -> None:
    t = _task()
    v = ObserveStep()._mechanical_verdict(_state("normal", t))
    assert v.task_outcome == "success"
    assert v.act_recap == ""
    assert t.status == "ACTIVE"  # 判决不写状态（Task 4：状态归 TM）
    assert t.observer_outcome is None


def test_mechanical_verdict_no_transcript_is_fail() -> None:
    t = _task()
    state = SimpleNamespace(transcript=[], act_exit_reason="normal", task=t)
    v = ObserveStep()._mechanical_verdict(state)
    assert v.task_outcome == "fail"
    assert v.act_recap == ""
    assert t.status == "ACTIVE"  # 判决不写状态（Task 4：状态归 TM）


# ── _should_use_llm gate tests ────────────────────────────────────────────────


def _llm_gate_state(exit_reason: str, parent_task_id, has_role: bool = True):
    identity = {"observe": SimpleNamespace()} if has_role else {}
    return SimpleNamespace(
        extra={"template": SimpleNamespace(identity=identity)},
        act_exit_reason=exit_reason,
        task=SimpleNamespace(parent_task_id=parent_task_id),
    )


def test_max_turns_forces_llm_even_for_root() -> None:
    s = _llm_gate_state("max_turns", None, has_role=True)
    assert ObserveStep()._should_use_llm(s) is True


def test_root_normal_exit_stays_rule() -> None:
    s = _llm_gate_state("normal", None, has_role=True)
    assert ObserveStep()._should_use_llm(s) is False


def test_no_role_stays_rule_even_at_max_turns() -> None:
    s = _llm_gate_state("max_turns", None, has_role=False)
    assert ObserveStep()._should_use_llm(s) is False


def test_delegated_normal_exit_uses_llm() -> None:
    s = _llm_gate_state("normal", "parent1", has_role=True)
    assert ObserveStep()._should_use_llm(s) is True


def test_verdict_has_act_recap_and_task_summary_fields():
    from ctx_weft.core.loop.steps.observe import Verdict
    v = Verdict(task_outcome="success", act_recap="did X", task_summary="whole journey")
    assert v.act_recap == "did X"
    assert v.task_summary == "whole journey"
    assert v.reported is False
    # task_summary 默认空
    assert Verdict(task_outcome="retry", act_recap="r").task_summary == ""


def test_task_model_has_task_summary_field():
    from ctx_weft.core.models.task import Task
    t = Task(id="t1", session_id="s1", status="ACTIVE")
    assert t.task_summary is None
    t.task_summary = "comprehensive"
    assert t.task_summary == "comprehensive"


def test_report_task_outcome_writes_act_recap_and_task_summary() -> None:
    task = _task(outputs="done")
    ctx = _ctx(task)
    report_task_outcome(
        task_status="success",
        act_recap="本轮我创建了 skill 文件并验证",
        task_summary="整段：看模板→写 SKILL.md→写脚本→验证，已就绪",
        ctx=ctx,
    )
    assert task.process_report == "本轮我创建了 skill 文件并验证"
    assert task.task_summary == "整段：看模板→写 SKILL.md→写脚本→验证，已就绪"
    assert task.observer_outcome == "success"


# ── persona prompt 守护 ───────────────────────────────────────────────────────


def test_default_role_prompt_uses_two_fields():
    import pathlib
    root = pathlib.Path(__file__).resolve().parents[3]  # ctx-weft/tests/unit → repo root
    for rel in ["resources/agents/default/ROLE.md",
                "packaging/default_data/agents/default/ROLE.md"]:
        text = (root / rel).read_text(encoding="utf-8")
        assert "act_recap" in text and "task_summary" in text, f"missing new fields in {rel}"
        assert "task_process_report" not in text, f"old field still present in {rel}"



# ── 非 LLM 路径：机械判决 + 转 background observe（Task 4）────────────────────


def _mech_state_ctx(*, exit_reason="normal", transcript=None, has_role=False,
                    parent_task_id="t0", cancelled=False):
    """ObserveStep.execute() 的最小搭台：默认「无 observe ROLE 的子任务」= 机械路径。"""
    from ctx_weft.core.control.tokens import CancelToken
    from ctx_weft.core.loop.driver import LoopContext, LoopState
    from ctx_weft.core.models.session import Session
    from ctx_weft.core.models.task import NormalTaskSettings
    from ctx_weft.protocols import MemoryAddress, ProviderContext
    from ctx_weft.protocols.template import AgentTemplate, IdentityFacet
    from ctx_weft.providers.llm.tokenizer import HeuristicTokenizer
    from ctx_weft.providers.memory.in_memory import InMemoryMemoryProvider

    task = Task(
        id="t1", session_id="s1", status="ACTIVE", assigned_agent_id="ag1",
        creator_agent_id="ag1", parent_task_id=parent_task_id,
        settings=NormalTaskSettings(),
    )
    template = None
    if has_role:
        template = AgentTemplate(
            id="tpl1", name="t", version="1.0.0",
            identity={"observe": IdentityFacet(text="ROLE")},
            capability_refs=[], memory_config=None, loop_config=None,
        )
    agent = SimpleNamespace(
        id="ag1",
        loop_config=SimpleNamespace(max_turns_per_observe=1, compact_keep_last=2,
                                    short_segment_token_threshold=0),
        runtime={"llm_model": "mock"},
        loop_guard=SimpleNamespace(context_limit=100_000, context_tokens=0),
    )
    state = LoopState(
        run_id="r1",
        session=Session(id="s1", tenant_id="default", user_prompt="hi", status="RUNNING"),
        task=task, agent=agent,
        scope=MemoryAddress(session_id="s1", task_id="t1", agent_id="ag1"),
        act_exit_reason=exit_reason,
        transcript=[SimpleNamespace(tool_calls=[])] if transcript is None else transcript,
        extra={"template": template},
        resolved_model=SimpleNamespace(model="mock", account=""),
    )

    class _Bus:
        async def emit(self, event):
            pass

    tok = CancelToken()
    if cancelled:
        tok.cancel()
    ctx = LoopContext(
        assembler=SimpleNamespace(),
        llm=SimpleNamespace(tokenizer=HeuristicTokenizer()),
        memory=InMemoryMemoryProvider(),
        event_bus=_Bus(),
        provider_ctx=ProviderContext(session_id="s1", tenant_id="default",
                                     task_id="t1", agent_id="ag1"),
        cancel_token=tok,
    )
    return state, ctx


def _capture_launches(monkeypatch) -> list[str]:
    launched: list[str] = []

    def _fake(state, ctx, *, boundary):
        launched.append(boundary)
        return None

    monkeypatch.setattr(
        "ctx_weft.core.loop.steps.background_observe.launch_background_observe", _fake
    )
    return launched


@pytest.mark.asyncio
async def test_non_llm_path_emits_no_synthetic_summary(monkeypatch) -> None:
    """机械判决路径不得产出任何合成摘要文本（用户裁定）。"""
    _capture_launches(monkeypatch)
    state, ctx = _mech_state_ctx()
    outcome = await ObserveStep().execute(state, ctx)
    assert outcome.state_patch["verdict"].act_recap == ""


@pytest.mark.asyncio
async def test_non_llm_path_launches_background_observe(monkeypatch) -> None:
    """摘要改由 background observe 产。"""
    launched = _capture_launches(monkeypatch)
    state, ctx = _mech_state_ctx()
    await ObserveStep().execute(state, ctx)
    assert launched == ["mechanical"], "非 LLM 路径必须转 background observe"


@pytest.mark.asyncio
async def test_close_boundary_not_double_launched(monkeypatch) -> None:
    """root + normal/actor_done 已在 close 边界 launch 过 → 机械路径不再重复 launch
    （重复虽有 per-task 锁兜着，但会多发一对 TaskRecapStarted/Done）。"""
    for reason, expected in [("normal", "normal"), ("actor_done", "finish")]:
        launched = _capture_launches(monkeypatch)
        state, ctx = _mech_state_ctx(exit_reason=reason, parent_task_id=None)
        await ObserveStep().execute(state, ctx)
        assert launched == [expected], reason


@pytest.mark.asyncio
async def test_mechanical_verdict_preserves_today_outcomes(monkeypatch) -> None:
    """判决逐字不变：机械退出 → retry；正常/actor_done → success；空 transcript → fail。"""
    _capture_launches(monkeypatch)
    for exit_reason, expected in [
        ("max_turns", "retry"), ("context_limit", "retry"),
        ("normal", "success"), ("actor_done", "success"),
    ]:
        state, ctx = _mech_state_ctx(exit_reason=exit_reason)
        outcome = await ObserveStep().execute(state, ctx)
        assert outcome.state_patch["verdict"].task_outcome == expected, exit_reason

    state, ctx = _mech_state_ctx(transcript=[])
    outcome = await ObserveStep().execute(state, ctx)
    assert outcome.state_patch["verdict"].task_outcome == "fail"


@pytest.mark.asyncio
async def test_cancelled_run_skips_llm_observe(monkeypatch) -> None:
    """取消时不跑 LLM ReAct，走机械判决 + background observe（observe 本身不中止）。"""
    called = False

    async def _never(*a, **kw):
        nonlocal called
        called = True
        raise AssertionError("must not run LLM observe after cancel")

    monkeypatch.setattr(ObserveStep, "_llm_observe", _never)
    launched = _capture_launches(monkeypatch)
    state, ctx = _mech_state_ctx(has_role=True, cancelled=True)
    outcome = await ObserveStep().execute(state, ctx)
    assert not called, "取消后不应再跑多轮 LLM observe"
    assert outcome.next_step == "finalize", "observe 仍须走完，不得中止"
    assert outcome.state_patch["verdict"].task_outcome == "success"
    assert launched == ["mechanical"], "取消是降级，不是中止——摘要仍交 background observe"


def test_no_mechanical_synthetic_summary_text_left_in_source() -> None:
    """硬裁定守卫：仓内不得再出现任何机械合成的摘要文本。"""
    import pathlib
    src = pathlib.Path(__file__).resolve().parents[2] / "src"
    banned = [
        "[No actor execution recorded]",
        "conversation round(s).",
        "Tools used:",
        "No tools were called.",
        "Task completed.",
    ]
    hits = [
        f"{p}: {phrase}"
        for p in src.rglob("*.py")
        for phrase in banned
        if phrase in p.read_text(encoding="utf-8")
    ]
    assert not hits, hits


def test_to_be_observed_is_gone() -> None:
    """死值域成员不该留在类型里（总账 D2）。"""
    import typing

    from ctx_weft.core.models.status import TaskStatus
    assert "TO_BE_OBSERVED" not in typing.get_args(TaskStatus)


@pytest.mark.asyncio
async def test_observe_emits_started_and_completed(monkeypatch) -> None:
    """observe 的起止成对，与后台 recap 的 TaskRecapStarted/Done 同形。"""
    _capture_launches(monkeypatch)
    state, ctx = _mech_state_ctx()
    outcome = await ObserveStep().execute(state, ctx)
    types = [e.type for e in outcome.events]
    assert EventType.OBSERVE_STARTED in types
    assert EventType.OBSERVE_COMPLETED in types
    assert types.index(EventType.OBSERVE_STARTED) < types.index(EventType.OBSERVE_COMPLETED)


@pytest.mark.asyncio
async def test_observe_started_payload_is_task_id_only(monkeypatch) -> None:
    """起点事件 payload 只放 task_id——是否用 LLM 在起点还没定，不能猜。"""
    _capture_launches(monkeypatch)
    state, ctx = _mech_state_ctx()
    outcome = await ObserveStep().execute(state, ctx)
    started = next(e for e in outcome.events if e.type == EventType.OBSERVE_STARTED)
    assert started.payload == {"task_id": state.task.id}
