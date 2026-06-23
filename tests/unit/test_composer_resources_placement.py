"""Resources/directive relocated from actor system prompt → first user message."""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

from ctx_weft.core.assembler.assembler import ContextBlock
from ctx_weft.core.assembler.composer import DefaultComposer


def _cap_block(name: str, kind: str, desc: str) -> ContextBlock:
    return ContextBlock(
        id=f"cap-{name}",
        source="capability",
        kind="capabilities",
        target="system",
        content=desc,
        priority=1,
        token_estimate=1,
        metadata={"capability_name": name, "capability_kind": kind},
    )


def _tool_block_kind(
    qname: str, desc: str, provider_name: str, provider_description: str, kind: str,
) -> ContextBlock:
    return ContextBlock(
        id=f"cap-{qname}",
        source="capability",
        kind="capabilities",
        target="system",
        content=desc,
        priority=1,
        token_estimate=1,
        metadata={
            "capability_name": qname,
            "capability_kind": kind,
            "provider_name": provider_name,
            "provider_description": provider_description,
        },
    )


def _tool_block(qname: str, desc: str, provider_name: str, provider_description: str) -> ContextBlock:
    return _tool_block_kind(qname, desc, provider_name, provider_description, "tool")


def _identity_block(text: str) -> ContextBlock:
    return ContextBlock(id="id", source="identity", kind="identity", target="system",
                        content=text, priority=0, token_estimate=1, metadata={})


def _background_block(text: str) -> ContextBlock:
    return ContextBlock(id="bg", source="memory", kind="background", target="system",
                        content=text, priority=1, token_estimate=1, metadata={})


def _directive_block(text: str) -> ContextBlock:
    return ContextBlock(id="dir", source="identity:skill", kind="directive", target="system",
                        content=text, priority=1, token_estimate=1,
                        metadata={"kind": "skill_instructions"})


def _fresh_task_request() -> SimpleNamespace:
    task = SimpleNamespace(user_prompt_in_memory=False, process_report=None,
                           title="T", description="D", user_prompt="hello world")
    return SimpleNamespace(task=task, purpose="act")


def test_actor_system_has_only_soul_and_background() -> None:
    blocks = [
        _identity_block("SOUL TEXT"),
        _background_block("BG TEXT"),
        _cap_block("web_search", "tool", "search the web"),
        _cap_block("researcher", "agent", "a research subagent"),
        _directive_block("Instructions for skill 'x':\nDo the thing"),
    ]
    system = DefaultComposer()._build_actor_system(blocks)
    assert "SOUL TEXT" in system
    assert "## Project Background" in system and "BG TEXT" in system
    assert "### Available Tools" not in system
    assert "### Available Sub-Agents" not in system
    assert "## Instructions for the current task" not in system
    assert "Do the thing" not in system


def test_act_directive_on_first_capabilities_on_last() -> None:
    blocks = [
        _identity_block("SOUL TEXT"),
        _background_block("BG TEXT"),
        _cap_block("web_search", "tool", "search the web"),
        _cap_block("researcher", "agent", "a research subagent"),
        _directive_block("Instructions for skill 'x':\nDo the thing"),
        _history_block("user", "## Current Message\nthe original ask", "1"),
        _history_block("assistant", "did some work", "2"),
    ]
    task = SimpleNamespace(user_prompt_in_memory=True, process_report="halfway done",
                           title="T", description="D", user_prompt="the original ask")
    msgs = DefaultComposer()._build_actor_messages(blocks, SimpleNamespace(task=task, purpose="act"))
    user_msgs = [m for m in msgs if m.role == "user"]
    first, last = user_msgs[0].content, user_msgs[-1].content
    # directive rides the first (history-derived) user message
    assert "## Instructions for the current task" in first
    assert "Do the thing" in first
    assert "the original ask" in first
    assert "### Available Tools" not in first
    # capabilities relocated to the trailing user message (recency, next to act guidance)
    assert "## Capabilities" in last
    assert "### Available Tools" in last
    assert "### Available Sub-Agents" in last


def test_act_directive_follows_current_task() -> None:
    blocks = [
        _identity_block("SOUL TEXT"),
        _background_block("BG TEXT"),
        _directive_block("Do the thing"),
    ]
    msgs = DefaultComposer()._build_actor_messages(blocks, _fresh_task_request())
    content = [m for m in msgs if m.role == "user"][0].content
    assert "## Current Task" in content
    assert "## Instructions for the current task" in content
    # directive sits after the task context, not before it
    assert content.index("## Current Task") < content.index("## Instructions for the current task")


def test_act_capabilities_intro_heading_on_last() -> None:
    blocks = [
        _identity_block("SOUL TEXT"),
        _background_block("BG TEXT"),
        _cap_block("web_search", "tool", "search the web"),
    ]
    msgs = DefaultComposer()._build_actor_messages(blocks, _fresh_task_request())
    last = [m for m in msgs if m.role == "user"][-1].content
    assert "## Capabilities" in last
    assert "complete the current task" in last
    # intro heading precedes the specific capability lists
    assert last.index("## Capabilities") < last.index("### Available Tools")


