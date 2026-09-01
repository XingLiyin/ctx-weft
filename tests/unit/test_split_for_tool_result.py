"""provider 经 CONTENT_PARTS_KEY 回传时要把文本与非文本 part 分开，这是那个拆分器。"""

from ctx_weft.core.content import split_for_tool_result
from ctx_weft.protocols import ImagePart, TextPart


def _img(n: int = 1) -> ImagePart:
    return ImagePart(data=f"blob:{n:064x}", media_type="image/png", source_type="ref")


def test_str_passes_through_as_same_object():
    """纯文本零开销：返回的就是传进去的那个对象（Global Constraint 第一条）。"""
    s = "hello"
    text, parts = split_for_tool_result(s)
    assert text is s
    assert parts == []


def test_none_becomes_empty_text():
    assert split_for_tool_result(None) == ("", [])


def test_empty_list_becomes_empty_text():
    assert split_for_tool_result([]) == ("", [])


def test_text_parts_are_concatenated_images_collected():
    content = [TextPart(text="see "), _img(7), TextPart(text="this")]
    text, parts = split_for_tool_result(content)
    assert text == "see this"
    assert [p.data for p in parts] == [_img(7).data]


def test_image_only_content_yields_empty_text():
    text, parts = split_for_tool_result([_img(3)])
    assert text == ""
    assert len(parts) == 1


def test_part_order_is_preserved():
    a, b = _img(1), _img(2)
    _, parts = split_for_tool_result([a, TextPart(text="x"), b])
    assert [p.data for p in parts] == [a.data, b.data]
