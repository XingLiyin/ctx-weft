"""Composer：把 ContextBlock 渲染成最终 AssembledPrompt。

格式与 miniAgents prompt_builder.py 完全对齐——这是 refactor 不是 redesign。

Actor system prompt（用 --- 分隔，仅 soul + 项目背景，per-task 资源不入此处）：
  soul（identity）
  ---
  ## Project Background\n\n{background}

Actor messages（多轮 LLMMessage：history 轮次在前，最后一条 user 含任务上下文）。
resources（skills/tools/agents）+ 当前 task 的 skill 指令以前缀形式拼到**当前 task 的首条** user
message（首条 task_conversation 来源的 user 回合；fresh task 时即末尾任务上下文那条，而非整个
列表里更早的跨 task agent_experience 回合）之前（仅发送，不入 memory）：
  ### Available Skills / ### Available Tools / ### Available Sub-Agents
  ## Instructions for the current task\n\n{skill_instructions / directive}
  ---
  {该首条 user message 原内容}
后续任务上下文 user message：
  ## Current Task
  {task.title}
  {task.description}
  ## Current Message
  {task.user_prompt}
  历史轮次（独立 LLMMessage）：
  User: {content}
  Assistant: {content} + tool_calls
  Tool: {result}

（Phase 3 2026-06-30: ## Task Background blackboard 段已移除；predecessor 结果经 memory recall 获取。）

Observer system prompt（与 act 同构）：
  role/soul（act identity）
  ---
  ## Project Background\n\n{background}

Observer messages：复用 actor messages（同样把 resources + skill 指令注入首条 user
message），再追加一条尾部 user message（仅发送，不入 memory）：
  {observe ROLE（identity）}
  ---
  {判定提示}
  ## Your sub-tasks（当有 extra["subtask_reviews"] 时）:
  - {task_id} — {title} [{outcome}]
"""

from __future__ import annotations

import dataclasses
import re
from abc import abstractmethod
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

from ctx_weft.protocols import LLMMessage, LLMTool
from ctx_weft.protocols.capability import qualify
from ctx_weft.core.utils import content_to_text, estimate_tokens, PROGRESS_SO_FAR_HEADING
from ctx_weft.core.orchestrator.control_capability import (
    DELEGATE_TASK_NAME,
    REPORT_TASK_OUTCOME_NAME,
)

if TYPE_CHECKING:
    from ctx_weft.core.assembler.assembler import (
        AssembledPrompt,
        ContextBlock,
        ContextRequest,
    )


_OBSERVER_ROLE_FALLBACK = "You are an objective observer evaluating task execution results."