def _skill_directive_block(text: str, skill_name: str) -> ContextBlock:
    return ContextBlock(id="dir", source="identity:skill", kind="directive", target="system",
                        content=text, priority=1, token_estimate=1,
                        metadata={"kind": "skill_instructions", "skill_name": skill_name})


def test_directive_merges_skill_name_into_heading_and_shifts_levels() -> None:
    md = "# Overview\nsurvey the network\n\n## Steps\n1. collect\n\n### Detail\nx"
    blocks = [_skill_directive_block(md, "sdn-migration-survey")]
    section = DefaultComposer()._build_directive_section(blocks)
    # skill name folded into the H2 heading; no separate "Instructions for skill" line
    assert section.startswith("## Instructions for the current task (skill: sdn-migration-survey)")
    assert "Instructions for skill 'sdn-migration-survey'" not in section
    # skill md headings shifted so its top level (H1) nests under the H2 container (→ H3)
    lines = section.split("\n")
    assert "### Overview" in lines
    assert "#### Steps" in lines
    assert "##### Detail" in lines
    assert "# Overview" not in lines  # original H1 line is gone (shifted)


def test_directive_without_headings_unchanged_body() -> None:
    blocks = [_skill_directive_block("just do the thing", "my-skill")]
    section = DefaultComposer()._build_directive_section(blocks)
    assert section == "## Instructions for the current task (skill: my-skill)\n\njust do the thing"


def test_act_skills_on_last_when_no_directive() -> None:
    blocks = [
        _identity_block("SOUL TEXT"),
        _background_block("BG TEXT"),
        _cap_block("code_runner", "skill", "run code"),
    ]
    msgs = DefaultComposer()._build_actor_messages(blocks, _fresh_task_request())
    last = [m for m in msgs if m.role == "user"][-1].content
    assert "### Available Skills" in last
    assert "code_runner" in last


def test_actor_no_resources_is_noop() -> None:
    blocks = [_identity_block("SOUL TEXT"), _background_block("BG TEXT")]
    msgs = DefaultComposer()._build_actor_messages(blocks, _fresh_task_request())
    first = [m for m in msgs if m.role == "user"][0].content
    assert "---\n\n## Current Task" not in first  # no preamble separator injected
    assert "## Current Task" in first


def test_tools_grouped_by_provider_with_description_and_prefix() -> None:
    blocks = [
        _tool_block("mcp__github__create_issue", "Create an issue", "mcp:github",
                    "Manage GitHub issues & PRs."),
        _tool_block("mcp__github__add_comment", "Comment on an issue", "mcp:github",
                    "Manage GitHub issues & PRs."),
        _tool_block("fs__bash_exec", "Run a shell command", "fs", ""),
    ]
    section = DefaultComposer()._build_resources_section(blocks)
    # provider 分块标题
    assert "#### mcp:github tools" in section
    assert "#### fs tools" in section
    # 有 description 的 provider：写出描述
    assert "Manage GitHub issues & PRs." in section
    # 每个 provider 段都带具体前缀提示（无论是否有 description）
    assert "prefix the tool name with `mcp__github__`" in section
    assert "prefix the tool name with `fs__`" in section
    # 工具仍按 qualified 名列出
    assert "- mcp__github__create_issue: Create an issue" in section
    assert "- fs__bash_exec: Run a shell command" in section
    # 描述出现在该 provider 标题之后、其工具之前
    assert section.index("#### mcp:github tools") < section.index("Manage GitHub issues") \
        < section.index("- mcp__github__create_issue")


def test_skills_grouped_by_provider_with_description_and_prefix() -> None:
    blocks = [
        _tool_block_kind("local_skill__pdf", "Work with PDFs", "local_skill",
                         "Filesystem-backed skills.", "skill"),
        _tool_block_kind("local_skill__csv", "Work with CSVs", "local_skill",
                         "Filesystem-backed skills.", "skill"),
    ]
    section = DefaultComposer()._build_resources_section(blocks)
    assert "### Available Skills (assign to tasks where appropriate)" in section
    assert "#### local_skill skills" in section
    assert "Filesystem-backed skills." in section
    assert "When assigning these skills, prefix the skill name with `local_skill__`" in section
    assert "- local_skill__pdf: Work with PDFs" in section


def test_subagents_grouped_by_provider_keep_delegate_preamble() -> None:
    blocks = [
        _tool_block_kind("template_agent__planner", "Plans work", "template_agent",
                         "Built-in agent templates.", "agent"),
    ]
    section = DefaultComposer()._build_resources_section(blocks)
    assert "### Available Sub-Agents" in section
    assert "Delegate via:" in section  # preamble preserved
    assert "#### template_agent sub-agents" in section
    assert "Built-in agent templates." in section
    assert "When delegating these sub-agents, prefix the sub-agent name with `template_agent__`" in section
    assert "- template_agent__planner: Plans work" in section


def test_tools_without_provider_name_render_flat() -> None:
    blocks = [_cap_block("web_search", "tool", "search the web")]
    section = DefaultComposer()._build_resources_section(blocks)
    assert "### Available Tools" in section
    assert "- web_search: search the web" in section
    assert "####" not in section  # 无 provider 分块标题


