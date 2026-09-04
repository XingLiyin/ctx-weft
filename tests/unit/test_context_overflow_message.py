from ctx_weft.core.models.errors import ContextOverflowError


def test_message_unchanged_when_no_images():
    """image_count 缺省时文案与改造前逐字符相同。"""
    err = ContextOverflowError(
        required=200_000, effective_limit=120_000,
        context_limit=128_000, reserved_output_tokens=8_192,
    )
    msg = str(err)
    assert msg == (
        "上下文超出模型可用窗口：保护槽位（角色设定 + 当前任务/消息）约 200000 tokens，"
        "已超过为输出预留后的可用窗口 effective_limit=120000"
        "（= 模型窗口 128000 − 输出预留 8192）。"
        "请改用更大上下文窗口的模型，或缩短当前消息 / 任务描述。"
    )
    assert "图片" not in msg


def test_message_mentions_images_when_present():
    err = ContextOverflowError(
        required=200_000, effective_limit=120_000,
        context_limit=128_000, reserved_output_tokens=8_192,
        image_count=12,
    )
    msg = str(err)
    assert "12 张图片" in msg
    assert "19200" in msg  # 12 * 1600
    assert err.image_count == 12


def test_explicit_message_still_wins():
    err = ContextOverflowError("custom", context_limit=128_000, image_count=3)
    assert str(err) == "custom"
