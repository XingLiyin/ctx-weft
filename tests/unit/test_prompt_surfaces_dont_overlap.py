"""observer prompt 的三面**互不复述**。

observer 看到的 prompt 由三面拼成，分工是：

| 面 | 回答什么 | 住在哪 |
|---|---|---|
| 工具 schema | 字段**是什么**：语义、何时必填、写到哪儿去 | `capabilities/control_tools.py` |
| 尾部 cue | **这一次**：本段怎么结束的、调哪个工具、填哪几格 | `assembler/composer.py` |
| ROLE.md | **怎么判**：什么算完成、什么算证据、continue 与 fail 的分界 | **宿主仓**（业务方维护） |

2026-09-28 那轮改造把分工定下来了，但只拆了最明显的几处，一次事后审计又抓出四处残留 ——
本文件是那四处各自的守护。ROLE 那一面的守护在宿主仓
（`tests/test_default_observer_role.py`），本文件只管 core 这两面，外加两个跨仓标题契约。

**为什么非要钉**：这四处当时全是绿的 —— 唯一相关的那条守护
（`test_background_observe_prompt.py::test_the_cue_never_restates_a_field_contract`）只查四个
特定短语，全是 `act_recap` 语义的，「填写条件」那一类一个都没覆盖。文字重复不会报错、不会
崩，只会在某一天两处开始说不同的话，而那时谁都不知道该信哪个。
"""
from __future__ import annotations

import typing

import pytest

from ctx_weft.core.assembler import composer as C
from ctx_weft.core.capabilities import control_tools as CT
from ctx_weft.core.utils.headings import FINAL_OUTPUT_HEADING, SUBTASKS_HEADING


def _field_descs(fn) -> dict[str, str]:
    """工具函数每个 `Annotated` 参数的描述文本。"""
    target = getattr(fn, "__wrapped__", fn)
    hints = typing.get_type_hints(target, include_extras=True)
    out: dict[str, str] = {}
    for name, hint in hints.items():
        meta = getattr(hint, "__metadata__", ())
        if meta and isinstance(meta[0], str):
            out[name] = meta[0]
    return out


_OUTCOME_DESCS = _field_descs(CT.report_task_outcome)
_JUDGMENT_CUE = C._judgment_cue("plain_text")


def test_the_helper_actually_reads_the_schema() -> None:
    """前提：上面那个取描述的助手真的取到了东西。

    没有这条，下面每一条 `X not in desc` 都可能是在对空字符串做断言 —— 全绿且毫无意义。
    """
    assert set(_OUTCOME_DESCS) >= {
        "task_status", "act_recap", "task_summary", "task_failure_reason", "next_step_hint"}
    assert all(len(v) > 40 for v in _OUTCOME_DESCS.values())


# ── ① cue 不复述条件字段的填写条件 ────────────────────────────────────────────


def test_the_cue_names_only_the_always_required_fields() -> None:
    """cue 说「这一次填哪几格」，但**条件字段的条件**不在它这儿。

    改造前它把四个条件逐条列了一遍（`task_summary` 何时加、`task_failure_reason` 何时填、
    `next_step_hint` 何时填），然后紧跟一句「Each field's own contract is on the tool itself」
    —— 先抄一遍 schema，再说契约在 schema 上。`task_failure_reason` 因此在 cue / schema /
    ROLE 三处各有一份填写条件。

    正向：恒填的那两个仍要点名（模型得知道最少要给什么）。
    """
    assert "`task_status`" in _JUDGMENT_CUE
    assert "`act_recap`" in _JUDGMENT_CUE
    # 条件字段一个都不点名 —— 连名字都不提，就不会有「提了却说错条件」的机会
    for field in ("task_summary", "task_failure_reason", "next_step_hint"):
        assert field not in _JUDGMENT_CUE, f"cue 又开始复述 {field} 的填写条件了"


@pytest.mark.parametrize("condition", [
    "when the status is",       # task_summary 的条件
    "genuinely blocked",        # task_failure_reason 对 continue 的条件
    "needs a warning",          # next_step_hint 的条件
    "Each field's own contract",  # 「抄完再说契约在别处」那句自相矛盾的收尾
])
def test_the_cue_does_not_restate_a_fill_condition(condition: str) -> None:
    """逐条钉住那四句原文，防止有人「顺手加回来」。"""
    assert condition not in _JUDGMENT_CUE


def test_the_fill_conditions_do_live_on_the_schema() -> None:
    """另一半：删了 cue 那份，条件本身必须仍在 schema 上 —— 不然是删掉而不是搬走。"""
    assert "success" in _OUTCOME_DESCS["task_summary"]
    assert "genuinely blocked" in _OUTCOME_DESCS["task_failure_reason"]
    assert "LEAVE IT EMPTY" in _OUTCOME_DESCS["task_failure_reason"]


