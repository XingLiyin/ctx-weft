"""Adapt current loop/memory data to prompt rendering."""
from __future__ import annotations

import re
from typing import TYPE_CHECKING

from ctx_weft.core.assembler.prompts import (
    BLOCK_FALLBACK,
    HEADINGS,
    connector,
    render_template_file,
)
from ctx_weft.core.assembler.request_policy import (
    ReviewIntent,
    compile_prompt_policy,
    resolve_prompt_policy,
    select_identity_facet,
)
from ctx_weft.core.utils.content import (
    content_to_text,
)
from ctx_weft.core.utils.task_ref import task_label, task_ref_parts
from ctx_weft.protocols import LLMMessage
from ctx_weft.protocols.capability import qualify

_RENDERABLE_KINDS = frozenset({
    "identity", "background", "task_spec", "history",
    "capabilities", "directive", "guidance",
})

if TYPE_CHECKING:
    from ctx_weft.core.assembler.assembler import (
        ContextBlock,
    )


#: 边界 → 要不要把 actor 的最终产出注入 prompt。**这是机制开关，不是文案**，所以留在 core：
#: 事实句的正文住 Composer asset 的 `review/boundary.<locale>.jinja`。
#:
#: 注入与否按边界定，不按前台/后台定：`finish_task` 是 SILENT 工具、产出不写任务层对话，不喂
#: 进来观察者只能虚构一段完成叙述；而 `plain_text` / `normal` 的产出**本身就是**一条 assistant
#: 回合、在重建的对话里看得见，注入等于让它出现两遍。
#:
#: 前台与后台共用这一张表（2026-09-28）：此前两边是两个函数、两套措辞，且都把「调了
#: `finish_task`」写死在标题里——于是 `normal` 边界（纯文本收尾，压根没调）会拿到一条自相
#: 矛盾的 prompt，而那份 recap 是要进记忆当段摘要的。
_BOUNDARY_INJECTS: dict[str, bool] = {
    # —— 以 finish_task 收尾：产出不在对话里，必须注入 ——
    "actor_done": True,
    "finish": True,
    "finish_park": True,
    # —— 以消息正文收尾：那段正文就在对话里 ——
    "normal": False,
    "plain_text": False,
    # —— 压根没跑完 ——
    "max_turns": False,
    "context_limit": False,
    "interrupt": False,
    "dispatch": False,
    "mechanical": False,
}

#: 边界 → (这一段是**怎么结束**的, 要不要注入产出)。正文从连接词包取，开关来自上表。
_BOUNDARY_FACTS: dict[str, tuple[str, bool]] = {
    b: (connector("review.boundary", boundary_kind=b), inject) for b, inject in _BOUNDARY_INJECTS.items()
}

_DEFAULT_BOUNDARY = "normal"

#: 与 loop.steps.observe.CLOSE_BOUNDARIES 保持一致（此处避免跨层 import）。
CLOSE_BOUNDARIES = {"finish", "normal"}

#: 要把 `task.outputs` 注入观察 prompt 的边界，由上表推出（唯一真相源是那张表）。
_OUTPUTS_BEARING_BOUNDARIES = frozenset(b for b, inject in _BOUNDARY_INJECTS.items() if inject)


def _boundary_fact(boundary: str) -> str:
    return _BOUNDARY_FACTS.get(boundary, _BOUNDARY_FACTS[_DEFAULT_BOUNDARY])[0]


def _task_anchor(request) -> str:
    """生成点附近的任务锚（2026-09-28）。

    act 的 guidance 有一行**恒有**的锚定行，理由写在那里：「长对话/续跑/追问回合里
    `## Current Task` 框远在历史深处，此处是生成点附近唯一的任务锚」。observe 的处境一样、而且
    更糟——判定边界恰恰多发生在追问之后——但 guidance 是 act-only，observe 从来没有这道锚。

    与 act 共用 `task_label`：title 空时退回开启该 task 的 prompt 首行（root task 在
    `recognize_intent` 填完标题之前恒空）。**不带 description**，那由 `## Current Task` 框承载
    ——与 act 的取舍逐字一致。
    """
    task = getattr(request, "task", None)
    ref = task_label(task) if task is not None and getattr(task, "id", None) else ""
    return (connector("common.task_anchor", label=ref) if ref
            else connector("common.task_anchor.unknown"))


