"""_build_act_guidance: conditional task block + queued-tasks section."""

from __future__ import annotations

from types import SimpleNamespace

from ctx_weft.core.loop.steps.act import _build_act_guidance


def _state(title="", description="", mode="auto", user_prompt=""):
    return SimpleNamespace(
        task=SimpleNamespace(
            id="t1", title=title, description=description,
            interaction_mode=mode, user_prompt=user_prompt,
        )
    )


def _ctx(tasks=None):
    if tasks is None:
        return SimpleNamespace(task_manager=None)
    return SimpleNamespace(task_manager=SimpleNamespace(all_tasks=lambda: tasks))


def test_no_title_or_description_omits_task_block():
    g = _build_act_guidance(_state(), _ctx())
    assert "## Your current task" not in g
    assert "control__finish_task" in g


def test_user_prompt_fallback_when_no_title_or_description():
    g = _build_act_guidance(_state(user_prompt="please summarize the repo"), _ctx())
    assert "## Your current task" in g
    assert "This task was started by the user's request:" in g
    assert "please summarize the repo" in g
    # finish reminder present (unified finish section below the task block)
    assert "control__finish_task" in g
    assert "final reply to the user" in g


def test_ask_user_reminder_present_in_all_modes():
    for mode in ("auto", "interactive"):
        g = _build_act_guidance(_state(title="T", mode=mode), _ctx())
        assert "control__ask_user" in g


def test_no_successors_omits_queued_section_entirely():
    g = _build_act_guidance(_state(title="T", description="D"), _ctx())
    assert "No tasks are queued" not in g
    assert "Tasks queued after" not in g
    assert "Do not start the queued tasks" not in g
    assert "## Your current task" in g and "Title: T" in g and "Description: D" in g


def test_title_only_shows_task_block_without_description():
    g = _build_act_guidance(_state(title="T"), _ctx())
    assert "## Your current task" in g
    assert "Title: T" in g
    assert "Description:" not in g


def test_successors_listed_and_warned():
    succ = SimpleNamespace(id="t2", title="Next", description="do next",
                           tracking_task_ids=["t1"], created_at=None)
    g = _build_act_guidance(_state(title="T"), _ctx(tasks=[succ]))
    assert "## Tasks queued after this one" in g
    assert "Next" in g
    assert "Do not start the queued tasks yourself." in g