# 尾部 observe user message 的判定提示（拼在 ROLE 之后）。
_OBSERVE_JUDGMENT_CUE = (
    "Now act as the observer for the current task. Based on the execution above, judge the "
    f"task's completion status and call `{REPORT_TASK_OUTCOME_NAME}` exactly once with: a `task_status` "
    "of `success` / `retry` / `fail`; an `act_recap` honestly recapping ONLY this act segment — the actor's "
    "execution AFTER the most recent `## Progress So Far` section (that section is the previous observation's "
    "recap; if there is none this is the first observation, so start after `## Current Task` / the user's "
    "message). Don't re-narrate anything before that point. "
    "And — when status is success/fail — a concise `task_summary`: the important steps and lessons of the "
    "whole task (a process report, not verbose, and NOT the final output), incorporating the results of any "
    "sub-tasks you dispatched. Optionally review your own sub-tasks via `task_reviews`. Call no other tools."
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

_BACKGROUND_BOUNDARY_DESC = {
    "interrupt": "本段被用户打断（中途打断）",
    "plain_text": "你以散文回复后让位用户、暂停等待用户输入",
    "finish": "任务已通过 finish_task 收尾",
    "normal": "任务以最终产出正常结束",
}


# close 段（finish/normal）：actor 以 finish_task 收尾，其 result 落 task.outputs。
# 与 loop.steps.background_observe._CLOSE_BOUNDARIES 保持一致（此处避免跨层 import）。
_CLOSE_BOUNDARIES = {"finish", "normal"}


def _background_observe_cue(boundary: str) -> str:
    desc = _BACKGROUND_BOUNDARY_DESC.get(boundary, _BACKGROUND_BOUNDARY_DESC["normal"])
    is_close = boundary in _CLOSE_BOUNDARIES
    summary_ask = (
        " 并给出 `task_summary`：整个 task 执行历程的简洁 process report（点出重要步骤与经验，不琐碎；"
        "不是最终输出），须综合已完成子任务（sub-task）的结果。"
        if is_close else ""
    )
    return (
        f"当前 task 的状态：{desc}。请基于以上执行过程，调用 `collect_process_report` 一次："
        "给出 `act_recap`（只复述本段 act——对话里最后一个 `## Progress So Far` 之后 actor 新做的执行；"
        "若没有该标题则为首次观察，从 `## Current Task` / 用户消息之后算起；该点之前不要回头重述）"
        + summary_ask +
        " 只需总结，无需判断 success/retry/fail，不要调用其他工具。"
    )


def _finish_result_section(request) -> str:
    """close 段（finish/normal）把 actor 的最终产出（task.outputs）注入 prompt。

    finish_task 是 SILENT 工具：其 result 进 task.outputs，**不写任务层对话**；delegate_task
    是 DISPATCH 工具、也排除出对话重建。若某段仅由 finish(+delegate) 组成，从记忆重建的对话里
    看不到任何 actor 动作，观察者会**虚构**一段完成叙述。把 task.outputs 显式喂进来，让它据实总结。
    返回空串表示无产出可注入（保持原行为）。
    """
    task = getattr(request, "task", None)
    outputs = getattr(task, "outputs", None) if task is not None else None
    if not outputs:
        return ""
    text = outputs if isinstance(outputs, str) else content_to_text(outputs)
    text = (text or "").strip()
    if not text:
        return ""
    return (
        "## Actor 的最终产出（已通过 finish_task 收尾本段）\n\n"
        f"{text}\n\n"
        "（上面是 actor 提交的最终结果，是本段唯一权威的产出依据。请据此如实总结本段进展，"
        "不要臆测未实际发生的工具调用、步骤或产物。）"
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
        from ctx_weft.core.assembler.assembler import AssembledPrompt

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
        elif request.purpose == "background_observe":
            system = self._build_act_system(blocks, request)
            messages = self._build_background_observe_messages(blocks, request)
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
          [N]      user: Current Task + Progress So Far + Current Message
        """
        # Phase 3 (2026-06-30): ## Task Background (blackboard predecessor blocks) removed.
        # Predecessors now surface via memory recall (Phase 2 inherit/recall); no bb_blocks needed.
        history_blocks = [b for b in blocks if b.kind == "history"]

        # Progress So Far (retry feedback): render as a chronologically-placed history
        # user-block (via task.process_report_at) so it sits right after the attempt that
        # produced it and before the next one — instead of floating to the end (which confusingly
        # re-states stale progress after the new attempt in the observe prompt). Send-only (not
        # persisted to memory). Without a timestamp it is NOT rendered at all (no trailing
        # fallback) so it can never be mis-placed; in practice the timestamp is always set
        # alongside process_report, so this only affects unexpected/legacy timestamp-less data.
        # max_turns 那轮 compact 直接复用 process_report 作 TASK_COMPACT_SUMMARY → 报告已在 task 层
        # 历史里，跳过单独的 Progress So Far block，避免同一份报告渲染两遍（Option A 去重）。
        progress_as_history = (
            None if self._progress_already_in_compact(request.task, history_blocks)
            else self._progress_history_block(request.task)
        )
        if progress_as_history is not None:
            history_blocks = [*history_blocks, progress_as_history]

        history_pairs = self._history_to_messages_with_sources(history_blocks)
        messages: list[LLMMessage] = [m for m, _src, _mtype in history_pairs]
        # 当前 task 的 user 回合（directive 的落点）：history 里**最后**一条 USER_PROMPT 来源的
        # user message（task_conversation / agent_recall 两种 source 均可能承载）。
        # task-resident 胶囊下，先前已结束 task 的 raw body（含其 USER_PROMPT）也会经 agent_recall
        # 召回进 history，故同时存在多条 user_prompt；当前 task 的永远是最近一条——取末条而非首条，
        # 与 _frame_current_message（同样取末条作 "## Current Message"）保持一致。
        # 找不到（fresh task）则留到下方追加的当前任务上下文 user message。
        current_task_user_idx = None
        for i, (m, src, mtype) in enumerate(history_pairs):
            if m.role == "user" and mtype == "user_prompt":
                current_task_user_idx = i

        task = request.task
        spec_title, spec_desc, spec_prompt = self._task_spec_fields(blocks, task)
        parts: list[str] = []

        if not task.user_prompt_in_memory:
            # daemon 或尚未持久化的路径：实时构建完整的用户消息（spec 取自 task_spec block）
            if spec_title and spec_desc:
                parts.append(f"## Current Task\n{spec_title}\n{spec_desc}")
            elif spec_title:
                parts.append(f"## Current Task\n{spec_title}")
            if getattr(task, "process_report", None):
                parts.append(f"{PROGRESS_SO_FAR_HEADING}\n{task.process_report}")
            if spec_prompt:
                parts.append(
                    f"## Current Message\n{spec_prompt}\n\n"
                    "（Reply in the same language as the Current Message above.）"
                )
        else:
            # in-memory：渲染期就地装饰最近一条 task_conversation user 回合
            self._frame_current_message(messages, history_pairs, task, blocks)

        if parts:
            # 这条实时构建的当前任务上下文也是「当前 task」回合；history 里没有 task_conversation
            # user 时（fresh task），directive 落到它身上。
            if current_task_user_idx is None:
                current_task_user_idx = len(messages)
            messages.append(LLMMessage(role="user", content="\n\n".join(parts)))
        # 兜底：actor prompt 必须以 user 回合结尾——避免以 assistant/tool 结尾让模型困惑地续写自己。
        # 正常情况下 active/retry 的 Progress So Far 已是末条 user；此处仅覆盖 summary 为空等边角。
        if messages and messages[-1].role != "user":
            messages.append(LLMMessage(role="user", content="Continue with the task above."))
        # 连续同角色 / 孤立 tool result 的合法化不在装配层做——统一交由
        # loop.llm_gateway.stream_llm 在发送前处理，使装配层不反向依赖 loop。
        merged = messages
        # Resources 注入（仅发送，不入 memory）：
        #   - directive（当前 task 指令）：act 追加到首条 user message 尾部（紧跟 ## Current Task
        #     等任务上下文之后）；其他 purpose 仍前置到首条。
        #   - capabilities（skills/tools/agents）：所有 purpose 一律放到末条 user message——
        #     act 紧邻 guidance / finish_task 提升工具调用积极性；observe/compact/recognize_intent
        #     也放末条，避免把整段能力清单压在任务消息之前喧宾夺主（与 act 一致）。
        directive_text = self._build_directive_section(blocks)
        capabilities_text = self._build_capabilities_section(blocks)
        if getattr(request, "purpose", None) == "act":
            # directive 落到「当前 task」的 user message（紧跟任务上下文之后），即 history 里末条
            # user_prompt——而不是整个 message 列表的第一条 user（可能是更早的、经 agent_recall
            # 召回的已结束 task 回合）。兜底退回末条 user。
            target_idx = current_task_user_idx
            if target_idx is None:
                target_idx = self._last_user_index(merged)
            merged = self._append_to_user_at(merged, target_idx, directive_text)
        else:
            if directive_text:
                merged = self._prepend_to_first_user(merged, directive_text)
        merged = self._append_to_last_user(merged, capabilities_text)
        return merged

    def _frame_current_message(self, messages, history_pairs, task, blocks=None) -> None:
        """In-memory 路径：把最近一条 USER_PROMPT user message 包成当前消息框架（不落库）。

        history_pairs 是 _history_to_messages_with_sources 返回的 (msg, src, mem_type) 三元组。
        识别当前消息只依赖 mem_type=="user_prompt"，与来源无关（兼容 agent_recall 及历史 task_conversation 标签）。
        spec（title/description）取自 task_spec block 的 metadata（无块时回退直读 task）。
        """
        target = None
        for i, (m, src, mtype) in enumerate(history_pairs):
            if m.role == "user" and mtype == "user_prompt":
                target = i
        if target is None:
            return
        raw = content_to_text(messages[target].content)
        spec_title, spec_desc, _ = self._task_spec_fields(blocks, task)
        prefix = ""
        if spec_title and spec_desc:
            prefix = f"## Current Task\n{spec_title}\n{spec_desc}\n\n"
        elif spec_title:
            prefix = f"## Current Task\n{spec_title}\n\n"
        framed = (
            f"{prefix}## Current Message\n{raw}\n\n"
            "（Reply in the same language as the Current Message above.）"
        )
        messages[target] = LLMMessage(role="user", content=framed)

    def _build_facet_trailing_messages(
        self,
        blocks: list["ContextBlock"],
        request: "ContextRequest",
        cue: str,
        extra_sections: list[str] | None = None,
        pre_cue_sections: list[str] | None = None,
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
        if pre_cue_sections:
            sections.extend(pre_cue_sections)
        sections.append(cue)
        if extra_sections:
            sections.extend(extra_sections)

        # facet + cue 并入最后一条 user 回合（_build_actor_messages 已保证以 user 收尾），
        # 就地避免连续 user，不再依赖发送前合并。
        return self._append_to_last_user(messages, "\n\n".join(sections))

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

    def _append_to_user_at(
        self, messages: list[LLMMessage], idx: int | None, text: str
    ) -> list[LLMMessage]:
        """把 text 拼到 messages[idx]（应为 user 回合）内容尾部。idx 越界/None 或空 text 为 no-op。"""
        if not text or idx is None or not (0 <= idx < len(messages)):
            return messages
        out = list(messages)
        m = out[idx]
        base = m.content if isinstance(m.content, str) else content_to_text(m.content)
        out[idx] = dataclasses.replace(m, content=f"{base}\n\n{text}")
        return out

    def _last_user_index(self, messages: list[LLMMessage]) -> int | None:
        """末条 user message 的下标；无 user 时返回 None。"""
        for i in range(len(messages) - 1, -1, -1):
            if messages[i].role == "user":
                return i
        return None

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

    def _progress_already_in_compact(self, task, history_blocks: list["ContextBlock"]) -> bool:
        """process_report 是否已作为本轮 TASK_COMPACT_SUMMARY 出现在历史里（max_turns compact 复用
        了它）。内容精确相等才算（二者同出 verdict.act_recap）；规则降级的独立摘要内容不同，不会误删。"""
        progress = getattr(task, "process_report", None)
        if not progress:
            return False
        from ctx_weft.protocols import MemoryEventType
        for b in history_blocks:
            if b.metadata.get("type") != MemoryEventType.TASK_COMPACT_SUMMARY:
                continue
            content = b.content if isinstance(b.content, str) else content_to_text(b.content)
            # 段摘要经 _history 渲染后可能冠了 PROGRESS_SO_FAR_HEADING（role=assistant、task_conversation
            # 来源）；裸串与带标题串都算"已在 compact 里"，避免漏判导致进度块重复渲染。
            if content == progress or content == f"{PROGRESS_SO_FAR_HEADING}\n{progress}":
                return True
        return False

    def _progress_history_block(self, task) -> "ContextBlock | None":
        """Progress So Far 作为带时间戳的 history user-block；缺时间戳/无 progress 时返回 None
        （由调用方退回"追加末尾"的 legacy 行为）。"""
        if not getattr(task, "user_prompt_in_memory", False):
            return None
        progress = getattr(task, "process_report", None)
        progress_at = getattr(task, "process_report_at", None)
        if not progress or progress_at is None:
            return None
        from ctx_weft.core.assembler.assembler import ContextBlock
        from ctx_weft.core.utils import generate_id
        text = f"{PROGRESS_SO_FAR_HEADING}\n{progress}"
        return ContextBlock(
            id=generate_id("blk"), source="current_progress", kind="history", target="messages",
            content=text, priority=3, token_estimate=estimate_tokens(text),
            # seq_no 取大值：万一 process_report_at 与某轮 timestamp 相等，也排在该轮之后。
            metadata={"role": "user", "timestamp": progress_at.isoformat(), "seq_no": 10**9},
        )

    def _history_to_messages(self, history_blocks: list["ContextBlock"]) -> list[LLMMessage]:
        """将 history blocks 转换为真实多轮 LLMMessage 列表。

        跨层（task_conversation + agent_experience）按 timestamp 正序归并，seq_no 作 tiebreak。
        """
        return [m for m, _src, _mtype in self._history_to_messages_with_sources(history_blocks)]

    def _history_to_messages_with_sources(
        self, history_blocks: list["ContextBlock"]
    ) -> list[tuple[LLMMessage, str, str]]:
        """同 _history_to_messages，但每条 message 附带来源 block.source 和 memory event type。

        返回 (LLMMessage, source, mem_type) 三元组。
        - source: 来源标识（"agent_recall" / "task_conversation" / "agent_experience" 等）
        - mem_type: MemoryEventType str（如 "user_prompt" / "agent_conversation_turn"）；
          无 type 的合成 block（如 capabilities / progress）传空字符串。
        用于区分「当前 task 自有对话」（USER_PROMPT）与跨 task 的 agent_experience 回合——
        directive 注入和 ## Current Message 框架需定位到当前 task 的首条 user_prompt 回合。
        """
        sorted_blocks = sorted(
            history_blocks,
            key=lambda b: (b.metadata.get("timestamp", ""), b.metadata.get("seq_no", 0)),
        )
        out: list[tuple[LLMMessage, str, str]] = []
        for b in sorted_blocks:
            role = b.metadata.get("role", "user")
            content = content_to_text(b.content)
            tool_calls = b.metadata.get("tool_calls") or [] if role == "assistant" else []
            if not content and not tool_calls:
                continue  # 空文本且无 tool_call 才跳过（保留仅含 tool_call 的 assistant 回合）
            if role == "assistant":
                msg = LLMMessage(
                    role="assistant",
                    content=content,
                    tool_calls=[
                        {"id": tc.get("id", ""), "name": tc.get("name", ""), "input": tc.get("input", {})}
                        for tc in tool_calls
                    ],
                )
            elif role == "tool":
                msg = LLMMessage(
                    role="tool",
                    content=content,
                    tool_call_id=b.metadata.get("tool_call_id", ""),
                )
            else:
                msg = LLMMessage(role="user", content=content)
            mem_type = str(b.metadata.get("type", ""))
            out.append((msg, b.source, mem_type))
        return out

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
        # Phase 3 (2026-06-30): blackboard subtask/predecessor block rendering removed.
        # Predecessor results surface via memory recall (Phase 2); subtask review handles come
        # from task_manager via request.extra["subtask_reviews"] (Task 1 below).
        # The blackboard mechanism (subscribe_topic/recall_topic/BlackboardSource) is kept intact.

        extra_sections: list[str] = []

        # Phase 3: reviewable sub-tasks come from task_manager via request.extra (not blackboard).
        # The observer reads each child's RESULT from the conversation (Phase 2); this clause only
        # surfaces the actionable handles (task_id/title/outcome) so it can confirm/reopen via task_reviews.
        reviews = (getattr(request, "extra", {}) or {}).get("subtask_reviews") or []
        if reviews:
            lines = ["## Your sub-tasks (confirm / reopen via `task_reviews`, referencing the task_id):"]
            for r in reviews:
                lines.append(f"- {r['task_id']} — {r.get('title', '')} [{r.get('outcome', '')}]")
            extra_sections.append("\n".join(lines))

        # finish_task 的产出走 SILENT，不入 task 层、不在重建的对话里——但 observer 须看到 actor
        # 最终提交了什么。显式补一段并标注来源（act 阶段调用 finish_task 的结果），置于判定提示之前。
        pre_cue_sections: list[str] = []
        outputs = getattr(request.task, "outputs", None)
        outputs_text = outputs if isinstance(outputs, str) else (content_to_text(outputs) if outputs else "")
        if outputs_text:
            pre_cue_sections.append(
                "## Final output\n"
                "The actor ended the act phase by calling the `finish_task` tool; the result it "
                "submitted (shown to the user) was:\n\n"
                + outputs_text
            )

        return self._build_facet_trailing_messages(
            blocks,
            request,
            _OBSERVE_JUDGMENT_CUE,
            extra_sections=extra_sections,
            pre_cue_sections=pre_cue_sections,
            facet_fallback=_OBSERVER_ROLE_FALLBACK,
        )

    def _build_background_observe_messages(self, blocks, request):
        """act 风格会话 + 尾部 background-observe cue（ROLE facet + boundary 状态 + 只给 process_report）。

        close 段（finish/normal）额外把 actor 的最终产出（task.outputs）注入 cue 之前——否则仅由
        finish(+delegate) 组成的段在对话重建里无 actor 动作可见，观察者会虚构完成叙述。
        """
        boundary = (getattr(request, "extra", {}) or {}).get("observe_boundary", "normal")
        pre_cue: list[str] | None = None
        if boundary in _CLOSE_BOUNDARIES:
            section = _finish_result_section(request)
            if section:
                pre_cue = [section]
        return self._build_facet_trailing_messages(
            blocks, request, _background_observe_cue(boundary),
            pre_cue_sections=pre_cue,
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

    def _task_spec_fields(self, blocks, task) -> tuple[str, str, str]:
        """当前 task 的 spec 字段 (title, description, user_prompt)。

        优先取 TaskSpecSource 产的 task_spec block 的 metadata；无块时回退直读 task
        （兼容手构 blocks / 未注册 TaskSpecSource 的调用）。block 是元数据载体，
        composer 用它去就地装饰当前消息，而非把它当独立消息渲染。
        """
        blk = self._first_kind(blocks, "task_spec") if blocks else None
        if blk is not None:
            md = blk.metadata
            return (
                md.get("title", "") or "",
                md.get("description", "") or "",
                md.get("user_prompt", "") or "",
            )
        title = task.title or ""
        description = task.description or ""
        up = task.user_prompt
        user_prompt = up if isinstance(up, str) else (content_to_text(up) if up else "")
        return title, description, user_prompt

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
