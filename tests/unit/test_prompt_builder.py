import pytest

from ctx_weft.core.assembler.builder import PromptBuilder
from ctx_weft.core.models.errors import ContextOverflowError
from ctx_weft.protocols import LLMMessage, LLMTool


def test_builder_preserves_template_order_and_decorates_by_ref():
    builder = PromptBuilder(token_counter=len)
    builder.system("identity\n", priority=0, required=True)
    builder.system("background", priority=1)
    builder.message(LLMMessage(role="user", content="body"), ref="opening",
                    priority=0, required=True)
    builder.before("opening", "before-", priority=0, required=True)
    builder.after("opening", "-after", priority=1)
    builder.user("tail", priority=1, merge=True)

    prompt = builder.build(1000)

    assert prompt.system == "identity\nbackground"
    assert [message.content for message in prompt.messages] == ["before-body-after\n\ntail"]


def test_builder_trims_rendered_optional_parts_not_required_parts():
    builder = PromptBuilder(token_counter=len)
    builder.message(LLMMessage(role="user", content="required"), ref="current",
                    priority=0, required=True)
    builder.after("current", "-optional", priority=7)

    # Message accounting includes the provider framing allowance.
    prompt = builder.build(12)

    assert [message.content for message in prompt.messages] == ["required"]


def test_builder_drops_tool_exchange_atomically():
    builder = PromptBuilder(token_counter=len)
    builder.message(
        LLMMessage(role="assistant", content="", tool_calls=[{
            "id": "call-1", "name": "tool", "input": {},
        }]),
        ref="call", priority=7,
    )
    builder.message(LLMMessage(role="tool", content="result", tool_call_id="call-1"),
                    ref="result", priority=7)
    builder.message(LLMMessage(role="user", content="keep"), ref="current",
                    priority=0, required=True)

    prompt = builder.build(8)

    assert [(message.role, message.content) for message in prompt.messages] == [("user", "keep")]


def test_builder_reserves_tool_schema_and_reports_floor_overflow():
    builder = PromptBuilder(token_counter=len)
    builder.tools([
        LLMTool(name="tool", description="description", input_schema={"type": "object"}),
    ])
    builder.message(LLMMessage(role="user", content="required"), ref="current",
                    priority=0, required=True)

    with pytest.raises(ContextOverflowError):
        builder.build(1, context_limit=10, reserved_output_tokens=2)
