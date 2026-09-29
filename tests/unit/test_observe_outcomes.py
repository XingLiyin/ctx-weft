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
        task_status="continue", act_recap="more needed",
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
        task_status="continue", act_recap="more needed", next_step_hint="do X", ctx=_ctx(t)
    )
    assert t.observer_outcome == "continue"
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
    assert t.observer_outcome == "continue"  # 护栏：无终稿 → 重试
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
    assert t.observer_outcome == "continue"  # 'active' 不在工具允许集


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
        assert v.task_outcome == "continue", reason
        assert v.act_recap == ""  # 不产合成摘要（用户裁定）
        assert t.status == "ACTIVE"  # 判决不写状态（Task 4：状态归 TM）
        # 机械判决只定结局、不写 task：原 _rule_observe 的 _apply_assessment 副作用已删。
        assert t.observer_outcome is None
        assert t.actor_done is False


def test_mechanical_verdict_plain_text_exit_is_continue_with_a_finish_reminder() -> None:
    """**2026-09-28 反转**：纯文本收尾（`normal`）从 success 改判 continue。

    机械判决**没有任何办法**知道那段正文有没有交付目标——它只看得见「这一段是怎么停的」。
    那它的安全默认就必须是「没完」：continue 可回头（retry），success 是终态、不可逆。同一条
    原则已在另外两处落地（`normalize_verdict` 认不出的值归 continue 而决不归 fail、
    `report_task_outcome` 的 success-without-outputs 护栏把 success 改判 continue）。

    于是「一个 task 怎么才算成功」收敛成一句话：**actor 明确调了 `finish_task`**。

    判 continue 就得给下一轮一个「该干什么不一样的事」，否则它只会把同一段正文再写一遍——
    所以这一支必须带 hint。
    """
    t = _task()
    v = ObserveStep()._mechanical_verdict(_state("normal", t))
    assert v.task_outcome == "continue"
    assert v.act_recap == ""
    assert "finish_task" in v.next_step_hint, "判 continue 却没告诉它去调 finish_task"
    assert t.status == "ACTIVE"  # 判决不写状态（Task 4：状态归 TM）
    assert t.observer_outcome is None
    # hint 也不由判决自己写进 task——那是 `execute` 的事（见
    # test_mechanical_continue_lands_the_finish_reminder_on_the_task）。
    assert t.next_step_hint is None


def test_mechanical_verdict_actor_done_is_success() -> None:
    """唯一还判 success 的一格：actor 明确调了 `finish_task`。

    注意这一格仍然**无条件**——不看 `task.outputs`、不看子任务成败。那道
    success-without-outputs 护栏长在 `report_task_outcome` 里，机械判决不经过它。
    """
    t = _task()
    v = ObserveStep()._mechanical_verdict(_state("actor_done", t))
    assert v.task_outcome == "success"
    assert v.next_step_hint == "", "终态判决不该给下一轮留转向"


def test_mechanical_verdict_no_transcript_is_fail() -> None:
    t = _task()
    state = SimpleNamespace(transcript=[], act_exit_reason="normal", task=t)
    v = ObserveStep()._mechanical_verdict(state)
    assert v.task_outcome == "fail"
    assert v.act_recap == ""
    assert t.status == "ACTIVE"  # 判决不写状态（Task 4：状态归 TM）


# ── _should_use_llm gate tests ────────────────────────────────────────────────


def _llm_gate_state(exit_reason: str, parent_task_id, has_role: bool = True):
    # 正文非空才算「有 ROLE」（与装配层同口径，见 `has_observe_role`）——空正文的 facet
    # 在 composer 那边会被当成不存在、改用框架兜底文案。
    identity = {"observe": SimpleNamespace(text="ROLE")} if has_role else {}
    return SimpleNamespace(
        extra={"template": SimpleNamespace(identity=identity)},
        act_exit_reason=exit_reason,
        task=SimpleNamespace(parent_task_id=parent_task_id),
    )


