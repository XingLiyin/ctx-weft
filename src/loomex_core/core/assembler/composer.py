"""Composer：把 ContextBlock 渲染成最终 AssembledPrompt。

格式与 miniAgents prompt_builder.py 完全对齐——这是 refactor 不是 redesign。

Actor system prompt（用 --- 分隔，仅 soul + 项目背景，per-task 资源不入此处）：
  soul（identity）
  ---
  ## Project Background\n\n{background}

Actor messages（多轮 LLMMessage：history 轮次在前，最后一条 user 含任务上下文）。
resources（skills/tools/agents）+ 当前 task 的 skill 指令以前缀形式拼到**首条** user
message 之前（仅发送，不入 memory）：
  ### Available Skills / ### Available Tools / ### Available Sub-Agents
  ## Instructions for the current task\n\n{skill_instructions / directive}
  ---
  {该首条 user message 原内容}
后续任务上下文 user message：
  ## Task Background
  - {blackboard_snippets}
  ## Current Task
  {task.title}
  {task.description}
  ## Current Message
  {task.user_prompt}
  历史轮次（独立 LLMMessage）：
  User: {content}
  Assistant: {content} + tool_calls
  Tool: {result}

Observer system prompt（与 act 同构）：
  role/soul（act identity）
  ---
  ## Project Background\n\n{background}

Observer messages：复用 actor messages（同样把 resources + skill 指令注入首条 user
message），再追加一条尾部 user message（仅发送，不入 memory）：
  {observe ROLE（identity）}
  ---
  {判定提示}
  Your sub-task results / Upstream task results:
  - {blackboard_snippets}
"""

from __future__ import annotations

import dataclasses
import re
from abc import abstractmethod
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

from loomex_core.protocols import LLMMessage, LLMTool
from loomex_core.protocols.capability import qualify
from loomex_core.core.utils import content_to_text, estimate_tokens
from loomex_core.core.orchestrator.control_capability import (
    DELEGATE_TASK_NAME,
    REPORT_TASK_OUTCOME_NAME,
)

if TYPE_CHECKING:
    from loomex_core.core.assembler.assembler import (
        AssembledPrompt,
        ContextBlock,
        ContextRequest,
    )


_OBSERVER_ROLE_FALLBACK = "You are an objective observer evaluating task execution results."

# 尾部 observe user message 的判定提示（拼在 ROLE 之后）。
_OBSERVE_JUDGMENT_CUE = (
    "Now act as the observer for the current task. Based on the execution above, judge the "
    f"task's completion status and call `{REPORT_TASK_OUTCOME_NAME}` exactly once: give a `task_status` "
    "of `success` (fully accomplished), `retry` (needs another attempt), or `fail` (cannot be "
    "completed), plus a thorough, evidence-based `task_process_report`. Optionally review your "
    "own sub-tasks via `task_reviews`. Call no other tools."
)

_COMPACTION_INSTRUCTION = (
    "Now act as a memory compactor. Summarize the conversation above into a concise "
    "[Context so far] section that preserves: key user intents, important facts discovered, "
    "decisions made, tool results, and any unfinished threads. Output only the summary text, "
    "no preamble."
)

_RECOGNIZE_INTENT_INSTRUCTION = (
    "Now set this task's metadata: call `control__update_task_metadata` exactly once with a concise "
    "title and description (and the session goal if the direction is now clear), then stop. "
    "Call no other tools."
)


_HEADING_RE = re.compile(r"^(#{1,6})\s")


