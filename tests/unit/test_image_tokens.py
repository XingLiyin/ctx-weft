from ctx_weft.core.utils import estimate_content_tokens, image_part_count, image_tokens
from ctx_weft.protocols import ImagePart, TextPart


def _img():
    return ImagePart(data="ZGF0YQ==", media_type="image/png")


def test_image_tokens_zero_for_plain_text():
    assert image_tokens("hello world") == 0


def test_image_tokens_zero_for_none_and_empty():
    assert image_tokens(None) == 0
    assert image_tokens("") == 0
    assert image_tokens([]) == 0


def test_image_tokens_zero_for_text_parts_only():
    assert image_tokens([TextPart(text="a"), TextPart(text="b")]) == 0


def test_image_tokens_counts_each_image():
    assert image_tokens([_img()]) == 1600
    assert image_tokens([TextPart(text="a"), _img(), _img()]) == 3200


def test_estimate_content_tokens_unchanged_for_plain_text():
    """重构 estimate_content_tokens 复用 image_tokens 后，纯文本口径必须不变。"""
    count = len  # 确定性计数，隔离 tokenizer 启发式
    assert estimate_content_tokens("hello", count=count) == 4 + 5


def test_estimate_content_tokens_still_counts_images():
    count = len
    got = estimate_content_tokens([TextPart(text="hi"), _img()], count=count)
    assert got == 4 + 2 + 1600


def test_image_part_count_zero_for_plain_text():
    assert image_part_count("hello world") == 0


def test_image_part_count_zero_for_none_and_empty():
    assert image_part_count(None) == 0
    assert image_part_count("") == 0
    assert image_part_count([]) == 0


def test_image_part_count_zero_for_text_parts_only():
    assert image_part_count([TextPart(text="a"), TextPart(text="b")]) == 0


def test_image_part_count_counts_each_image():
    assert image_part_count([_img()]) == 1
    assert image_part_count([TextPart(text="a"), _img(), _img()]) == 2


def test_image_tokens_ties_to_image_part_count():
    for content in (None, "", "hello", [], [TextPart(text="a")],
                    [_img()], [TextPart(text="a"), _img(), _img()]):
        assert image_tokens(content) == 1600 * image_part_count(content)
