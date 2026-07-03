"""_build_act_guidance: session task tree + finish/switch/ask_user sections."""

from __future__ import annotations

from types import SimpleNamespace

from ctx_weft.core.loop.steps.act import _build_act_guidance


def _task(id, title="", status="PENDING", parent=None, description="", created_at=None,
          user_prompt=""):
    return SimpleNamespace(
        id=id, title=title, description=description, status=status,
        parent_task_id=parent, created_at=created_at, user_prompt=user_prompt,
    )


def _state(title="", description="", mode="auto", id="t1"):
    return SimpleNamespace(
        task=SimpleNamespace(
            id=id, title=title, description=description,
            interaction_mode=mode, user_prompt="",
        )
    )


def _ctx(tasks=None):
    if tasks is None:
        return SimpleNamespace(task_manager=None)
    return SimpleNamespace(task_manager=SimpleNamespace(all_tasks=lambda: tasks))


def test_no_task_manager_omits_tree_but_keeps_finish():
    g = _build_act_guidance(_state(title="T"), _ctx())
    assert "## The overall plan" not in g
    assert "control__finish_task" in g
    assert "final reply to the user" in g


def test_lone_root_still_shows_itself_in_tree():
    # root 独自 act（唯一非终态 task）：仍出树、列自己一行，但表头不提「别做其他任务」。
    cur = _task("t1", "Current", "ACTIVE")
    g = _build_act_guidance(_state(title="Current", id="t1"), _ctx(tasks=[cur]))
    assert "## The overall plan (▶ = your current task):" in g
    assert "- [ACTIVE] ▶ Current" in g
    assert "do NOT do them yourself" not in g
    assert "Do not start the other tasks yourself." not in g
    assert "control__finish_task" in g


def test_no_open_tasks_omits_tree():
    # 全部终态（含当前）时无非终态节点 → 不出树，但完成方式仍在。
    cur = _task("t1", "Current", "FINISHED")
    g = _build_act_guidance(_state(title="Current", id="t1"), _ctx(tasks=[cur]))
    assert "## The overall plan" not in g
    assert "control__finish_task" in g


def test_current_task_title_description_not_duplicated_here():
    # composer 的 ## Current Task 框承载 title/description；act guidance 不再重复渲染。
    g = _build_act_guidance(_state(title="T", description="D"), _ctx())
    assert "## Your current task" not in g
    assert "Title: T" not in g
    assert "Description: D" not in g


def test_ask_user_reminder_present_in_all_modes():
    for mode in ("auto", "interactive"):
        g = _build_act_guidance(_state(title="T", mode=mode), _ctx())
        assert "control__ask_user" in g


def test_tree_lists_nonterminal_with_status_and_current_marker():
    cur = _task("t1", "Write report", "ACTIVE")
    sib = _task("t2", "Publish", "PENDING")
    g = _build_act_guidance(_state(id="t1"), _ctx(tasks=[cur, sib]))
    assert "## The overall plan" in g
    assert "- [ACTIVE] ▶ Write report" in g
    assert "- [PENDING] Publish" in g
    assert "do NOT do them yourself" in g
    assert "Do not start the other tasks yourself." in g


def test_terminal_tasks_excluded_from_tree():
    cur = _task("t1", "CURTASK", "ACTIVE")
    tasks = [
        cur,
        _task("t2", "DONETASK", "FINISHED"),
        _task("t3", "FAILTASK", "FAILED"),
        _task("t4", "CANCTASK", "CANCELED"),
        _task("t5", "PENDTASK", "PENDING"),
    ]
    g = _build_act_guidance(_state(id="t1"), _ctx(tasks=tasks))
    assert "PENDTASK" in g
    assert "CURTASK" in g
    assert "DONETASK" not in g
    assert "FAILTASK" not in g
    assert "CANCTASK" not in g


def test_child_task_indented_under_parent():
    parent = _task("t1", "Parent", "SUSPENDED")
    child = _task("t2", "Child", "PENDING", parent="t1")
    g = _build_act_guidance(_state(id="t1"), _ctx(tasks=[parent, child]))
    assert "- [SUSPENDED] ▶ Parent" in g
    assert "  - [PENDING] Child" in g


def test_orphan_nonterminal_promoted_to_root():
    # parent 不在非终态集合里（终态/缺失）→ 提升到 root 层，不缩进、不丢失。
    cur = _task("t1", "Current", "ACTIVE")
    orphan = _task("t2", "Orphan", "PENDING", parent="tX")
    g = _build_act_guidance(_state(id="t1"), _ctx(tasks=[cur, orphan]))
    assert "- [PENDING] Orphan" in g
    assert "  - [PENDING] Orphan" not in g


def test_untitled_task_uses_start_prompt_as_label():
    # 无 title 的 task（如 root）用开启它的 prompt 首行作标签。
    cur = _task("t1", "Current", "ACTIVE")
    root = _task("tsk_01", "", "ACTIVE", user_prompt="Summarize the quarterly report\nand email it")
    g = _build_act_guidance(_state(id="t1"), _ctx(tasks=[cur, root]))
    assert "- [ACTIVE] Summarize the quarterly report" in g
    assert "(untitled" not in g


def test_untitled_task_long_prompt_truncated():
    cur = _task("t1", "Current", "ACTIVE")
    long_prompt = "x" * 200
    root = _task("tsk_01", "", "ACTIVE", user_prompt=long_prompt)
    g = _build_act_guidance(_state(id="t1"), _ctx(tasks=[cur, root]))
    assert ("x" * 80 + "…") in g
    assert ("x" * 81) not in g


def test_untitled_task_no_prompt_falls_back_to_id_prefix():
    cur = _task("t1", "Current", "ACTIVE")
    blank = _task("abcdef123456", "", "PENDING")
    g = _build_act_guidance(_state(id="t1"), _ctx(tasks=[cur, blank]))
    assert "(untitled abcdef)" in g


def test_interactive_mode_keeps_pause_note_with_tree():
    cur = _task("t1", "Cur", "ACTIVE")
    sib = _task("t2", "Other", "PENDING")
    g = _build_act_guidance(_state(mode="interactive", id="t1"), _ctx(tasks=[cur, sib]))
    assert "## The overall plan" in g
    assert "pauses the task and waits" in g