def _shift_markdown_headings(md: str, base_level: int) -> str:
    """把 md 内的标题层级整体下移，使最浅一级标题成为 base_level 的子级（base_level+1）。

    用于把 skill 正文嵌进 "## Instructions for the current task"（H2）之下：md 里的
    H1/H2 会被下移，避免与容器标题抢层级；已比 base_level+1 更深则不动（不上提）。
    跳过 ``` / ~~~ 围栏代码块内的 # 行。
    """
    lines = md.split("\n")

    def _iter_heading_levels():
        in_fence = False
        for ln in lines:
            stripped = ln.lstrip()
            if stripped.startswith("```") or stripped.startswith("~~~"):
                in_fence = not in_fence
                continue
            if in_fence:
                continue
            m = _HEADING_RE.match(ln)
            if m:
                yield len(m.group(1))

    levels = list(_iter_heading_levels())
    if not levels:
        return md
    shift = (base_level + 1) - min(levels)
    if shift <= 0:
        return md

    out: list[str] = []
    in_fence = False
    for ln in lines:
        stripped = ln.lstrip()
        if stripped.startswith("```") or stripped.startswith("~~~"):
            in_fence = not in_fence
            out.append(ln)
            continue
        if not in_fence:
            m = _HEADING_RE.match(ln)
            if m:
                new_level = min(6, len(m.group(1)) + shift)
                ln = "#" * new_level + ln[len(m.group(1)):]
        out.append(ln)
    return "\n".join(out)


def _merge_consecutive_messages(messages: list[LLMMessage]) -> list[LLMMessage]:
    """合并连续相同角色的消息（tool 消息因绑定 tool_call_id 不合并）。"""
    merged: list[LLMMessage] = []
    for msg in messages:
        if (
            merged
            and merged[-1].role == msg.role
            and msg.role != "tool"
        ):
            prev = merged[-1]
            prev_text = prev.content if isinstance(prev.content, str) else content_to_text(prev.content)
            cur_text = msg.content if isinstance(msg.content, str) else content_to_text(msg.content)
            merged[-1] = LLMMessage(
                role=prev.role,
                content=f"{prev_text}\n\n{cur_text}",
                tool_calls=prev.tool_calls + msg.tool_calls,
            )
        else:
            merged.append(msg)
    return merged


@runtime_checkable
class Composer(Protocol):
    """ContextBlock → AssembledPrompt 渲染器。"""

    @abstractmethod
    async def compose(
        self,
        blocks: list["ContextBlock"],
        request: "ContextRequest",
    ) -> "AssembledPrompt":
        ...


