"""Schema extraction must unwrap Optional[T] / T | None to the inner JSON type.

Regression: Annotated[int | None, ...] was advertised to the LLM as type "string"
(union fell through _PY_TO_JSON), so models sent "3" for integer params and tools
crashed on arithmetic (e.g. grep before_context). See _parse_annotated.
"""

from typing import Annotated, Optional

from ctx_weft.core.utils import _parse_annotated, extract_schema


def test_optional_int_is_integer():
    assert _parse_annotated(Annotated[int | None, "d"]) == ("integer", "d")


def test_optional_via_typing_optional_is_integer():
    assert _parse_annotated(Annotated[Optional[int], "d"]) == ("integer", "d")


def test_optional_float_is_number():
    assert _parse_annotated(Annotated[float | None, ""])[0] == "number"


def test_optional_bool_is_boolean():
    assert _parse_annotated(Annotated[bool | None, ""])[0] == "boolean"


def test_optional_list_is_array():
    assert _parse_annotated(Annotated[list[str] | None, ""])[0] == "array"


def test_optional_dict_is_object():
    assert _parse_annotated(Annotated[dict[str, int] | None, ""])[0] == "object"


def test_plain_int_still_integer():
    assert _parse_annotated(Annotated[int, "x"]) == ("integer", "x")


def test_extract_schema_grep_context_is_integer():
    from ctx_weft.providers.capability_filesystem.provider import _FS_IMPLS

    schema = extract_schema(_FS_IMPLS["grep"])
    props = schema["properties"]
    for name in ("context", "before_context", "after_context"):
        assert props[name]["type"] == "integer", name