def test_max_turns_forces_llm_even_for_root() -> None:
    s = _llm_gate_state("max_turns", None, has_role=True)
    assert ObserveStep()._should_use_llm(s) is True


def test_root_normal_exit_is_judged_by_the_llm() -> None:
    """**2026-09-28 反转**：root 的正常收尾从此也过 LLM observer。

    此前这里断言 `is False`（`_should_use_llm` 对 `parent_task_id is None` 降级走机械判决），
    而 `_mechanical_verdict` 把 `normal`/`actor_done` **无条件**映射成 success——那道
    success-without-outputs 护栏长在 `report_task_outcome` 里、不在机械判决的路上。于是 root
    上「这个 task 到底完没完」全凭 actor 自己说，零复核。

    不对称到了荒谬的程度：纯文本回合（S5，`plain_text` 边界）要被判、让位的 finish_task
    （S-b，`finish_park` 边界）要被判，唯独**不让位的那条收尾**——也就是 actor 明确宣布
    完成的那一次——不被判。等于给模型留了一个能绕开复核的开关。
    """
    s = _llm_gate_state("normal", None, has_role=True)
    assert ObserveStep()._should_use_llm(s) is True


def test_root_actor_done_is_judged_by_the_llm() -> None:
    """同上，`finish_task` 收尾那一格（`act_exit_reason == "actor_done"`）。

    这一格只有在**不让位**时才走到 observe：有人值守的 root 调 `finish_task` 会在 act 里就
    park（S-b，`boundary="finish_park"`），压根到不了这里。所以这条覆盖的是 unattended root
    与没有 hitl provider 的部署——恰恰是此前那个零复核洞的全部栖息地。
    """
    s = _llm_gate_state("actor_done", None, has_role=True)
    assert ObserveStep()._should_use_llm(s) is True


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
    assert Verdict(task_outcome="continue", act_recap="r").task_summary == ""


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


def test_the_observer_fallback_carries_the_judgement_criteria() -> None:
    """模板没有任何 ROLE facet 时的兜底身份，必须仍带着那条最要紧的准则。

    这条测试**取代**了原先那个 `test_default_role_prompt_uses_two_fields`：它从 core 去读
    `Loome-02/resources/agents/default/ROLE.md`，而那份 ROLE 自两仓拆分起就住在 host 仓，
    路径失效、长期红着。ROLE 的守护搬去 host（它是那些文件的主人），core 这边守自己的兜底。

    为什么兜底不能只有一句自我介绍（2026-09-28 的新分工）：「怎么判」整段归 ROLE，cue 只说
    「这一次做什么」、schema 只说「字段是什么」——那么没有 ROLE 的 agent 就等于什么判断准则都
    没有。而「向用户要东西一律 continue」这条**没有任何机械护栏**兜着，只能靠文字。
    """
    from ctx_weft.core.assembler.composer import _OBSERVER_ROLE_JUDGE_FALLBACK as fb

    assert "never re-execute" in fb and "never decide on the actor's behalf" in fb
    assert "is not evidence" in fb, "证据准则丢了"
    assert "`continue`: never `success`, never `fail`" in fb, "「在问人就不是完成」这条丢了"
    # 字段语义不在这里（那在工具 schema 上），别把 ROLE 写成第二份契约。
    for leaked in ("## Progress So Far", "whichever comes later", "First person"):
        assert leaked not in fb, f"兜底身份复述了字段语义：{leaked!r}"


