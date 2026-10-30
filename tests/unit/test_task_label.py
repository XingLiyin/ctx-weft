from types import SimpleNamespace

import pytest

from ctx_weft.core.utils.task_ref import TASK_LABEL_MAX, task_label, task_label_parts
from ctx_weft.protocols import ImagePart, TextPart


@pytest.mark.parametrize(
    ("title", "prompt", "expected_title"),
    [
        ("", "修复登录问题\n保留现有接口", "修复登录问题"),
        (" ", "\n  Fix login\nKeep the API", "Fix login"),
        ("", [ImagePart(data="", media_type="image/png"),
                          TextPart(text="Describe this image")], "Describe this image"),
        ("", "x" * (TASK_LABEL_MAX + 1), "x" * TASK_LABEL_MAX + "…"),
        ("", None, ""),
        ("", [ImagePart(data="", media_type="image/png")], ""),
        ("", "Actual request", "Actual request"),
        ("Named task", "Actual request", "Named task"),
        ("User Message", "Actual request", "User Message"),
    ],
)
def test_task_labels_fall_back_to_the_request_only_when_untitled(title, prompt, expected_title):
    task = SimpleNamespace(id="tsk_1", title=title, user_prompt=prompt)
    expected = f"{expected_title!r} (tsk_1)" if expected_title else "tsk_1"

    assert task_label(task) == expected
    assert task_label_parts(task.id, title, prompt) == expected
    assert task.title == title
