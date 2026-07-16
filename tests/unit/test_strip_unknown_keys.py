"""Gateway 丢弃 input_schema 未声明的顶层键（对任意调用生效）。

模型偶尔臆造 schema 里没有的键；畸形缓冲救援也可能抠出带杂键的对象。剥掉它们，
只把 schema 声明的参数交给工具。仅在能明确「什么是已知键」时才剥（fail-open）。
"""

from ctx_weft.core.loop.capability_gateway import _strip_unknown_keys

_SCHEMA = {
    "type": "object",
    "properties": {"q": {"type": "string"}, "limit": {"type": "integer"}},
}


def test_drops_unknown_key():
    assert _strip_unknown_keys({"q": "x", "b": 2}, _SCHEMA) == {"q": "x"}


def test_keeps_all_known_keys():
    assert _strip_unknown_keys({"q": "x", "limit": 5}, _SCHEMA) == {"q": "x", "limit": 5}


def test_all_unknown_stripped_to_empty():
    assert _strip_unknown_keys({"b": 2, "c": 3}, _SCHEMA) == {}


def test_no_properties_is_passthrough():
    assert _strip_unknown_keys({"anything": 1}, {"type": "object"}) == {"anything": 1}


def test_empty_or_none_schema_passthrough():
    assert _strip_unknown_keys({"anything": 1}, {}) == {"anything": 1}
    assert _strip_unknown_keys({"anything": 1}, None) == {"anything": 1}


def test_absent_additional_properties_strips():
    # JSON Schema 默认允许附加属性，但按需求「对任意调用生效」→ 未显式允许即剥。
    assert _strip_unknown_keys({"q": "x", "b": 2}, _SCHEMA) == {"q": "x"}


def test_additional_properties_false_strips():
    schema = {**_SCHEMA, "additionalProperties": False}
    assert _strip_unknown_keys({"q": "x", "b": 2}, schema) == {"q": "x"}


def test_additional_properties_true_is_respected():
    # schema 显式允许附加属性 → 不剥。
    schema = {**_SCHEMA, "additionalProperties": True}
    assert _strip_unknown_keys({"q": "x", "b": 2}, schema) == {"q": "x", "b": 2}


def test_additional_properties_subschema_is_respected():
    # additionalProperties 为子 schema（允许并约束附加属性）→ 不剥。
    schema = {**_SCHEMA, "additionalProperties": {"type": "integer"}}
    assert _strip_unknown_keys({"q": "x", "b": 2}, schema) == {"q": "x", "b": 2}


def test_composition_keyword_disables_strip():
    # allOf/anyOf/oneOf 下键可能由子 schema 声明，顶层 properties 不全 → 不剥，避免误删。
    schema = {"properties": {"q": {"type": "string"}}, "anyOf": [{"properties": {"b": {}}}]}
    assert _strip_unknown_keys({"q": "x", "b": 2}, schema) == {"q": "x", "b": 2}


def test_only_top_level_stripped_nested_untouched():
    # 仅剥顶层未知键；嵌套对象内部不递归（top-level-only，避免组合/$ref 误删）。
    schema = {"type": "object", "properties": {"obj": {"type": "object",
              "properties": {"a": {"type": "integer"}}}}}
    assert _strip_unknown_keys({"obj": {"a": 1, "junk": 2}}, schema) == {"obj": {"a": 1, "junk": 2}}