def test_the_recap_fallback_carries_no_judgement_at_all() -> None:
    """只摘要档的兜底身份**不得**谈判决（2026-09-28 从判定版拆出来）。

    这一档的工具面里没有判决工具、schema 里没有任何字段收 `continue`/`success`/`fail`。ROLE
    位上要求它判，就是 prompt 命令模型做工具面不允许的事——三面分工那一轮刚消灭的形状。而且
    这一档正是「没有 ROLE 的 agent」唯一到得了的观察路径，兜底文案在这里最要紧。
    """
    from ctx_weft.core.assembler.composer import _OBSERVER_ROLE_RECAP_FALLBACK as fb

    assert "never re-execute" in fb and "never decide on the actor's behalf" in fb
    for leaked in ("judge", "`continue`", "`success`", "`fail`", "task_status"):
        assert leaked not in fb, f"只摘要档的兜底身份谈了判决：{leaked!r}"



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
        "ctx_weft.core.loop.background.launch_recap", _fake
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
    """root 在 close 边界 launch 过 → 机械路径不再重复 launch（重复虽有 per-task 锁兜着，
    但会多发一对 TaskRecapStarted/Done）。

    `normal` 自 2026-09-28 起不再是 close 边界：机械判决判它 continue，close 那一支要求
    `verdict != continue`，于是它落到 `mechanical`。仍然只 launch 一次——本用例守的就是「一次」。
    """
    for reason, expected in [("normal", "mechanical"), ("actor_done", "finish")]:
        launched = _capture_launches(monkeypatch)
        state, ctx = _mech_state_ctx(exit_reason=reason, parent_task_id=None)
        await ObserveStep().execute(state, ctx)
        assert launched == [expected], reason


@pytest.mark.asyncio
async def test_mechanical_verdict_full_mapping_through_execute(monkeypatch) -> None:
    """整张表走一遍 `execute`（含那道 max_turns 强制覆盖）：只有 `actor_done` 判 success。"""
    _capture_launches(monkeypatch)
    for exit_reason, expected in [
        ("max_turns", "continue"), ("context_limit", "continue"),
        ("normal", "continue"),          # 2026-09-28：纯文本不算收尾
        ("actor_done", "success"),       # 唯一的 success
    ]:
        state, ctx = _mech_state_ctx(exit_reason=exit_reason)
        outcome = await ObserveStep().execute(state, ctx)
        assert outcome.state_patch["verdict"].task_outcome == expected, exit_reason

    state, ctx = _mech_state_ctx(transcript=[])
    outcome = await ObserveStep().execute(state, ctx)
    assert outcome.state_patch["verdict"].task_outcome == "fail"


@pytest.mark.asyncio
async def test_mechanical_continue_lands_the_finish_reminder_on_the_task(monkeypatch) -> None:
    """纯文本判 continue 时，那句提醒必须真的落到 `task.next_step_hint`。

    渠道全在：`act_guidance` 把它渲染成「## Note from the review of your previous attempt」，
    `prepare` 在消费点清掉（一次性）。判决只把 hint 带出来，落地是 `execute` 的事——所以这条
    钉的是那一步接上了，光有 `Verdict.next_step_hint` 不算。
    """
    _capture_launches(monkeypatch)
    state, ctx = _mech_state_ctx(exit_reason="normal")
    assert state.task.next_step_hint is None
    await ObserveStep().execute(state, ctx)
    hint = state.task.next_step_hint or ""
    assert "finish_task" in hint, f"提醒没落到 task 上，实得 {hint!r}"


@pytest.mark.asyncio
async def test_mechanical_success_does_not_leave_a_hint(monkeypatch) -> None:
    """`actor_done` 判 success 时不许留转向——task 已经结束，下一轮不存在。

    反面对照：没有它，上面那条用「恒写 hint」也能过。
    """
    _capture_launches(monkeypatch)
    state, ctx = _mech_state_ctx(exit_reason="actor_done")
    await ObserveStep().execute(state, ctx)
    assert not state.task.next_step_hint


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
    # 判决就是机械映射对 `normal` 的那一格（2026-09-28 起 continue）。它**不会**让已取消的
    # task 被重排：driver 在每个 step 边界都查 token 并抛 `CancelledError`
    # （driver.py 的 `raise_if_cancelled`），observe 之后那次检查先于 finalize 命中，
    # run_outcome 落 CANCELED、task 落 CANCELED，这份 verdict 到不了 `disposition_for`。
    assert outcome.state_patch["verdict"].task_outcome == "continue"
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
