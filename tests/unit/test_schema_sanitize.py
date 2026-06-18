"""Boolean JSON Schema sanitization for strict providers (e.g. DeepSeek V4)."""

from ctx_weft.protocols import LLMTool
from ctx_weft.providers.llm._schema import sanitize_boolean_schemas
from ctx_weft.providers.llm.openai import _map_tools as _openai_map_tools
from ctx_weft.providers.llm.anthropic import _map_tools as _anthropic_map_tools


def _tool():
    return LLMTool(
        name="t",
        description="",
        input_schema={"type": "object", "additionalProperties": True,
                      "properties": {"a": {"items": True}}},
    )


def test_openai_map_tools_sanitizes_schema():
    out = _openai_map_tools([_tool()])
    params = out[0]["function"]["parameters"]
    assert "additionalProperties" not in params
    assert params["properties"]["a"]["items"] == {}


def test_anthropic_map_tools_sanitizes_schema():
    out = _anthropic_map_tools([_tool()])
    schema = out[0]["input_schema"]
    assert "additionalProperties" not in schema
    assert schema["properties"]["a"]["items"] == {}


def test_boolean_schema_true_becomes_empty_object():
    assert sanitize_boolean_schemas({"items": True}) == {"items": {}}


def test_boolean_schema_false_becomes_not_empty():
    assert sanitize_boolean_schemas({"items": False}) == {"items": {"not": {}}}


def test_additional_properties_true_removed():
    assert sanitize_boolean_schemas({"additionalProperties": True}) == {}


def test_required_bool_removed():
    out = sanitize_boolean_schemas({"properties": {"x": {"type": "string", "required": True}}})
    assert out == {"properties": {"x": {"type": "string"}}}


def test_boolean_valued_keywords_preserved():
    schema = {"type": "array", "uniqueItems": True, "items": {"type": "string"}}
    assert sanitize_boolean_schemas(schema) == schema


def test_recurses_into_properties_and_items():
    schema = {
        "type": "object",
        "properties": {"a": {"items": True}},
        "items": False,
    }
    assert sanitize_boolean_schemas(schema) == {
        "type": "object",
        "properties": {"a": {"items": {}}},
        "items": {"not": {}},
    }
