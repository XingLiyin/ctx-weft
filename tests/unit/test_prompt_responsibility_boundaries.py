"""Prompt ownership and tool guidance survive actual Jinja assembly in both locales."""
from __future__ import annotations

import re
from types import SimpleNamespace as NS

import pytest

from ctx_weft.core.assembler.assembler import ContextBlock
from ctx_weft.core.assembler.builder import PromptBuilder, _prompt_facts
from ctx_weft.core.assembler.context import PromptContext
from ctx_weft.core.assembler.prompts import render_purpose
from ctx_weft.core.capabilities.control_tools import _CONTROL_TOOLS
from ctx_weft.core.capabilities.skill_executor import _SKILL_EXECUTOR_TOOLS
from ctx_weft.core.models.errors import ContextOverflowError
from ctx_weft.protocols import LLMTool
from ctx_weft.protocols.capability import qualify


def render(locale, purpose, *, tool_names=(), grouped=True, template=None, **options):
    context = PromptContext(
        purpose=purpose,
        task={"id": "task-1", "title": "Task", "user_prompt": "do the work"},
        **options,
    )
    tools = []
    for name in tool_names:
        registry = _CONTROL_TOOLS if name in _CONTROL_TOOLS else _SKILL_EXECUTOR_TOOLS
        cap = registry[name][0]
        context.add(ContextBlock(
            name, "capability", "capabilities", cap,
            {"provider_name": cap.id.split(":")[0]} if grouped else {},
        ))
        tools.append(LLMTool(name=qualify(cap.id), description=cap.description,
                             input_schema=cap.input_schema))
    builder = PromptBuilder(len)
    builder.tools(tools)
    facts = _prompt_facts(context, template=template)
    name = facts.pop("template_name")
    render_purpose(name, locale=locale, purpose=name, context=context, prompt=builder, **facts)
    result = builder.build(100_000)
    text = result.system + "\n" + "\n".join(str(m.content) for m in result.messages)
    return text, builder


@pytest.mark.parametrize("locale", ("en", "zh"))
@pytest.mark.parametrize("grouped", (False, True))
def test_actor_gets_tool_instructions_without_a_guidance_block(locale, grouped):
    text, _ = render(locale, "act", grouped=grouped,
                     tool_names=("ask_user", "switch_task", "delegate_task", "delegate_plan"))
    for parameter in ("questions", "target_task_id", "reason", "skill_name",
                      "subagent_template", "inherit_memory"):
        assert f"`{parameter}`" in text
    assert "20" in text
    assert "interactive" not in text


@pytest.mark.parametrize("locale", ("en", "zh"))
def test_actor_does_not_teach_unavailable_control_tools(locale):
    text, _ = render(locale, "act", tool_names=("ask_user",))
    assert "`questions`" in text
    for absent in ("control__switch_task", "control__delegate_task", "control__delegate_plan",
                   "target_task_id", "inherit_memory"):
        assert absent not in text


@pytest.mark.parametrize("locale", ("en", "zh"))
def test_skill_executor_paging_and_command_instructions_follow_available_tools(locale):
    text, _ = render(locale, "act", tool_names=("list_files", "read_file", "exec_script"))
    for parameter in ("pattern", "path", "offset", "limit", "char_offset", "char_limit", "command"):
        assert f"`{parameter}`" in text
    assert "2000" in text and "200" in text
    assert "do not mix the two modes" in text if locale == "en" else "不要混用两种模式" in text
    assert "control__delegate_task" not in text


@pytest.mark.parametrize("locale", ("en", "zh"))
def test_tool_instructions_cannot_be_trimmed_while_the_tool_remains(locale):
    _, builder = render(locale, "act", tool_names=("ask_user",))
    instructions = [op for op in builder._operations if op.group == "tool-instructions"]
    assert instructions and all(op.required for op in instructions)
    with pytest.raises(ContextOverflowError):
        builder.build(1)


@pytest.mark.parametrize("locale", ("en", "zh"))
@pytest.mark.parametrize("scope", ("task", "agent"))
@pytest.mark.parametrize("custom", (False, True))
def test_compact_preserves_five_sections_and_uses_only_the_selected_policy(locale, scope, custom):
    template = NS(identity={"compact": NS(text="CUSTOM_SUMMARY_POLICY")}) if custom else None
    text, _ = render(locale, "compact", compact_scope=scope, template=template)
    assert re.findall(r"^## (Goal|Constraints|Done|Remaining|Evidence) —", text, re.M) == [
        "Goal", "Constraints", "Done", "Remaining", "Evidence"]
    assert text.count("CUSTOM_SUMMARY_POLICY") == int(custom)
    # The detailed retention policy belongs to COMPACT.md, including for fallback renders.
    for copied_policy in ("explanations ruled out", "已排除的解释", "remove superseded state",
                          "移除已被取代的状态"):
        assert copied_policy not in text


@pytest.mark.parametrize("locale", ("en", "zh"))
@pytest.mark.parametrize("judge", (False, True))
@pytest.mark.parametrize("boundary", ("context_limit", "max_turns", "finish", "task_switch"))
def test_observe_keeps_field_routing_and_injects_each_md_policy_once(locale, judge, boundary):
    template = NS(identity={"observe": NS(text="CUSTOM_EVALUATION_POLICY"),
                            "compact": NS(text="CUSTOM_SUMMARY_POLICY")})
    text, _ = render(locale, "observe", tool_names=("report_observation",), template=template,
                     judge_task=judge, terminal_only=judge and boundary == "task_switch",
                     observe_boundary=boundary)
    assert text.count("CUSTOM_SUMMARY_POLICY") == 1
    assert text.count("CUSTOM_EVALUATION_POLICY") == int(judge)
    assert "`act_recap`" in text and "`control__report_observation`" in text
    assert "first person" in text if locale == "en" else "第一人称" in text
    if judge or boundary == "finish":
        assert "`task_summary`" in text
    if judge:
        assert "`task_failure_reason`" in text and "`next_step_hint`" in text
        assert ("leave empty for `continue` or `waiting`" in text if locale == "en"
                else "`continue` 或 `waiting` 时留空" in text)
    for copied_policy in ("explanations ruled out", "已排除的解释", "actor's message asks",
                          "只要那段消息在向用户要"):
        assert copied_policy not in text


@pytest.mark.parametrize("locale", ("en", "zh"))
def test_metadata_limits_live_in_the_localized_instruction(locale):
    text, _ = render(locale, "recognize_intent", tool_names=("update_task_metadata",))
    for field in ("title", "description", "session_goal"):
        assert f"`{field}`" in text
    for limit in ("20", "80", "60"):
        assert limit in text


def test_control_schemas_remain_data_descriptions():
    def descriptions(value):
        if isinstance(value, dict):
            for key, item in value.items():
                if key == "description" and isinstance(item, str):
                    yield item
                else:
                    yield from descriptions(item)
        elif isinstance(value, list):
            for item in value:
                yield from descriptions(item)

    for cap, _ in _CONTROL_TOOLS.values():
        prose = "\n".join((cap.description, *descriptions(cap.input_schema))).lower()
        for instruction in ("leave empty", "required when", "call exactly", "call alone",
                            "do not", "must ", "always ", "first person", "≤"):
            assert instruction not in prose, (cap.id, instruction)
