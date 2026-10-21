"""JSON Schema 清洗：把严格 provider（DeepSeek V4 / OpenAI 等）拒绝的 boolean schema
规整成 object。移植自 QwenPaw ``openai_chat_model_compat._sanitize_boolean_schemas``。

JSON Schema 里 boolean 有两种用法：
  1. **boolean schema**（出现在「该是 schema」的位置）：``true`` = 接受任意、``false`` = 拒绝一切。
     合法但被严格 provider 拒。转成 ``true→{}``、``false→{"not": {}}``。
  2. **boolean 取值的关键字**（``nullable`` / ``uniqueItems`` / ``deprecated`` …）：必须保持 boolean。
本游走器只递归进「schema 位置」，故普通 boolean 注解原样透传。
"""

from __future__ import annotations

from typing import Any

# 值本身是一个 schema 的关键字。
_SINGLE_SCHEMA_KEYWORDS = frozenset({
    "items", "additionalProperties", "additionalItems", "unevaluatedProperties",
    "unevaluatedItems", "contains", "propertyNames", "not", "if", "then", "else",
    "contentSchema",
})
# 值是 schema 数组的关键字。
_ARRAY_SCHEMA_KEYWORDS = frozenset({"allOf", "anyOf", "oneOf", "prefixItems"})
# 值是「name→schema」映射的关键字。
_MAP_SCHEMA_KEYWORDS = frozenset({
    "properties", "patternProperties", "$defs", "definitions", "dependentSchemas",
})


def sanitize_boolean_schemas(schema: Any) -> Any:
    """按位置游走，规整 boolean schema；boolean 注解关键字原样保留。"""
    if schema is True:
        return {}
    if schema is False:
        return {"not": {}}
    if not isinstance(schema, dict):
        return schema

    result: dict[str, Any] = {}
    for key, value in schema.items():
        # additionalProperties: true 是默认值，显式形式被部分严格校验器拒 → 删。
        if key == "additionalProperties" and value is True:
            continue
        # required: <bool> 是畸形（真 JSON Schema 用 required: ["field"]）→ 删。
        if key == "required" and isinstance(value, bool):
            continue

        if key in _SINGLE_SCHEMA_KEYWORDS:
            if key == "items" and isinstance(value, list):
                result[key] = [sanitize_boolean_schemas(v) for v in value]
            else:
                result[key] = sanitize_boolean_schemas(value)
        elif key in _ARRAY_SCHEMA_KEYWORDS:
            result[key] = (
                [sanitize_boolean_schemas(v) for v in value]
                if isinstance(value, list) else value
            )
        elif key in _MAP_SCHEMA_KEYWORDS:
            result[key] = (
                {k: sanitize_boolean_schemas(v) for k, v in value.items()}
                if isinstance(value, dict) else value
            )
        elif key == "dependencies" and isinstance(value, dict):
            result[key] = {
                k: (sanitize_boolean_schemas(v) if isinstance(v, (dict, bool)) else v)
                for k, v in value.items()
            }
        else:
            result[key] = value
    return result
