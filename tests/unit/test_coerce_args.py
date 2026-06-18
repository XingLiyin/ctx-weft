"""Gateway coerces string-valued args to their schema-declared scalar type.

Defense-in-depth: even with correct schemas, a model may emit "3" for an integer
param. The gateway coerces against cap.input_schema so the tool receives an int
instead of crashing on arithmetic.
"""

from ctx_weft.core.loop.capability_gateway import _coerce_args

_SCHEMA = {
    "type": "object",
    "properties": {
        "n": {"type": "integer"},
        "f": {"type": "number"},
        "b": {"type": "boolean"},
        "s": {"type": "string"},
    },
}


def test_string_int_coerced():
    assert _coerce_args({"n": "3"}, _SCHEMA) == {"n": 3}


def test_string_number_coerced():
    assert _coerce_args({"f": "2.5"}, _SCHEMA) == {"f": 2.5}


def test_string_bool_coerced():
    assert _coerce_args({"b": "true"}, _SCHEMA)["b"] is True
    assert _coerce_args({"b": "false"}, _SCHEMA)["b"] is False


def test_already_typed_untouched():
    assert _coerce_args({"n": 3, "f": 1.0, "b": True}, _SCHEMA) == {"n": 3, "f": 1.0, "b": True}


def test_string_param_untouched():
    assert _coerce_args({"s": "hello"}, _SCHEMA) == {"s": "hello"}


def test_uncoercible_left_as_is():
    # "abc" is not a valid integer → leave unchanged rather than raise
    assert _coerce_args({"n": "abc"}, _SCHEMA) == {"n": "abc"}


def test_unknown_key_left_as_is():
    assert _coerce_args({"zzz": "9"}, _SCHEMA) == {"zzz": "9"}


def test_empty_schema_is_passthrough():
    assert _coerce_args({"n": "3"}, {}) == {"n": "3"}
    assert _coerce_args({"n": "3"}, None) == {"n": "3"}