def _finish_result_text(request) -> str:
    """取 actor 的最终产出；标题和证据说明由 review 完整模板负责。

    为什么要注入：`finish_task` 是 SILENT 工具，它的 result 进 `task.outputs`、**不写任务层
    对话**；`delegate_task` 是 DISPATCH 工具、同样排除出对话重建。若某段仅由 finish(+delegate)
    组成，从记忆重建的对话里看不到任何 actor 动作，观察者会**虚构**一段完成叙述。

    谁该注入由 `_BOUNDARY_FACTS` 那张表定（`_OUTPUTS_BEARING_BOUNDARIES` 由它推出），本函数只
    做内容正规化。返回空串表示无产出可注入。
    """
    task = getattr(request, "task", None)
    outputs = getattr(task, "outputs", None) if task is not None else None
    if not outputs:
        return ""
    text = outputs if isinstance(outputs, str) else content_to_text(outputs)
    text = (text or "").strip()
    if not text:
        return ""
    return text


_HEADING_RE = re.compile(r"^(#{1,6})\s")

# 图片策略由 RequestPolicy 明示（用户裁定 D2）。只有 act 需要模型真看图：
# compact 恰在上下文超预算时触发且产出按 spec §8 恒为纯文本；recognize_intent 是填元数据；
# observe 判任务成败靠 actor 产出与工具结果。其余 purpose 的图在
# compose 出口降级成文本占位（见 compose 末尾）。


_JSON_TYPE_ABBR = {
    "string": "str", "integer": "int", "number": "num", "boolean": "bool",
    "array": "list", "object": "obj", "null": "null",
}

# 单个工具最多列几个参数：MCP 工具偶有几十个入参，全列会把索引撑爆，超出部分折成省略号。
_MAX_SIGNATURE_PARAMS = 8


def _abbr_type(spec: object) -> str:
    """JSON Schema 的 type 字段 → 短名；联合类型取首个，缺失为 any。"""
    if not isinstance(spec, dict):
        return "any"
    t = spec.get("type")
    if isinstance(t, list):
        t = t[0] if t else None
    if not isinstance(t, str):
        return "any"
    return _JSON_TYPE_ABBR.get(t, t)


def _compact_signature(input_schema: object) -> str:
    """input_schema → ``a: str, b?: int`` 形式的紧凑入参签名（可选参数带 ?）。

    工具的完整描述与 JSON Schema 随请求的 tools 参数一并下发，prompt 里这份清单只作索引：
    留名字与参数形状，够模型判断「有没有这个能力、要准备什么」，细节去 tool 定义里看。
    """
    if not isinstance(input_schema, dict):
        return ""
    props = input_schema.get("properties")
    if not isinstance(props, dict) or not props:
        return ""
    required = input_schema.get("required")
    required = set(required) if isinstance(required, (list, set, tuple)) else set()
    parts: list[str] = []
    for pname, spec in list(props.items())[:_MAX_SIGNATURE_PARAMS]:
        mark = "" if pname in required else "?"
        parts.append(f"{pname}{mark}: {_abbr_type(spec)}")
    if len(props) > _MAX_SIGNATURE_PARAMS:
        parts.append("…")
    return ", ".join(parts)


def _is_named_skill(block: ContextBlock, skill_name: str) -> bool:
    """capability block 是否就是名为 skill_name 的那个 skill。

    只认 qualified 名（``provider__name``），与 PrepareStep 加载 skill 正文时用的
    capability_cache.get_by_qualified_name 同一口径——两个决定（正文进不进 prompt、
    条目从不从清单里剔除）由同一个键决定，不会一边命中一边落空。

    不接受裸名兜底：裸名跨 provider 撞车（``local_skill:pdf`` 与 ``mcp:skills:pdf``
    都叫 pdf），一剔就是一片；且裸名在 get_by_qualified_name 下本就查不到 skill 定义，
    正文根本没进 prompt，此时再把条目藏掉是双输。
    """
    md = block.metadata
    name = md.get("capability_name") or qualify(str(md.get("capability_id", "")))
    return name == skill_name


def _is_blank_content(content: object) -> bool:
    """内容是否真的为空。

    与 llm_gateway._is_empty_content 同义但更严格地只判「有无内容」：
    str 看是否空；列表看是否为空、或是否只含空 TextPart。
    非文本 part（图片）一律视为有内容——纯图片消息不是空消息。

    注意：本函数**不** strip——纯空白文本（如 "   "）在此判为非空，与
    llm_gateway._is_empty_content（会 strip，纯空白判空）不同义。今日安全仅因为
    legalize_messages 链路最终会再经 _is_empty_content 把纯空白消息滤掉一道；
    这对不同判据不是巧合但也没人写下来过，改动前请先看
    tests/unit/test_composer_vs_gateway_blank_content.py。
    """
    if content is None:
        return True
    if isinstance(content, str):
        return not content
    for p in content:
        if not hasattr(p, "text"):
            return False        # 图片 = 有内容
        if p.text:
            return False
    return True


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