# ── ② schema 不写职权规则 ─────────────────────────────────────────────────────


@pytest.mark.parametrize("authority", [
    "do not decide what happens next",
    "chooses for itself",
    "delegate a fresh sub-task",
])
def test_the_hint_field_does_not_carry_the_authority_rule(authority: str) -> None:
    """「你不决定下一步」是 ROLE 的地盘。

    改造把字段契约从 ROLE 搬去 schema，结果 schema 里长出了一段**职权规则**，而 ROLE 已有一份
    近乎逐字的同款文本 —— 字段描述越界写职权规则，与「ROLE 越界写字段契约」是同一个病的两个
    方向。schema 只说「这个字段也承载标记子任务这件事」。
    """
    assert authority.lower() not in _OUTCOME_DESCS["next_step_hint"].lower()


def test_the_hint_field_still_says_what_it_carries() -> None:
    """删职权规则不等于删用途：指名子任务这件事仍归这个字段。"""
    desc = _OUTCOME_DESCS["next_step_hint"]
    assert "sub-task" in desc
    assert SUBTASKS_HEADING in desc, "指路的标题名必须来自共享常量，不散写字面量"


# ── ③ 让位边界的顶针：只留结论 ────────────────────────────────────────────────


@pytest.mark.parametrize("boundary", sorted(C._YIELDED_REMINDER))
def test_the_yielded_reminder_carries_the_whole_conclusion(boundary: str) -> None:
    """顶针必须把**两个否定都**说出来。

    改造前 `plain_text` 那句只说「judge continue」，没说「决不 fail」—— 而判 fail 比判 success
    更糟（success 只是丢掉一轮对话，fail 是把一个没出错的任务钉死）。
    """
    text = C._YIELDED_REMINDER[boundary]
    assert "`continue`" in text
    assert "never `success`" in text
    assert "never `fail`" in text


@pytest.mark.parametrize("boundary", sorted(C._YIELDED_REMINDER))
def test_the_yielded_reminder_does_not_re_enumerate(boundary: str) -> None:
    """列举归 ROLE，顶针只顶结论。

    改造前它把「问题 / 缺信息 / 选择 / 确认」四项照 ROLE 抄了一遍，却漏了 ROLE 的第五项
    （「只有用户能做的动作」）—— 抄一半是最容易漂的形态：ROLE 加第六项时没人会想到还要来改
    这里。这条也顺带保住顶针的短小，它处在 prompt 尾部 recency 最强的位置。
    """
    text = C._YIELDED_REMINDER[boundary]
    for enumerated in ("missing information", "a choice", "a confirmation"):
        assert enumerated not in text, f"{boundary} 的顶针又开始列举了：{enumerated!r}"
    assert len(text) < 200, f"{boundary} 的顶针涨到 {len(text)} 字符，八成又在抄 ROLE"


# ── ④ 「别想太多」只说一遍 ────────────────────────────────────────────────────


def test_call_promptly_is_said_exactly_once_across_the_two_surfaces() -> None:
    """同一个 prompt 里这句话只该出现一次。

    它是关于**这次调用**的指令，归 cue；不是字段语义，不归 schema。此前两处各一句。
    """
    surfaces = [_JUDGMENT_CUE, *_OUTCOME_DESCS.values()]
    hits = [s for s in surfaces if "over-think" in s.lower()]
    assert len(hits) == 1, f"「别想太多」出现了 {len(hits)} 次"
    assert "over-think" in _JUDGMENT_CUE.lower(), "留下来的那一份该在 cue 上"


# ── 跨仓标题契约：唯一真相源 ──────────────────────────────────────────────────


def test_the_injected_output_section_uses_the_shared_heading() -> None:
    """composer 渲染那一段时用的必须是共享常量，不是散写的字面量。

    这个标题名是**跨仓契约**：宿主仓的 ROLE.md 里有一句「产出由 trailing prompt 注入在这个
    标题下」。改了常量而不改 ROLE，ROLE 当场撒谎且不会有任何报错 —— 改造前 ROLE 里那个指向
    不存在工具的名字就是这样烂掉的。宿主侧的对侧断言在
    `tests/test_default_observer_role.py`。
    """
    class _T:
        outputs = "交付物正文"
        id = "tsk_1"

    class _R:
        task = _T()

    section = C._finish_result_section(_R())
    assert section.startswith(FINAL_OUTPUT_HEADING + "\n\n")
    # 变异防御：把常量改了，渲染出来的标题必须跟着变（而不是两处各一份字面量）
    assert FINAL_OUTPUT_HEADING in section
    assert section.count("Actor's Final Output") == 1