def test_observer_system_excludes_resources_and_directive() -> None:
    template = SimpleNamespace(identity={"act": SimpleNamespace(text="ACT SOUL")})
    request = SimpleNamespace(template=template)
    blocks = [
        _background_block("BG TEXT"),
        _cap_block("report_task_outcome", "tool", "report the outcome"),
        _directive_block("Instructions for skill 'x':\nDo the thing"),
    ]
    system = DefaultComposer()._build_act_system(blocks, request)
    assert "ACT SOUL" in system
    assert "## Project Background" in system and "BG TEXT" in system
    assert "### Available Tools" not in system
    assert "## Instructions for the current task" not in system
    assert "Do the thing" not in system


def test_observer_messages_inject_resources_and_keep_role() -> None:
    blocks = [
        _identity_block("OBSERVER ROLE"),  # purpose=observe → identity block is the ROLE
        _cap_block("report_task_outcome", "tool", "report the outcome"),
    ]
    task = SimpleNamespace(title="T", description="d", user_prompt="up",
                           user_prompt_in_memory=False, process_report=None)
    request = SimpleNamespace(task=task)
    msgs = DefaultComposer()._build_observer_messages(blocks, request)
    user_msgs = [m for m in msgs if m.role == "user"]
    assert "### Available Tools" in user_msgs[0].content
    assert "report the outcome" in user_msgs[0].content
    joined = "\n".join(m.content for m in msgs if isinstance(m.content, str))
    assert "OBSERVER ROLE" in joined


def _history_block(role: str, content: str, ts: str) -> ContextBlock:
    return ContextBlock(id=f"h-{ts}", source="task_conversation", kind="history",
                        target="messages", content=content, priority=3, token_estimate=1,
                        metadata={"role": role, "timestamp": ts})


def _experience_block(role: str, content: str, ts: str) -> ContextBlock:
    return ContextBlock(id=f"e-{ts}", source="agent_experience", kind="history",
                        target="messages", content=content, priority=3, token_estimate=1,
                        metadata={"role": role, "timestamp": ts})


def test_act_directive_targets_current_task_not_prior_experience() -> None:
    """The current task's skill directive must ride the current task's first user message
    (task_conversation), not an earlier cross-task agent_experience turn that happens to be
    the first user message of the whole list."""
    blocks = [
        _identity_block("SOUL TEXT"),
        _directive_block("Do the thing"),
        # cross-task experience (older) — must NOT receive the current task's directive
        _experience_block("user", "prior task ask", "1"),
        _experience_block("assistant", "prior task work", "2"),
        # the current task's own conversation (newer)
        _history_block("user", "## Current Message\nthe current ask", "3"),
    ]
    task = SimpleNamespace(user_prompt_in_memory=True, process_report=None,
                           title="T", description="D", user_prompt="the current ask")
    msgs = DefaultComposer()._build_actor_messages(blocks, SimpleNamespace(task=task, purpose="act"))
    user_msgs = [m for m in msgs if m.role == "user"]
    prior = next(m for m in user_msgs if "prior task ask" in m.content)
    current = next(m for m in user_msgs if "the current ask" in m.content)
    # directive rides the current task's first user message, not the prior experience turn
    assert "## Instructions for the current task" not in prior.content
    assert "## Instructions for the current task" in current.content
    assert "Do the thing" in current.content


def test_resumed_task_directive_on_history_capabilities_on_progress() -> None:
    """Resumed act task: the directive attaches to the first (history-derived) user message;
    capabilities ride the trailing Current Progress message (recency)."""
    blocks = [
        _identity_block("SOUL TEXT"),
        _background_block("BG TEXT"),
        _cap_block("web_search", "tool", "search the web"),
        _directive_block("Instructions for skill 'x':\nDo the thing"),
        _history_block("user", "## Current Message\nthe original ask", "1"),
        _history_block("assistant", "did some work", "2"),
    ]
    # process_report_at present → Current Progress renders as a timestamped history block that
    # sorts after the prior turns (string "2026-..." > "2"), so it is the last user message.
    task = SimpleNamespace(user_prompt_in_memory=True, process_report="halfway done",
                           process_report_at=datetime(2026, 1, 1, tzinfo=UTC),
                           title="T", description="D", user_prompt="the original ask")
    msgs = DefaultComposer()._build_actor_messages(blocks, SimpleNamespace(task=task, purpose="act"))
    user_msgs = [m for m in msgs if m.role == "user"]
    first, last = user_msgs[0].content, user_msgs[-1].content
    # directive on the first (history-derived) user message, AFTER the task content
    assert "## Instructions for the current task" in first
    assert "the original ask" in first
    assert first.index("the original ask") < first.index("## Instructions for the current task")
    assert "### Available Tools" not in first
    # the trailing dynamic-context message carries the progress AND the capabilities
    assert "## Current Progress" in last and "halfway done" in last
    assert "### Available Tools" in last
    assert first is not last