class LoopPromptAdapter:
    """Loop adaptation and legacy private helper entrypoints."""

    def prepare(self, blocks, request, policy=None):
        """Resolve loop-specific positions into ordinary message references and slots."""
        from ctx_weft.core.assembler.prompt_document import MessageRef, PromptDocument
        from ctx_weft.core.assembler.prompts import load_prompt_template

        policy = policy or resolve_prompt_policy(request)
        entry = load_prompt_template(policy.template)
        pairs = self._history_to_messages_with_sources([b for b in blocks if b.kind == "history"])
        # References identify occurrences, not mutable list positions or non-unique source IDs.
        document = PromptDocument([p[0] for p in pairs],
                                  refs=[MessageRef(f"history:{i}") for i in range(len(pairs))])
        task = request.task
        title, description, user_prompt, task_ref = self._task_spec_fields(blocks, task)
        extra = getattr(request, "extra", {}) or {}
        if policy.template == "observe":
            # facet 标签决定内容含义；无标签的旧 block 仅代表请求的主 facet。
            identities = [b for b in blocks if b.kind == "identity"]
            legacy = next((b for b in identities if not b.metadata.get("facet_purpose")), None)
            selected = next((b for b in identities
                             if b.metadata.get("facet_purpose") == policy.facet), legacy)
            summary = next((b for b in identities if b.metadata.get("facet_purpose") == "compact"),
                           selected if policy.facet == "compact" else None)
        else:
            selected = self._first_kind(blocks, "identity")
        background = self._first_kind(blocks, "background")
        persona = select_identity_facet(getattr(request, "template", None), "act")
        soul = (content_to_text(selected.content) if selected else "") if policy.template == "act" else (
            getattr(persona, "text", "") if persona else "")
        role = content_to_text(selected.content) if selected else ""
        if not role and policy.facet == "compact" and policy.template != "observe":
            role = BLOCK_FALLBACK["compact"]
        ctx = dict(
            title=title, description=description, user_prompt=user_prompt, task_ref=task_ref,
            headings=HEADINGS, soul=soul,
            project_background=content_to_text(background.content) if background else "",
            directive=self._build_directive_section(blocks),
            capabilities=self._build_capabilities_section(blocks, current_skill_name=extra.get("skill_name", "")),
            guidance="", resume=extra.get("act_resume_cue", ""), needs_pointer=False,
            scope=policy.scope, judge_task=policy.judge_task,
        )
        if policy.template == "observe":
            ctx.update(
                evaluation_role=role if policy.judge_task else "",
                summary_principles=content_to_text(summary.content) if summary else "",
            )
        else:
            ctx["role_section"] = render_template_file(
                "layouts/role.jinja", heading=HEADINGS["role"], body=role)
        resume_context = extra.get("act_resume_context")
        if policy.template == "act" and resume_context is not None:
            ctx["resume"] = entry.render("resume_snapshot", resume_context)
        anchor_index = self._current_task_user_index(pairs, getattr(task, "id", ""))
        anchor = document.refs[anchor_index] if anchor_index is not None else None
        bindings = {}
        if not task.user_prompt_in_memory:
            fresh = entry.render("fresh", ctx)
            if fresh:
                ref = document.append(LLMMessage(role="user", content=fresh))
                if anchor is None:
                    anchor = ref
        elif anchor is not None:
            latest_index = self._latest_task_user_index(pairs, getattr(task, "id", ""))
            latest = document.refs[latest_index if latest_index is not None else anchor_index]
            bindings["context"] = (anchor,)
            bindings["input"] = (latest,)
            if anchor != latest:
                bindings["opening"] = (anchor,)

        if policy.template == "act" and document.messages and document.messages[-1].role != "user":
            document.append(LLMMessage(role="user", content=entry.render("resume", ctx)))

        user_refs = [ref for ref, message in zip(document.refs, document.messages, strict=True)
                     if message.role == "user"]
        if policy.template == "act":
            target = anchor or (user_refs[-1] if user_refs else None)
            if target is not None:
                bindings["directive_after"] = (target,)
        elif user_refs:
            bindings["directive_before"] = (user_refs[0],)

        cap_ref = None
        if ctx["capabilities"]:
            cap_ref = anchor or (user_refs[-1] if user_refs else None)
            if cap_ref is None:
                cap_ref = document.append(LLMMessage(role="user", content=ctx["capabilities"]))
            else:
                bindings["resources"] = (cap_ref,)
        document.decorate(entry, bindings=bindings, context=ctx)
        if policy.template == "act":
            guidance = self._first_kind(blocks, "guidance")
            ctx["guidance"] = content_to_text(guidance.content) if guidance else ""
        # Act resumes already have a trailing user. Preserve the existing pointer rule.
        ctx["needs_pointer"] = cap_ref is not None and cap_ref != document.refs[-1]
        if policy.template == "observe":
            boundary = extra.get("observe_boundary") or _DEFAULT_BOUNDARY
            judging = policy.judge_task
            ctx.update(task_anchor=_task_anchor(request), boundary=_boundary_fact(boundary),
                       final_output=_finish_result_text(request) if boundary in _OUTPUTS_BEARING_BOUNDARIES else "")
            if judging:
                ctx.update(plain_text_yielded=boundary == "plain_text",
                           finish_park_yielded=boundary == "finish_park",
                           subtasks=self._observe_subtasks_text(extra.get("subtasks") or []))
            else:
                ctx["closed"] = boundary in CLOSE_BOUNDARIES
        return document, entry, ctx

    def _build_actor_messages(self, blocks, request, definition=None):
        """Compatibility helper: render conversation decorations without the purpose body."""
        document, entry, ctx = self.prepare(blocks, request, definition)
        document.append(LLMMessage(role="user", content=entry.render("continuation", ctx)),
                        merge_with_last=True)
        return document.messages

    @staticmethod
    def _current_task_user_index(history_pairs, task_id) -> int | None:
        """定位「当前 task」**首条** user_prompt 的下标：task_id 精确匹配取首条，回退最后一条 user_prompt。

        history_pairs 是 _history_to_messages_with_sources 的 (msg, src, mem_type, task_id) 四元组。
        - 匹配取首条：同一 task 会累积多条 user_prompt（observer 判 retry → park → 人回复
          → 冷 resume 同一个 task，每条新消息一条），
          ## Current Task 框 / directive / capabilities 须钉在**开启该 task 的首条消息**上。
          取末条会让这些重量级注入跟着每条新消息漂移——上一轮被装饰的回合在下一轮重建时恢复
          原文，cache 前缀每轮被打穿。（## Current Message 框不在此列：它语义上就是最新一条，
          由 _latest_task_user_index 定位、随新消息走，代价只是尾部一小段 cache。）
        - parent resume 后召回里存在其它 task 的 user_prompt（更新的同 agent 子 body 等）——task_id
          过滤保证不误顶 parent 头。
        - task_id 匹配不到（fresh task / 旧数据无 task_id）时回退末条 user_prompt。
        """
        match = last = None
        for i, (m, _src, mtype, tid) in enumerate(history_pairs):
            if m.role == "user" and mtype == "user_prompt":
                last = i
                if match is None and task_id and tid == task_id:
                    match = i
        return match if match is not None else last

    def _frame_current_message(self, messages, history_pairs, task, blocks=None) -> None:
        """Compatibility wrapper using the same Jinja decorations as prepare()."""
        from ctx_weft.core.assembler.prompt_document import PromptDocument
        from ctx_weft.core.assembler.prompts import load_prompt_template

        anchor = self._current_task_user_index(history_pairs, getattr(task, "id", ""))
        if anchor is None:
            return
        latest = self._latest_task_user_index(history_pairs, getattr(task, "id", ""))
        latest = anchor if latest is None else latest
        title, description, _, task_ref = self._task_spec_fields(blocks, task)
        document = PromptDocument(messages)
        bindings = {"context": (document.refs[anchor],), "input": (document.refs[latest],)}
        if anchor != latest:
            bindings["opening"] = (document.refs[anchor],)
        document.decorate(load_prompt_template("act"), bindings=bindings,
                          context=dict(title=title, description=description, task_ref=task_ref, headings=HEADINGS))
        messages[:] = document.messages

    @staticmethod
    def _latest_task_user_index(history_pairs, task_id) -> int | None:
        """「当前 task」**最新一条** user_prompt 的下标（## Current Message 框的落点）。

        task_id 精确匹配取末条（追问里最新那条才是「当前消息」；同 agent 更晚的
        其它 task user_prompt——如子 body——被过滤掉），匹配不到回退整个列表的末条 user_prompt
        （与 _current_task_user_index 的回退一致，此时两者同指一条、合并框）。
        """
        match = last = None
        for i, (m, _src, mtype, tid) in enumerate(history_pairs):
            if m.role == "user" and mtype == "user_prompt":
                last = i
                if task_id and tid == task_id:
                    match = i
        return match if match is not None else last

    def _build_resources_section(
        self, blocks: list[ContextBlock], *, current_skill_name: str = "",
    ) -> str:
        """从 capabilities blocks 按 kind 分组渲染（miniAgents 风格）。

        三类（skills / tools / sub-agents）统一按 provider 分块（分级标题 + provider
        描述 + 带具体前缀的引用提示），见 layouts/resources.jinja。
        """
        cap_blocks = [b for b in blocks if b.kind == "capabilities"]
        skills = [b for b in cap_blocks if b.metadata.get("capability_kind") == "skill"]
        tools = [b for b in cap_blocks if b.metadata.get("capability_kind") == "tool"]
        agents = [b for b in cap_blocks if b.metadata.get("capability_kind") == "agent"]

        # 当前 task 已绑定的 skill：正文整段进了 ## Instructions for the current task，
        # 列表里再列一遍是重复，故从清单中剔除；其余 skill 保留——它们是派发给子任务的候选，
        # 绑了一个 skill 不该让模型看不见别的。判据是 skill 名（task.settings.skill_name，
        # 与 capability_name = qualify(cap.id) 同一口径），不是「有没有 directive 块」。
        if current_skill_name:
            skills = [b for b in skills if not _is_named_skill(b, current_skill_name)]

        def item(block):
            return dict(name=block.metadata.get("capability_name", "?"),
                        signature=_compact_signature(block.metadata.get("input_schema")),
                        description=content_to_text(block.content))

        sections = []
        for kind, candidates in (("skill", skills), ("tool", tools), ("agent", agents)):
            if not candidates:
                continue
            ungrouped, groups = [], {}
            for block in candidates:
                name = block.metadata.get("provider_name") or ""
                if not name:
                    ungrouped.append(item(block))
                    continue
                if name not in groups:
                    groups[name] = dict(
                        name=name, prefix=qualify(name) + "__",
                        description=(block.metadata.get("provider_description") or "").strip(),
                        entries=[],
                    )
                groups[name]["entries"].append(item(block))
            sections.append(dict(kind=kind, ungrouped=ungrouped, groups=list(groups.values())))
        return render_template_file("layouts/resources.jinja", sections=sections, headings=HEADINGS)

    def _build_capabilities_section(
        self, blocks: list[ContextBlock], *, current_skill_name: str = "",
    ) -> str:
        """Capabilities（skills/tools/agents）段，带 ## Capabilities 引导标题。空时返回 ""。"""
        resources_section = self._build_resources_section(
            blocks, current_skill_name=current_skill_name)
        return render_template_file("layouts/capabilities.jinja",
                                    heading=HEADINGS["capabilities"], resources=resources_section)

    def _build_directive_section(self, blocks: list[ContextBlock]) -> str:
        """当前 task 的 skill 指令段。空时返回 ""。

        skill 名并入标题（## Instructions for the current task (skill: <name>)）；
        skill 正文里的标题层级整体下移到该 H2 之下（_shift_markdown_headings）。
        """
        directive = self._first_kind(blocks, "directive")
        if not directive:
            return ""
        skill_name = directive.metadata.get("skill_name", "")
        body = _shift_markdown_headings(content_to_text(directive.content), base_level=2)
        return render_template_file("layouts/directive.jinja",
                                    heading=HEADINGS["skill_instructions"], skill_name=skill_name, body=body)

    def _history_to_messages(self, history_blocks: list[ContextBlock]) -> list[LLMMessage]:
        """将 history blocks 转换为真实多轮 LLMMessage 列表。

        跨层（task_conversation + agent_experience）按 timestamp 正序归并，seq_no 作 tiebreak。
        """
        return [m for m, _src, _mtype, _tid in self._history_to_messages_with_sources(history_blocks)]

    def _history_to_messages_with_sources(
        self, history_blocks: list[ContextBlock]
    ) -> list[tuple[LLMMessage, str, str, str]]:
        """同 _history_to_messages，但每条 message 附带来源 block.source、memory event type、task_id。

        返回 (LLMMessage, source, mem_type, task_id) 四元组。
        - source: 来源标识（"agent_recall" / "task_conversation" / "agent_experience" 等）
        - mem_type: MemoryEventType str（如 "user_prompt" / "agent_conversation_turn"）；
          无 type 的合成 block（如 capabilities / progress）传空字符串。
        - task_id: 记录所属 task（USER_PROMPT 记录带；其余可空）。用于把 ## Current Message 框架
          精确定位到「当前 task（request.task.id）」自己的 user_prompt 回合——而非召回历史里最后
          一条 user_prompt（parent resume 后，同 agent 子 body 的 user_prompt 更新，会误顶 parent 头）。
        用于区分「当前 task 自有对话」（USER_PROMPT）与跨 task 的 agent_experience 回合——
        directive 注入和 ## Current Message 框架需定位到当前 task 的 user_prompt 回合。
        """
        sorted_blocks = sorted(
            history_blocks,
            key=lambda b: (b.metadata.get("timestamp", ""), b.metadata.get("seq_no", 0)),
        )
        out: list[tuple[LLMMessage, str, str, str]] = []
        for b in sorted_blocks:
            role = b.metadata.get("role", "user")
            content = b.content
            tool_calls = b.metadata.get("tool_calls") or [] if role == "assistant" else []
            # 判空必须 parts-aware：纯图片消息的 content_to_text 是空串，
            # 旧写法会把它当空消息静默丢弃（spec §6.4）。
            if _is_blank_content(content) and not tool_calls:
                continue  # 真空且无 tool_call 才跳过（保留仅含 tool_call 的 assistant 回合）
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
            out.append((msg, b.source, mem_type, str(b.metadata.get("task_id", ""))))
        return out

    # ── Observer ──────────────────────────────────────────────────────────────

    def _build_observe_messages(self, blocks, request, definition=None):
        """Compatibility wrapper around the actual root template, not a second renderer."""
        if definition is None:
            definition = (compile_prompt_policy(ReviewIntent())
                          if getattr(request, "intent", None) is None and not hasattr(request, "purpose")
                          else resolve_prompt_policy(request))
        document, entry, context = self.prepare(blocks, request, definition)
        document.append(LLMMessage(role="user", content=entry.instruction(context)), merge_with_last=True)
        return document.messages

    @staticmethod
    def _observe_subtasks_text(subtasks: list) -> str:
        """子任务**指名清单**——只作信息，不重跑任何东西。

        observer 从对话里读每个子任务的 RESULT（Phase 2）；这里只把句柄
        （task_id/title/outcome）摆出来，好让它在产出不合格时于 `next_step_hint` 里
        **指名**是哪一个。`task_reviews`（以及随它的 reopen）已于 2026-09-19 删除——
        对一个坏结果做什么，是下一个 actor 回合自己的事。
        """
        return render_template_file("layouts/subtasks.jinja", subtasks=[
            dict(ref=task_ref_parts(row.get("task_id", "") or "", row.get("title", "") or ""),
                 outcome=row.get("outcome", ""), note=row.get("note"))
            for row in subtasks
        ])

    # ── 共用工具 ──────────────────────────────────────────────────────────────

    def _first_kind(
        self,
        blocks: list[ContextBlock],
        kind: str,
    ) -> ContextBlock | None:
        for b in blocks:
            if b.kind == kind:
                return b
        return None

    def _task_spec_fields(self, blocks, task) -> tuple[str, str, str, str]:
        """当前 task 的 spec 字段 (title, description, user_prompt, ref)。

        优先取 TaskSpecSource 产的 task_spec block 的 metadata；无块时回退直读 task
        （兼容手构 blocks / 未注册 TaskSpecSource 的调用）。block 是元数据载体，
        composer 用它去就地装饰当前消息，而非把它当独立消息渲染。

        ``ref`` 是 `task_ref` 的规范称呼（标题 + id），供 `## Current Task` 框使用：
        id 一直就在 block 的 metadata 里（`task_spec` source 始终在写 ``task_id``），
        此前只是没被取出来渲染，于是任务连自己的 id 都不知道。
        """
        blk = self._first_kind(blocks, "task_spec") if blocks else None
        if blk is not None:
            md = blk.metadata
            title = md.get("title", "") or ""
            return (
                title,
                md.get("description", "") or "",
                md.get("user_prompt", "") or "",
                task_ref_parts(md.get("task_id", "") or "", title),
            )
        title = task.title or ""
        description = task.description or ""
        up = task.user_prompt
        user_prompt = up if isinstance(up, str) else (content_to_text(up) if up else "")
        return title, description, user_prompt, task_ref_parts(
            getattr(task, "id", "") or "", title)
