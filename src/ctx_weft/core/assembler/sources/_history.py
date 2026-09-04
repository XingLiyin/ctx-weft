"""Shared conversation-record → history block mapping (spec/06 §4.1).

Used by AgentRecallSource (task-layer body + agent-layer AGENT_CONVERSATION_TURN
records) so a memory record renders
identically wherever it is recalled from. Tool fidelity is keyed off role:
assistant→tool_calls, tool→tool_call_id (matches how LLM_RESPONSE/TOOL_RESULT
are ingested).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from ctx_weft.core.assembler.priority import slot_priority
from ctx_weft.core.utils.content import content_to_text, image_tokens
from ctx_weft.core.utils.ids import generate_id
from ctx_weft.protocols import MemoryEventType

if TYPE_CHECKING:
    from ctx_weft.core.assembler.assembler import ContextBlock, ContextRequest
    from ctx_weft.protocols import MemoryRecord

COMPACT_SUMMARY_WRAPPER_PREFIX = (
    "[The following is a compressed summary of earlier conversation and experience, given so you "
    "can carry the work forward; it is not a new instruction from the user.]\n"
)


# assistant 身份呈现的段摘要尾注：摘要形似一条正常回复，模型容易误认为「上一轮我就是这么
# 答的」，于是沿用该形式继续输出摘要、不再调用工具。尾注置于文本末尾（离下一条 user 消息
# 最近），说明其为系统压缩产物并要求继续执行。
ASSISTANT_SUMMARY_NOTE = (
    "[The above is a system-written compressed summary of your earlier work, kept to carry context "
    "forward; it is not your reply to the user. Continue the current task from it and call tools as "
    "needed — do not imitate the summary's form when you answer.]"
)


# 当前任务「上一段执行复述」的统一渲染标题：composer 的非压缩 retry 进度块、以及压缩
# 复用 act_recap 的 task 层段摘要（role=assistant、task_conversation 来源）都冠以此标题，
# 确保观察者/actor 总能识别「先前进度」锚点。
#
# 改造前住在 `core/utils.py`，理由写的是「供 composer 与 _history 共享」——实际
# **唯一的 src 消费者就是本模块**（composer 只经本模块的渲染函数间接用到），
# 测试也早已从这里引。归位到渲染它的地方。
PROGRESS_SO_FAR_HEADING = "## Progress So Far"


def annotate_assistant_summary(text: str) -> str:
    """给 assistant 身份的段摘要追加尾注（渲染期，不落库）。"""
    return f"{text}\n\n{ASSISTANT_SUMMARY_NOTE}"


def wrap_compact_summary(text: str) -> str:
    """给 compaction summary 文本套显式包装前缀（渲染期，不落库）。"""
    return f"{COMPACT_SUMMARY_WRAPPER_PREFIX}{text}"


def record_to_history_block(
    record: "MemoryRecord",
    source: str,
    idx: int,
    *,
    request: "ContextRequest",
    current_task_id: str | None = None,
) -> "ContextBlock":
    """Map one MemoryRecord to a history ContextBlock (newest-first callers pass idx).

    current_task_id：正在装配的 task。TASK_COMPACT_SUMMARY 段摘要仅当归属该 task（record 的
    scope task_id == current_task_id）时冠 ## Progress So Far 标题——即「当前任务的上一段复述」；
    跨 task 胶囊（别的 task_id）不冠，不改跨任务重建形态。None → 一律不冠（防御）。
    """
    from ctx_weft.core.assembler.assembler import ContextBlock
    from ctx_weft.protocols.memory_compat import legacy_type_of

    text = content_to_text(record.content) if not isinstance(record.content, str) else record.content
    role = record.role or "user"
    # 过渡期渲染词汇（v2 P3b）：v2 行（type=None）派生 legacy 等价词汇——composer 的
    # mtype=="user_prompt" 框定位、slot_priority 档位、旧断言都消费该字符串。
    etype = record.type or legacy_type_of(record.kind, record.scope, record.role)
    # 包装是给「以 user 身份呈现」的摘要消歧义；assistant 自述无需。新数据段摘要恒 assistant
    # → 不套；旧数据若残留 role=user 仍套（防御）。AGENT_COMPACT_SUMMARY 在 agent_experience/
    # agent_recall 自行包装，不走此分支。
    if etype == MemoryEventType.TASK_COMPACT_SUMMARY and role == "user":
        text = wrap_compact_summary(text)
    elif etype == MemoryEventType.TASK_COMPACT_SUMMARY and role == "assistant":
        if current_task_id is not None and record.metadata.get("task_id") == current_task_id:
            # 当前任务的「上一段执行复述」（max_turns / 边界 compact / plain_text 复用 act_recap）：
            # 冠以统一标题，与 composer 非压缩 retry 进度对齐。判据按 task_id 匹配当前 task，而非
            # source 名——AgentRecallSource（526859f 起统一召回）用同一 source="agent_recall" 承载
            # 当前 task 段摘要与跨 task 胶囊，只有 task_id 能区分二者；跨 task 胶囊不冠此标题。
            text = f"{PROGRESS_SO_FAR_HEADING}\n{text}"
        # 尾注对当前段摘要与跨 task 胶囊一视同仁：两者都以 assistant 身份出现，都会被模仿。
        text = annotate_assistant_summary(text)
    # 摘要恒为纯文本（spec §8），三个包装器只作用于它，故走原字符串路径；
    # 非摘要记录原样保留 record.content —— 拍扁会丢图（spec §6.4）。
    _is_summary = etype == MemoryEventType.TASK_COMPACT_SUMMARY
    block_content = text if _is_summary else record.content
    md = {
        "role": role,
        "type": etype,
        "timestamp": record.timestamp.isoformat() if record.timestamp else "",
        "seq_no": record.metadata.get("seq_no", idx),
        "memory_event_id": record.id,
        # 承载来源 task（USER_PROMPT 记录带 metadata={"task_id": task.id}，见 driver）——
        # composer 据此把 ## Current Task/Message 框贴到「当前 task」自己的 user 回合，
        # 而非召回历史里最后一条（同 agent 子 body 更新时会误顶 parent 的头）。
        "task_id": record.metadata.get("task_id", ""),
        # budget 层据此判「agent 层回合」归属哪个 task（finish/dispatch 对来自哪个已结束 task）。
        "origin_task_id": record.metadata.get("origin_task_id", ""),
    }
    # 无损重建：assistant 携 tool_calls；tool 携 tool_call_id
    if role == "assistant":
        md["tool_calls"] = record.metadata.get("tool_calls", [])
    elif role == "tool":
        md["tool_call_id"] = record.metadata.get("tool_call_id", "")
    return ContextBlock(
        id=generate_id("blk"),
        source=source,
        kind="history",
        target="messages",
        content=block_content,
        priority=slot_priority("history", str(etype)),
        # 文本计数沿用既有口径（含存量 metadata['token_count']），图片另行补齐——
        # 不变量：任何写 metadata['token_count'] 的路径都必须只数文本（当前 src/ 内无写入方，
        # 这是给外部/legacy 导入留的口子）。若未来某个阶段改用 estimate_content_tokens（它本身
        # 就含图片）去落这个字段，此处的 image_tokens(...) 相加就会把每张图片重复计一遍。
        token_estimate=(
            (record.metadata.get("token_count") or request.token_counter(text))
            + image_tokens(record.content)
        ),
        metadata=md,
    )