class DefaultComposer(Composer):
    """默认 Composer——按 miniAgents 风格组装。

    purpose=act：Actor 风格
    purpose=observe：Observer 风格（复用 act 会话 + 末尾 observe 指令）
    purpose=compact：复用 act system + 会话 + 末尾压缩指令，tools 为空
    purpose=recognize_intent：复用 act system + 会话 + 末尾元数据指令（update_task_metadata 工具）
    """

    async def compose(
        self,
        blocks: list["ContextBlock"],
        request: "ContextRequest",
    ) -> "AssembledPrompt":
        from loomex_core.core.assembler.assembler import AssembledPrompt

        if request.purpose == "act":
            system = self._build_actor_system(blocks)
            messages = self._build_actor_messages(blocks, request)
            tools = self._collect_llm_tools(blocks)
        elif request.purpose == "observe":
            system = self._build_act_system(blocks, request)
            messages = self._build_observer_messages(blocks, request)
            tools = self._collect_llm_tools(blocks)
        elif request.purpose == "recognize_intent":
            system = self._build_act_system(blocks, request)
            messages = self._build_facet_trailing_messages(blocks, request, _RECOGNIZE_INTENT_INSTRUCTION)
            tools = self._collect_llm_tools(blocks)
        else:  # compact
            system = self._build_act_system(blocks, request)
            messages = self._build_facet_trailing_messages(blocks, request, _COMPACTION_INSTRUCTION)
            tools = []

        token_count = estimate_tokens(system) + sum(
            estimate_tokens(content_to_text(m.content)) for m in messages
        )
        return AssembledPrompt(
            system=system,
            messages=messages,
            tools=tools,
            token_count=token_count,
        )

    # ── Actor ─────────────────────────────────────────────────────────────────

    def _build_actor_system(self, blocks: list["ContextBlock"]) -> str:
        """soul + Project Background，--- 分隔。

        Resources（skills/tools/agents）与 skill 指令不再进 system，
        改由 _build_actor_messages 注入本 task 首条 user message（不入 memory）。
        """
        identity = self._first_kind(blocks, "identity")
        background = self._first_kind(blocks, "background")

        parts: list[str] = []
        if identity:
            parts.append(content_to_text(identity.content))
        if background:
            parts.append(f"## Project Background\n\n{content_to_text(background.content)}")

        return "\n\n---\n\n".join(parts)

    def _build_actor_messages(
        self,
        blocks: list["ContextBlock"],
        request: "ContextRequest",
    ) -> list[LLMMessage]:
        """Actor messages：历史轮次在前，任务上下文 + 当前消息作为最后一条 user message。

        结构：
          [0..N-1] 历史轮次：user / assistant / tool 各自独立的 LLMMessage
          [N]      user: Task Background + Current Task + Current Progress + Current Message
        """
        # Task Background 只放跨 plan 前序（predecessor / tracking 等）结果；
        # 排除 subtask——自己派发的子任务结果已通过 agent_experience(tool result) 呈现，
        # 避免与之重复（spec/06 §12）。
        bb_blocks = [
            b for b in blocks
            if b.kind == "blackboard" and b.metadata.get("intent") != "subtask"
        ]
        history_blocks = [b for b in blocks if b.kind == "history"]

        messages: list[LLMMessage] = self._history_to_messages(history_blocks)

        task = request.task
        parts: list[str] = []

        if bb_blocks:
            lines = ["## Task Background"]
            for b in bb_blocks:
                lines.append(f"- {content_to_text(b.content)}")
            parts.append("\n".join(lines))

        if task.user_prompt_in_memory:
            # user_prompt（含 Current Task / Current Message）已在 history 里，
            # 最后一条只补充本轮动态上下文
            if getattr(task, "process_report", None):
                parts.append(f"## Current Progress\n{task.process_report}")
        else:
            # daemon 或尚未持久化的路径：实时构建完整的用户消息
            if task.title and task.description:
                parts.append(f"## Current Task\n{task.title}\n{task.description}")
            elif task.title:
                parts.append(f"## Current Task\n{task.title}")
            if getattr(task, "process_report", None):
                parts.append(f"## Current Progress\n{task.process_report}")
            if task.user_prompt:
                user_prompt_text = (
                    task.user_prompt
                    if isinstance(task.user_prompt, str)
                    else content_to_text(task.user_prompt)
                )
                parts.append(f"## Current Message\n{user_prompt_text}")

        if parts:
            messages.append(LLMMessage(role="user", content="\n\n".join(parts)))
        merged = _merge_consecutive_messages(messages)
        # 兜底：actor prompt 必须以 user 回合结尾——避免以 assistant/tool 结尾让模型困惑地续写自己。
        # 正常情况下 active/retry 的 Current Progress 已是末条 user；此处仅覆盖 summary 为空等边角。
        if merged and merged[-1].role != "user":
            merged.append(LLMMessage(role="user", content="Continue with the task above."))
        # Resources 注入（仅发送，不入 memory）：
        #   - directive（当前 task 指令）：act 追加到首条 user message 尾部（紧跟 ## Current Task
        #     等任务上下文之后）；其他 purpose 仍与 capabilities 一并前置到首条。
        #   - capabilities（skills/tools/agents）：act 放到末条 user message，紧邻 act guidance /
        #     finish_task，提升工具调用积极性；其他 purpose 前置到首条。
        directive_text = self._build_directive_section(blocks)
        capabilities_text = self._build_capabilities_section(blocks)
        if getattr(request, "purpose", None) == "act":
            merged = self._append_to_first_user(merged, directive_text)
            merged = self._append_to_last_user(merged, capabilities_text)
        else:
            preamble = "\n\n".join(p for p in (capabilities_text, directive_text) if p)
            merged = self._prepend_to_first_user(merged, preamble)
        return merged

    def _build_facet_trailing_messages(
        self,
        blocks: list["ContextBlock"],
        request: "ContextRequest",
        cue: str,
        extra_sections: list[str] | None = None,
        facet_fallback: str = "",
        facet_heading: str = "## Your Current Role",
    ) -> list[LLMMessage]:
        """act 会话 + 末尾一条 user message：purpose facet + cue（+ 可选附加段）。

        observe / compact / recognize_intent 共用。facet 取 purpose 对应的 identity block
        （IdentitySource 已按 purpose 产出），冠以 facet_heading 标题后并入最后一条 user 回合
        （避免连续 user）。
        """
        messages = self._build_actor_messages(blocks, request)
        facet = self._first_kind(blocks, "identity")
        facet_text = content_to_text(facet.content) if facet else ""
        if not facet_text:
            facet_text = facet_fallback

        sections: list[str] = []
        if facet_text:
            if facet_heading:
                facet_text = f"{facet_heading}\n\n{facet_text}"
            sections.extend([facet_text, "---"])
        sections.append(cue)
        if extra_sections:
            sections.extend(extra_sections)

        messages.append(LLMMessage(role="user", content="\n\n".join(sections)))
        return _merge_consecutive_messages(messages)

    def _build_resources_section(self, blocks: list["ContextBlock"]) -> str:
        """从 capabilities blocks 按 kind 分组渲染（miniAgents 风格）。

        三类（skills / tools / sub-agents）统一按 provider 分块（分级标题 + provider
        描述 + 带具体前缀的引用提示），见 _render_grouped_section。
        """
        cap_blocks = [b for b in blocks if b.kind == "capabilities"]
        skills = [b for b in cap_blocks if b.metadata.get("capability_kind") == "skill"]
        tools = [b for b in cap_blocks if b.metadata.get("capability_kind") == "tool"]
        agents = [b for b in cap_blocks if b.metadata.get("capability_kind") == "agent"]

        # 如果有 skill_instructions（directive），跳过 skill 列表渲染（避免重复）
        has_directive = any(b.kind == "directive" for b in blocks)

        sections: list[str] = []
        if skills and not has_directive:
            sections.append(self._render_grouped_section(
                "### Available Skills (assign to tasks where appropriate)",
                skills, action="assigning", noun="skill", noun_plural="skills",
            ))
        if tools:
            sections.append(self._render_grouped_section(
                "### Available Tools (use them via tool calls)",
                tools, action="calling", noun="tool", noun_plural="tools",
            ))
        if agents:
            sections.append(self._render_grouped_section(
                "### Available Sub-Agents",
                agents, action="delegating", noun="sub-agent", noun_plural="sub-agents",
                preamble=f"Delegate via: {DELEGATE_TASK_NAME}(use_subagent=True, subagent_template='<name>')",
            ))
        return "\n\n".join(sections)

    def _render_grouped_section(
        self,
        header: str,
        blocks: list["ContextBlock"],
        *,
        action: str,
        noun: str,
        noun_plural: str,
        preamble: str | None = None,
    ) -> str:
        """按 provider 分块渲染一个能力段（tools / skills / sub-agents 共用）。

        每个有 provider_name 的能力归入对应 provider 子段（#### 标题）；若该 provider
        有 description 则写出，并附「引用时须带前缀 `<prefix>`」提示（prefix 由 provider 名
        qualify 得到，与下方条目名一致）。无 provider_name 的条目（旧路径/测试构造）平铺在
        标题下，保持向后兼容。
        """
        def _line(b: "ContextBlock") -> str:
            name = b.metadata.get("capability_name", "?")
            return f"- {name}: {content_to_text(b.content)}"

        ungrouped: list["ContextBlock"] = []
        grouped: dict[str, list["ContextBlock"]] = {}
        order: list[str] = []
        for b in blocks:
            pname = b.metadata.get("provider_name") or ""
            if not pname:
                ungrouped.append(b)
                continue
            if pname not in grouped:
                grouped[pname] = []
                order.append(pname)
            grouped[pname].append(b)

        lines = [header]
        if preamble:
            lines.append(preamble)
        for b in ungrouped:
            lines.append(_line(b))

        for pname in order:
            group = grouped[pname]
            lines.append("")
            lines.append(f"#### {pname} {noun_plural}")
            pdesc = (group[0].metadata.get("provider_description") or "").strip()
            prefix = qualify(pname) + "__"
            if pdesc:
                lines.append("")
                lines.append(pdesc)
            lines.append(
                f"When {action} these {noun_plural}, prefix the {noun} name with `{prefix}`, "
                "exactly as shown below."
            )
            lines.append("")
            for b in group:
                lines.append(_line(b))
        return "\n".join(lines)

    def _build_capabilities_section(self, blocks: list["ContextBlock"]) -> str:
        """Capabilities（skills/tools/agents）段，带 ## Capabilities 引导标题。空时返回 ""。"""
        resources_section = self._build_resources_section(blocks)
        if not resources_section:
            return ""
        return (
            "## Capabilities\n\n"
            "You can use the capabilities listed below to complete the current task.\n\n"
            + resources_section
        )

    def _build_directive_section(self, blocks: list["ContextBlock"]) -> str:
        """当前 task 的 skill 指令段。空时返回 ""。

        skill 名并入标题（## Instructions for the current task (skill: <name>)）；
        skill 正文里的标题层级整体下移到该 H2 之下（_shift_markdown_headings）。
        """
        directive = self._first_kind(blocks, "directive")
        if not directive:
            return ""
        skill_name = directive.metadata.get("skill_name", "")
        heading = "## Instructions for the current task"
        if skill_name:
            heading = f"{heading} (skill: {skill_name})"
        body = _shift_markdown_headings(content_to_text(directive.content), base_level=2)
        return f"{heading}\n\n{body}"

    def _prepend_to_first_user(
        self, messages: list[LLMMessage], text: str
    ) -> list[LLMMessage]:
        """把 text 拼到首条 user message 内容前（以 --- 分隔）。空 text 为 no-op。"""
        if not text:
            return messages
        out = list(messages)
        for i, m in enumerate(out):
            if m.role == "user":
                base = m.content if isinstance(m.content, str) else content_to_text(m.content)
                out[i] = dataclasses.replace(m, content=f"{text}\n\n---\n\n{base}")
                return out
        return out

    def _append_to_first_user(
        self, messages: list[LLMMessage], text: str
    ) -> list[LLMMessage]:
        """把 text 拼到首条 user message 内容尾部（任务上下文之后）。空 text 为 no-op。"""
        if not text:
            return messages
        out = list(messages)
        for i, m in enumerate(out):
            if m.role == "user":
                base = m.content if isinstance(m.content, str) else content_to_text(m.content)
                out[i] = dataclasses.replace(m, content=f"{base}\n\n{text}")
                return out
        out.append(LLMMessage(role="user", content=text))
        return out

    def _append_to_last_user(
        self, messages: list[LLMMessage], text: str
    ) -> list[LLMMessage]:
        """把 text 拼到末条 user message 内容尾部。无 user message 时追加一条。空 text 为 no-op。"""
        if not text:
            return messages
        out = list(messages)
        for i in range(len(out) - 1, -1, -1):
            if out[i].role == "user":
                base = out[i].content if isinstance(out[i].content, str) else content_to_text(out[i].content)
                out[i] = dataclasses.replace(out[i], content=f"{base}\n\n{text}")
                return out
        out.append(LLMMessage(role="user", content=text))
        return out

    def _history_to_messages(self, history_blocks: list["ContextBlock"]) -> list[LLMMessage]:
        """将 history blocks 转换为真实多轮 LLMMessage 列表。

        跨层（task_conversation + agent_experience）按 timestamp 正序归并，seq_no 作 tiebreak。
        """
        sorted_blocks = sorted(
            history_blocks,
            key=lambda b: (b.metadata.get("timestamp", ""), b.metadata.get("seq_no", 0)),
        )
        messages: list[LLMMessage] = []
        for b in sorted_blocks:
            role = b.metadata.get("role", "user")
            content = content_to_text(b.content)
            tool_calls = b.metadata.get("tool_calls") or [] if role == "assistant" else []
            if not content and not tool_calls:
                continue  # 空文本且无 tool_call 才跳过（保留仅含 tool_call 的 assistant 回合）
            if role == "assistant":
                messages.append(LLMMessage(
                    role="assistant",
                    content=content,
                    tool_calls=[
                        {"id": tc.get("id", ""), "name": tc.get("name", ""), "input": tc.get("input", {})}
                        for tc in tool_calls
                    ],
                ))
            elif role == "tool":
                messages.append(LLMMessage(
                    role="tool",
                    content=content,
                    tool_call_id=b.metadata.get("tool_call_id", ""),
                ))
            else:
                messages.append(LLMMessage(role="user", content=content))
        return messages

    # ── Observer ──────────────────────────────────────────────────────────────

    def _build_act_system(
        self, blocks: list["ContextBlock"], request: "ContextRequest"
    ) -> str:
        """system = act identity(SOUL) + Project Background。observe/compact/metadata 共用。

        与 act 同构：resources 与 skill 指令不进 system；purpose 专属 facet（observe ROLE /
        compact / metadata persona）由 trailing user message 承载，不进 system。
        """
        parts: list[str] = []
        act_facet = request.template.identity.get("act") if request.template else None
        if act_facet and getattr(act_facet, "text", ""):
            parts.append(act_facet.text)
        background = self._first_kind(blocks, "background")
        if background:
            parts.append(f"## Project Background\n\n{content_to_text(background.content)}")
        return "\n\n---\n\n".join(parts)

    def _build_observer_messages(
        self,
        blocks: list["ContextBlock"],
        request: "ContextRequest",
    ) -> list[LLMMessage]:
        """act 风格完整会话 + 尾部一条 observe user message（仅发送，不入 memory）。

        复用 _build_facet_trailing_messages：observe facet（ROLE）+ 判定提示 + 可复核清单。
        subtask 可 confirm/reopen，predecessor 只读。
        """
        bb_blocks = [b for b in blocks if b.kind == "blackboard"]
        subtask_blocks = [b for b in bb_blocks if b.metadata.get("intent") == "subtask"]
        pred_blocks = [b for b in bb_blocks if b.metadata.get("intent") == "predecessor"]

        extra_sections: list[str] = []
        if subtask_blocks:
            extra_sections.append(
                "Your sub-task results (you may confirm / reopen these):\n"
                + self._render_bb(subtask_blocks)
            )
        if pred_blocks:
            extra_sections.append(
                "Upstream task results (read-only context):\n" + self._render_bb(pred_blocks)
            )

        return self._build_facet_trailing_messages(
            blocks,
            request,
            _OBSERVE_JUDGMENT_CUE,
            extra_sections=extra_sections,
            facet_fallback=_OBSERVER_ROLE_FALLBACK,
        )

    @staticmethod
    def _render_bb(blks: list["ContextBlock"]) -> str:
        """渲染 blackboard 结果块为复核清单行。"""
        lines: list[str] = []
        for b in blks:
            title = b.metadata.get("title", "")
            outcome = b.metadata.get("outcome", "")
            body = content_to_text(b.content)
            head = f"{title} [{outcome}]" if title else (f"[{outcome}]" if outcome else "")
            lines.append(f"- {head}: {body}" if head else f"- {body}")
        return "\n".join(lines)

    # ── 共用工具 ──────────────────────────────────────────────────────────────

    def _first_kind(
        self,
        blocks: list["ContextBlock"],
        kind: str,
    ) -> "ContextBlock | None":
        for b in blocks:
            if b.kind == kind:
                return b
        return None

    def _collect_llm_tools(self, blocks: list["ContextBlock"]) -> list[LLMTool]:
        """从 capabilities blocks 抽出 LLMTool 数组传给 LLM API。"""
        tools: list[LLMTool] = []
        for b in blocks:
            if b.kind != "capabilities":
                continue
            llm_tool = b.metadata.get("llm_tool")
            if isinstance(llm_tool, LLMTool):
                tools.append(llm_tool)
            elif b.metadata.get("capability_kind") == "tool":
                # 构造一个 LLMTool
                tools.append(LLMTool(
                    name=b.metadata.get("capability_name", "?"),
                    description=content_to_text(b.content),
                    input_schema=b.metadata.get("input_schema", {}),
                ))
        return tools
