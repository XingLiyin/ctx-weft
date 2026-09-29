"""observe 装配的尾部：任务锚 → 边界事实 → actor 产出 → cue（前台与后台共用一条路径）。

## 这次重构（2026-09-28）在钉什么

三面分工：**schema 答「字段是什么」，cue 答「这一次做什么」，ROLE 答「怎么判」**。改造前这三者
各写了一遍 `act_recap` 的作用域规则、一遍字段清单、一遍子任务指名规则——同一句话三处维护，而且
其中两处的工具名早已过期（指向一个不存在的 `collect_process_report`）。所以本文件既验「该有的
都在」，也验**「不该有的不在」**：cue 里不得再出现字段语义的复述。

另外两条是修缺陷：

- 边界事实句与「要不要注入 actor 产出」由**同一张表**（`_BOUNDARY_FACTS`）定，前台后台共用。
  此前两边是两个函数两套措辞，且都把「调了 `finish_task`」写死在标题里——于是 `normal` 边界
  （纯文本收尾，压根没调）会拿到一条自相矛盾的 prompt。
- 只摘要那一档不再说「不要判」：它的工具面里压根没有判决工具，那句话既多余，又曾与
  `report_task_outcome` 的「`task_status` 必填」直接冲突。
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from ctx_weft.core.assembler.composer import (
    _BOUNDARY_FACTS,
    CLOSE_BOUNDARIES,
    _OUTPUTS_BEARING_BOUNDARIES,
    DefaultComposer,
)
# 判定档名单的唯一真相源在 loop 侧（装配层改为只看 purpose，不再镜像这张表，2026-09-28）。
from ctx_weft.core.loop.background.boundaries import JUDGING_BOUNDARIES


def _blocks():
    # 一条 identity facet（模拟 ROLE）+ 一条 user 历史，保证 _build_actor_messages 以 user 收尾
    from ctx_weft.core.assembler.assembler import ContextBlock
    return [
        ContextBlock(id="b0", source="identity", kind="identity", target="system",
                     content="ROLE-OBSERVE-BODY", priority=0, token_estimate=1, metadata={}),
        ContextBlock(id="b1", source="task_conversation", kind="history", target="messages",
                     content="原始诉求", priority=3, token_estimate=1,
                     metadata={"role": "user", "timestamp": "2026-01-01T00:00:00+00:00"}),
    ]


def _req(boundary, outputs="", *, purpose=None, title="理一遍工作目录",
         task_id="tsk_1", user_prompt="把工作目录理一遍"):
    # purpose 默认按 boundary 推——生产里这两者恒一致（`background_observe._run_recap`
    # 就是按 `_judges(boundary)` 选的 purpose）。装配层只看 purpose，手构 request 时让它们
    # 对齐，测的才是真实组合。
    if purpose is None:
        purpose = ("background_observe" if boundary in JUDGING_BOUNDARIES
                   else "background_recap")
    return SimpleNamespace(
        purpose=purpose,
        task=SimpleNamespace(
            id=task_id, user_prompt_in_memory=True, title=title, description="",
            user_prompt=user_prompt, outputs=outputs, process_report="",
            process_report_at=None, parent_task_id=None),
        session=SimpleNamespace(user_prompt=user_prompt),
        template=None, bound_capabilities=[],
        extra={"observe_boundary": boundary})


def _text(request) -> str:
    msgs = DefaultComposer()._build_observe_messages(_blocks(), request)
    return "\n".join(m.content for m in msgs if isinstance(m.content, str))


_FINISH_RESULT = "工作目录现状：仅一个 即兴演讲训练.pptx，无活跃项目。"
_ALL_BOUNDARIES = sorted(_BOUNDARY_FACTS)


# ── ① 任务锚定行 ──────────────────────────────────────────────────────────────


@pytest.mark.parametrize("boundary", _ALL_BOUNDARIES)
def test_every_boundary_gets_the_task_anchor(boundary: str) -> None:
    """**恒有**：observe 此前从来没有这道锚，而它的处境比 act 更糟——判定边界多发生在追问之后，
    那时 `## Current Task` 框远在历史深处。act 的 guidance 为此有一行恒有的锚定行，observe
    没有（guidance 是 act-only）。
    """
    assert "Current task: '理一遍工作目录' (tsk_1)" in _text(_req(boundary))


def test_the_anchor_falls_back_to_the_opening_message_without_a_title() -> None:
    """root task 在 `recognize_intent` 填完标题之前 `title` 恒空（`start_session` 建它时就空）
    ——那时退回开启它的 prompt 首行，与 act 共用同一份 `task_label`。
    """
    text = _text(_req("plain_text", title="", user_prompt="把工作目录理一遍\n第二行不该出现"))
    assert "Current task: '把工作目录理一遍' (tsk_1)" in text


def test_the_anchor_has_a_fallback_when_there_is_no_task_at_all() -> None:
    """手构 request / 无 id 的替身不得让装配崩——措辞与 act 的兜底逐字一致。"""
    assert "Current task: (as framed in the conversation above)" in _text(
        _req("plain_text", title="", task_id="", user_prompt=""))


def test_the_anchor_carries_no_description() -> None:
    """不带 description：那由 `## Current Task` 框承载，与 act 的取舍一致（锚只是锚）。"""
    req = _req("plain_text")
    req.task.description = "这段描述不该出现在锚定行里"
    anchor_line = _text(req).split("Current task:")[1].splitlines()[0]
    assert "这段描述" not in anchor_line


# ── ② 边界事实句：一张表，前台后台共用 ────────────────────────────────────────


@pytest.mark.parametrize("boundary", _ALL_BOUNDARIES)
def test_each_boundary_states_how_the_segment_ended(boundary: str) -> None:
    assert _BOUNDARY_FACTS[boundary][0] in _text(_req(boundary))


def test_the_normal_boundary_does_not_claim_a_finish_task_call() -> None:
    """回归（2026-09-28）：`normal` = 纯文本收尾，**没有** finish_task。

    此前两条注入段都把「closed out with finish_task」写死在标题里，于是这个边界的 prompt 一边说
    「这一段是用 finish_task 收的」、一边（边界描述）说「以最终产出正常结束」，而同一段话还命令
    「不要虚构没发生过的工具调用」。观察者很可能就把那次不存在的调用写进 `act_recap`，而那份
    recap 是要进记忆当段摘要的。
    """
    text = _text(_req("normal", outputs=_FINISH_RESULT))
    assert "did not call `finish_task`" in text
    assert "closed out with finish_task" not in text


def test_dispatch_is_not_described_as_a_normal_ending() -> None:
    """dispatch 段：父挂起等子任务，不是「正常结束」——不得落回 normal 的兜底文案。"""
    text = _text(_req("dispatch"))
    assert _BOUNDARY_FACTS["normal"][0] not in text
    assert "delegated" in text and "suspended" in text


def test_an_unknown_boundary_falls_back_to_normal() -> None:
    """未登记的边界不得让装配崩（恢复路径会传存量事件里的 boundary 字符串）。"""
    assert _BOUNDARY_FACTS["normal"][0] in _text(_req("something_new"))


# ── ③ 注入 actor 产出：只有以 finish_task 收尾的那几个 ────────────────────────


def test_the_injection_set_is_derived_from_the_one_table() -> None:
    """名单由表推出，不手写第二份——手写的那份必然与表漂。"""
    assert _OUTPUTS_BEARING_BOUNDARIES == frozenset(
        b for b, (_desc, inject) in _BOUNDARY_FACTS.items() if inject)
    assert _OUTPUTS_BEARING_BOUNDARIES == frozenset({"actor_done", "finish", "finish_park"})


@pytest.mark.parametrize("boundary", sorted(_OUTPUTS_BEARING_BOUNDARIES))
def test_finish_task_boundaries_inject_the_final_output(boundary: str) -> None:
    """`finish_task` 是 SILENT 工具，产出不写任务层对话——不喂进来，观察者只能虚构完成叙述。"""
    text = _text(_req(boundary, outputs=_FINISH_RESULT))
    assert "## Actor's Final Output" in text
    assert _FINISH_RESULT in text


@pytest.mark.parametrize(
    "boundary", sorted(set(_ALL_BOUNDARIES) - _OUTPUTS_BEARING_BOUNDARIES))
def test_other_boundaries_do_not_inject(boundary: str) -> None:
    """`plain_text` / `normal` 的产出**本身就是**一条 assistant 回合、在重建的对话里看得见，
    注入等于让它在同一个 prompt 里出现两遍；其余边界压根没有最终产出。
    """
    assert "## Actor's Final Output" not in _text(_req(boundary, outputs=_FINISH_RESULT))


def test_no_outputs_means_no_injection() -> None:
    assert "## Actor's Final Output" not in _text(_req("finish", outputs=""))


def test_the_reading_order_is_anchor_then_fact_then_output_then_cue() -> None:
    """顺序即阅读顺序：认清是哪个任务 → 这一段怎么结束的 → 它交了什么 → 你要做什么。"""
    text = _text(_req("finish_park", outputs=_FINISH_RESULT))
    assert (text.index("Current task:")
            < text.index(_BOUNDARY_FACTS["finish_park"][0])
            < text.index("## Actor's Final Output")
            < text.index("report_task_outcome"))


# ── ④ 判定档 vs 只摘要档 ──────────────────────────────────────────────────────


@pytest.mark.parametrize("boundary", sorted(JUDGING_BOUNDARIES))
def test_judging_boundaries_ask_for_the_verdict_tool(boundary: str) -> None:
    text = _text(_req(boundary))
    assert "control__report_task_outcome" in text
    assert "control__collect_process_report" not in text


@pytest.mark.parametrize(
    "boundary", sorted(set(_ALL_BOUNDARIES) - JUDGING_BOUNDARIES - {"actor_done"}))
def test_recap_boundaries_ask_for_the_recap_tool(boundary: str) -> None:
    """只摘要档拿的是另一个工具。`actor_done` 不在此列：它只作为**前台**边界出现，而前台恒判定。"""
    text = _text(_req(boundary))
    assert "control__collect_process_report" in text
    assert "control__report_task_outcome" not in text


def test_the_recap_cue_does_not_tell_it_not_to_judge() -> None:
    """那句「do not judge success/retry/fail」删了。

    它既多余（摘要档的工具面里压根没有判决工具），又曾**直接冲突**：桌面上唯一的工具把
    `task_status` 列为必填，模型只能违背其一。说「别判」还反过来暗示它有得选。
    """
    text = _text(_req("interrupt"))
    assert "do not judge" not in text
    assert "task_status" not in text


@pytest.mark.parametrize("boundary", sorted(CLOSE_BOUNDARIES))
def test_close_boundaries_also_ask_for_the_whole_task_summary(boundary: str) -> None:
    """close 边界是 `task_summary` 的**唯一**来源：root 的前台 observe 走机械判决、没有摘要，
    finish 对的 tool 槽只能靠这一档填。
    """
    assert "task_summary" in _text(_req(boundary, outputs=_FINISH_RESULT))


@pytest.mark.parametrize("boundary", ["interrupt", "dispatch", "mechanical"])
def test_non_close_recap_boundaries_do_not_ask_for_task_summary(boundary: str) -> None:
    """它们的 `task_summary` 没有消费者（那三条分支只读 `act_recap`），要它就是噪音。"""
    assert "task_summary" not in _text(_req(boundary))


def test_the_foreground_always_judges_whatever_the_boundary() -> None:
    """前台 observe 本身就是判决路径——boundary 只用来说事实与决定注入，不改「判不判」。"""
    text = _text(_req("normal", purpose="observe"))
    assert "control__report_task_outcome" in text
    assert "control__collect_process_report" not in text


# ── ⑤ 「向用户要东西 → 一律 continue」那句硬提醒 ───────────────────────────────


@pytest.mark.parametrize("boundary", sorted(JUDGING_BOUNDARIES))
def test_the_yielding_boundaries_carry_the_continue_reminder(boundary: str) -> None:
    """让位的两个边界是「把提问判成 success」的高发地，而那条判断**没有任何机械护栏**——
    success-without-outputs 护栏读 `task.outputs`，而 park 之前合成的 outputs 正是那段提问本身、
    非空，护栏原地失效（见 tests/unit/test_verdict_vocabulary.py 末条）。所以只能在生成点附近
    再顶一句。
    """
    assert "judge `continue`" in _text(_req(boundary))


@pytest.mark.parametrize(
    "boundary", sorted(set(_ALL_BOUNDARIES) - JUDGING_BOUNDARIES))
def test_other_boundaries_do_not_carry_the_reminder(boundary: str) -> None:
    """没人在等的边界顶这句是噪音（子任务的 finish_task、无人值守的收尾都没有「用户」在场）。"""
    assert "judge `continue`" not in _text(_req(boundary))


# ── ⑥ cue 不复述字段语义（这次重构的要点，容易被后来人「顺手补回来」）──────────


@pytest.mark.parametrize("boundary", _ALL_BOUNDARIES)
def test_the_cue_never_restates_a_field_contract(boundary: str) -> None:
    """字段「是什么」只在工具 schema 上写一份。

    这条是防回归的：改造前 `act_recap` 的作用域规则在 ROLE、cue、schema 里各写了一遍，谁改一处
    另两处就开始撒谎。cue 只说「这一次填哪些」，不说「这个字段是什么意思」。
    """
    text = _text(_req(boundary, outputs=_FINISH_RESULT))
    for leaked in ("## Progress So Far", "First person", "whichever comes later",
                   "do not restate anything before"):
        assert leaked not in text, f"{boundary} 的 cue 复述了字段语义：{leaked!r}"


def test_the_close_boundary_set_stays_in_sync_with_the_loop_side() -> None:
    """close 名单仍各写了一份（避免跨层 import），这里钉住它不漂。

    判定名单已不在此列：装配层 2026-09-28 起只看 `request.purpose`，那张镜像的
    `JUDGING_BOUNDARIES` 随之删除——「判不判」只剩 loop 侧一个真相源。
    """
    from ctx_weft.core.loop.background import (
        CLOSE_BOUNDARIES as loop_close,
    )

    assert CLOSE_BOUNDARIES == loop_close


# ── ⑦ 子任务指名清单只在判定档 ────────────────────────────────────────────────


def test_judging_lists_subtasks_for_the_hint() -> None:
    req = _req("plain_text")
    req.extra["subtasks"] = [{"task_id": "tsk_a", "title": "甲", "outcome": "failed"}]
    assert "tsk_a" in _text(req)


def test_recap_never_lists_subtasks() -> None:
    """只摘要档不产 `next_step_hint`，列出来是纯噪音。"""
    req = _req("interrupt")
    req.extra["subtasks"] = [{"task_id": "tsk_a", "title": "甲", "outcome": "failed"}]
    assert "tsk_a" not in _text(req)


# ── ⑧ 地基：observe 装配不依赖 observe ROLE ───────────────────────────────────


def _identity_blocks_for(template, purpose="background_observe"):
    """跑真 IdentitySource，拿它在给定 template 下实际产出的 blocks。"""
    import asyncio

    from ctx_weft.core.assembler.sources.identity import IdentitySource

    req = SimpleNamespace(purpose=purpose, template=template, extra={}, token_counter=len)

    async def _run():
        return [b async for b in IdentitySource().fetch(req, deps=None)]

    return asyncio.run(_run())


def _template(identity: dict):
    from ctx_weft.protocols.template import AgentTemplate, IdentityFacet

    return AgentTemplate(
        id="tpl1", name="t", version="1.0.0",
        identity={k: IdentityFacet(text=v) for k, v in identity.items()},
        capability_refs=[], memory_config=None, loop_config=None,
    )


@pytest.mark.parametrize("purpose", ["background_observe", "background_recap"])
def test_both_background_purposes_reuse_the_observe_role(purpose: str) -> None:
    """两档共用 observe 的 ROLE——判断准则与「这一次要不要判」无关。"""
    blocks = _identity_blocks_for(_template({"observe": "OBSERVE-SOUL"}), purpose)
    assert [b.content for b in blocks] == ["OBSERVE-SOUL"]


@pytest.mark.parametrize("purpose", ["background_observe", "background_recap"])
def test_background_identity_never_falls_back_to_the_actor_soul(purpose: str) -> None:
    """无 observe facet → **不产 identity block**（2026-09-28 反转）。

    回落 act 是这条路上最坏的一种「有总比没有好」：observer 的全部意义在于它不是 actor，
    顶着 actor 的 SOUL 判 actor 等于没判；而 system 提示本来就是同一段 SOUL
    （`_build_act_system`），回落还让它出现两遍。空着交给 `_OBSERVER_ROLE_JUDGE_FALLBACK`。
    """
    assert _identity_blocks_for(_template({"act": "ACT-SOUL-BODY"}), purpose) == []


def test_the_actor_soul_fallback_still_holds_for_the_actors_own_purposes() -> None:
    """compact / recognize_intent 缺 facet 时仍回落 act——它们是 actor 自己的内部工序。"""
    for purpose in ("compact", "recognize_intent"):
        blocks = _identity_blocks_for(_template({"act": "ACT-SOUL-BODY"}), purpose)
        assert [b.content for b in blocks] == ["ACT-SOUL-BODY"], purpose


def _joined(boundary: str) -> str:
    """没有任何 identity block 时装出来的整段 observe prompt。"""
    from ctx_weft.core.assembler.assembler import ContextBlock

    hist = [ContextBlock(id="b1", source="task_conversation", kind="history",
                         target="messages", content="原始诉求", priority=3, token_estimate=1,
                         metadata={"role": "user", "timestamp": "2026-01-01T00:00:00+00:00"})]
    msgs = DefaultComposer()._build_observe_messages(hist, _req(boundary))
    return "\n".join(m.content for m in msgs if isinstance(m.content, str))


def test_the_prompt_is_usable_with_no_identity_block_at_all() -> None:
    """连 act facet 都没有 → composer 用兜底身份，cue 仍完整产出。"""
    from ctx_weft.core.assembler.composer import _OBSERVER_ROLE_RECAP_FALLBACK

    assert _identity_blocks_for(_template({})) == []
    assert _identity_blocks_for(None) == []

    joined = _joined("normal")
    assert _OBSERVER_ROLE_RECAP_FALLBACK in joined
    assert "control__collect_process_report" in joined
    assert _BOUNDARY_FACTS["normal"][0] in joined


def test_the_two_fallback_identities_follow_the_tier_not_the_boundary() -> None:
    """兜底身份跟着档走：判定档给判定版，只摘要档给摘要版。

    两者必须与工具面一致——判定版通篇在讲怎么判，而只摘要档的桌面上没有判决工具，把它发过去
    就是在要求模型做工具面不允许的事。
    """
    from ctx_weft.core.assembler.composer import (
        _OBSERVER_ROLE_JUDGE_FALLBACK, _OBSERVER_ROLE_RECAP_FALLBACK,
    )

    judging = _joined("plain_text")      # → purpose=background_observe
    assert _OBSERVER_ROLE_JUDGE_FALLBACK in judging
    assert _OBSERVER_ROLE_RECAP_FALLBACK not in judging

    recap = _joined("interrupt")         # → purpose=background_recap
    assert _OBSERVER_ROLE_RECAP_FALLBACK in recap
    assert _OBSERVER_ROLE_JUDGE_FALLBACK not in recap
