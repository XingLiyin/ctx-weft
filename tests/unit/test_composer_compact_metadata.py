"""compact/recognize_intent assembled like observe: act SOUL in system, purpose facet in trailing message."""

from __future__ import annotations

from types import SimpleNamespace

from loomex_core.core.assembler.assembler import ContextBlock
from loomex_core.core.assembler.composer import DefaultComposer


def _identity(text):
    return ContextBlock(id="id", source="identity", kind="identity", target="system",
                        content=text, priority=0, token_estimate=1, metadata={})


def _bg(text):
    return ContextBlock(id="bg", source="memory", kind="background", target="system",
                        content=text, priority=1, token_estimate=1, metadata={})


def _tool(name, desc):
    return ContextBlock(id=f"c-{name}", source="capability", kind="capabilities", target="system",
                        content=desc, priority=1, token_estimate=1,
                        metadata={"capability_name": name, "capability_kind": "tool",
                                  "llm_tool": None})


def _req(purpose):
    task = SimpleNamespace(user_prompt_in_memory=False, process_report=None,
                           title="T", description="D", user_prompt="do the thing")
    template = SimpleNamespace(identity={"act": SimpleNamespace(text="ACT-SOUL", style=None)})
    return SimpleNamespace(purpose=purpose, task=task, template=template)


async def test_compact_system_is_act_facet_and_persona_in_trailing():
    blocks = [_identity("COMPACT-PERSONA"), _bg("BG")]
    prompt = await DefaultComposer().compose(blocks, _req("compact"))

    assert "ACT-SOUL" in prompt.system
    assert "## Project Background" in prompt.system
    assert "COMPACT-PERSONA" not in prompt.system

    last_user = [m for m in prompt.messages if m.role == "user"][-1].content
    assert "## Your Current Role\n\nCOMPACT-PERSONA" in last_user  # persona under a heading
    assert "[Context so far]" in last_user          # compaction cue present
    assert "do the thing" in last_user              # actor conversation reused
    assert prompt.tools == []


async def test_metadata_system_is_act_facet_and_persona_in_trailing():
    blocks = [_identity("MD-PERSONA"), _bg("BG"), _tool("update_task_metadata", "set title/desc")]
    prompt = await DefaultComposer().compose(blocks, _req("recognize_intent"))

    assert "ACT-SOUL" in prompt.system
    assert "MD-PERSONA" not in prompt.system

    last_user = [m for m in prompt.messages if m.role == "user"][-1].content
    assert "## Your Current Role\n\nMD-PERSONA" in last_user  # persona under a heading
    assert "update_task_metadata" in last_user      # metadata cue names the tool
    assert any(t.name == "update_task_metadata" for t in prompt.tools)
