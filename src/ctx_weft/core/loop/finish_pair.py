"""finish 对：它怎么造、怎么被真摘要替换，以及两个交接槽。

**为什么单独一个模块**（2026-09-29）：这套东西有两个对手方——`FinalizeStep` 在 close 时
造出 finish 对，后台 recap 事后把占位换成真摘要——而它此前劈成两半，`build_finish_slots`
住在 finalize、两个槽与 `replace_finish_report` 住在后台 recap 那边。于是两个模块**互相
import**，两边都只能写成函数内局部 import 才不炸。收到一处之后依赖变成单向：finalize 与
background 都只依赖本模块。

## 两槽协议

close 与真摘要谁先到都不能错，所以是一对槽而不是一次调用：

- **`_close_report`**（后台先到）：recap 把 `(act_recap, task_summary)` 放这儿，
  finalize close 时 `pop_close_report` 取走，直接造出带真摘要的 finish 对。
- **`_close_synth`**（finalize 先到）：close 先造**占位** finish 对并登记
  `(tool_call_id, scope, outcome, raw_fold_scope)`，后台回来 `pop_close_synth` 拿到句柄，
  用 `replace_finish_report` 整条重写。

`raw_fold_scope` 非 None 表示**末段 raw 还没折**（spec 2026-07-20 延迟折叠）：占位期间
raw 仍是这一段唯一的账，等真报告落地才补删——不变量是「末段 raw 与真实 Process Report
至少存其一」，同一段绝不记两遍。
"""

from __future__ import annotations

import logging
from datetime import timedelta

from ctx_weft.core.media import placeholder_refs
from ctx_weft.core.utils.task_ref import task_ref_parts
from ctx_weft.protocols.capability import qualify
from ctx_weft.protocols import (
    MemoryEvent,
    MemoryKind,
    MemoryScope,
)

logger = logging.getLogger(__name__)


# ── 交接槽 ────────────────────────────────────────────────────────────────────

# close 路径结果槽：task_id → (act_recap, task_summary)（finalize Task 8 通过 pop_close_report 取用）
_close_report: dict[str, tuple[str, str]] = {}

# close 路径合成槽：task_id → (tool_call_id, scope, outcome, raw_fold_scope)
# finalize 先到时登记，bg 回调后替换 finish tool 记录；raw_fold_scope 非 None 时
# 替换成功后按它补删末段 raw（spec 2026-07-20 延迟折叠——占位 close 不即折）。
_close_synth: dict[str, tuple] = {}


# ── finish 对的构造 ───────────────────────────────────────────────────────────

def _finish_report_prefix(ref: str, outcome: str) -> str:
    """finish 对 tool 槽的前缀：`[task: '<title>' (<id>)] ` 标明归属（+ fail 标记）。

    归属标记的必要性：finish 对的 assistant 槽是**无参**收尾标记（`finish_task{}`，反转契约），
    自身不带任何归属信息。派发侧不存在这个问题——框带 `input:{title, description}` 自描述；
    收尾侧此前没有对称物，于是同 agent 嵌套（孙→子相继 close）时，父重建出的对话里会出现两组
    完全同形的 `[assistant finish_task{}][tool …]`，只能靠正文猜是哪个 task 收的尾。

    标记落 tool 槽而非 `input`：`input` 受 finish_task 的 schema 约束，而
    ControlCapabilityProvider._handle 会过滤 schema 未声明的 key（见 control_capability.py），
    塞进去只会被静默丢弃；且历史里出现未声明参数会诱导模型照此形状调用。tool 槽是自由文本，
    与既有 `[outcome=fail]` 前缀同一惯例。归属在前、outcome 标记在后；title 为空则不产空标记。

    `outcome == "cancelled"`（统一取消胶囊闭合，见 synthesize_cancel_closure）→ `[outcome=cancelled]`，
    与既有 `[outcome=fail]` 同一惯例，标明这条 finish 对是取消收尾而非正常/失败终态。
    """
    parts: list[str] = []
    ref = (ref or "").strip()
    if ref:
        # 带 id 才真的消歧：本前缀存在的理由就是「同 agent 嵌套时两组同形的 finish 对
        # 只能靠正文猜归属」，而同名兄弟任务恰好让只印标题的版本原地失效。
        parts.append(f"[task: {ref}]")
    if outcome == "fail":
        parts.append("[outcome=fail]")
    elif outcome == "cancelled":
        parts.append("[outcome=cancelled]")
    return "".join(f"{p} " for p in parts)


# 最终回复锚点（close 折末段 raw 时补位）：反转契约下答复正文是收尾回合的普通消息，
# 随末段 raw 一起被删；锚点把它留在 task 层胶囊里。提示词用 assistant 第一人称，与
# 「以上为系统压缩摘要」的尾注互不交叉（各自只描述自己那条消息的正文）。
FINAL_REPLY_NOTE = (
    "[Final reply for task {ref} — the answer I delivered on finishing it, "
    "verbatim; not a summary.]"
)
FINAL_REPLY_NOTE_UNTITLED = (
    "[Final reply — the answer I delivered on finishing this task, verbatim; not a summary.]"
)
# 收束尾注：锚点是**真回复**，比段摘要更容易被读成「我刚刚就是这么答的」——紧随其后的往往是
# 新的用户消息或另一个 task，没有收束就会照抄/续写它。作用与段摘要的 ASSISTANT_SUMMARY_NOTE
# 对称，措辞同为方括号系统注解。
FINAL_REPLY_CLOSING_NOTE = (
    "[End of that final reply. It was delivered to the user when the task closed — do not repeat "
    "it, do not imitate its form, and do not treat it as the answer to whatever is being asked "
    "now.]"
)


def _finish_tool_text(task_summary: str, act_recap: str, outcome: str) -> str:
    """finish 对 tool 槽内容 = task_summary（process report）。R2 兜底：空则退 act_recap，
    再空给占位。**不掺 outputs**——最终输出由 task 层的最终回复锚点承载
    （见 _supersede_final_raw_segment），tool 槽只讲过程、不重复它。
    绝不返回空串（避免空 tool 回合 / 400）。"""
    for cand in (task_summary, act_recap):
        if cand and cand.strip():
            return cand
    return ("(no final output)" if outcome == "fail"
            else "(nothing further to report for this segment)")


# recap 槽的收束尾注：它是 agent 层普通 assistant 回合，拿不到段摘要那条尾注
# （_history.annotate_assistant_summary 只贴 TASK_COMPACT_SUMMARY），而形态上同样像
# 「我上一轮就是这么答的」，同样会被模仿。与 FINAL_REPLY_CLOSING_NOTE 一并夹住胶囊：
# 前者说「这段是过程」，后者说「那段答复已经交付过了」。
PROCESS_RECAP_NOTE = (
    "[The above is a system-written recap of how this task was carried out, kept for context — "
    "it is not your reply to the user. Do not imitate its form when you answer.]"
)

_RECAP_PLACEHOLDER = "(no process recap for this segment)"


def _recap_block(act_recap: str) -> str:
    """recap 槽正文 = 过程复述 + 收束尾注（正文为空时用占位，注解照贴）。"""
    body = (act_recap or "").strip() or _RECAP_PLACEHOLDER
    return f"{body}\n\n{PROCESS_RECAP_NOTE}"


def _final_reply_block(ref: str, reply: str) -> str:
    """锚点正文 = 前置提示 + 答复原文 + 收束尾注。"""
    note = (FINAL_REPLY_NOTE.format(ref=ref.strip()) if (ref or "").strip()
            else FINAL_REPLY_NOTE_UNTITLED)
    return f"{note}\n\n{reply.strip()}\n\n{FINAL_REPLY_CLOSING_NOTE}"


def build_finish_slots(*, scope, task_id: str, parent_task_id, title: str, outcome: str,
                       act_recap: str, task_summary: str, final_reply: str,
                       base, tool_call_id: str) -> list[MemoryEvent]:
    """finish 对的槽位事件。close 合成与 bg 事后重写共用，保证两处形态永远一致。

    `final_reply` 非空 → **三槽**：

        assistant  act_recap                      过程复述，不挂 tool_calls
        assistant  提示 + 答复 + 收束尾注           锚点，finish_task{} 挂这条（metadata.final_reply）
        tool       process report                 与锚点配对

    finish_task 挂在答复那条、而不是 recap 那条：重建出的历史因此示范了 finish_task 的真实
    用法——答复正文与收尾调用同一轮（见 control_capability.finish_task 的说明）。挂在 recap
    上等于反过来教模型「收尾时正文写过程复述」。

    `final_reply` 空 → **两槽**（旧形态，finish_task 挂 recap）。用于末段 raw 没被折的场景：
    short leaf 原文即胶囊、observer 护栏兜底导致 outputs 为空、以及占位 close（raw 还在，
    等 bg 真报告落地时再由 _replace_finish_report 升三槽）。

    时间戳按槽位递增 1µs：顺序由写入点定死，不依赖后续记录的时刻。
    """
    # 任务的规范称呼（标题 + id）在此合成：本函数本来就同时持有两者，调用方无需改动。
    ref = task_ref_parts(task_id, title)
    md = {"origin_task_id": task_id, "parent_task_id": parent_task_id}
    call = [{"id": tool_call_id, "name": qualify("control:finish_task"), "input": {}}]
    report = f"{_finish_report_prefix(ref, outcome)}"              f"{_finish_tool_text(task_summary, act_recap, outcome)}"
    reply = (final_reply or "").strip()

    def turn(content, ts, role, extra):
        # act_recap / task_summary / final_reply 都可能是折叠产物，逐字带着 L0.5 占位向前走
        # （见 `placeholder_refs` docstring）。这三槽既经 `_replace_finish_report` 的 fold
        # 写入（旧记录被 supersede），也经 `_synthesize_dispatch_pair` 的裸 ingest 写入
        # （无 supersede，但 mark 判据一视同仁、同样只看结构化字段）——两条路都要声明，
        # 故放在两个调用方共用的这一层，而不是分别在各自的调用点补。
        return MemoryEvent(
            kind=MemoryKind.CONVERSATION_TURN, scope=MemoryScope.AGENT, address=scope,
            content=content, timestamp=ts, role=role, metadata={**md, **extra},
            blob_refs=placeholder_refs(content),
        )

    if reply:
        return [
            turn(_recap_block(act_recap), base, "assistant", {}),
            turn(_final_reply_block(ref, reply), base + timedelta(microseconds=1),
                 "assistant", {"tool_calls": call, "final_reply": True}),
            turn(report, base + timedelta(microseconds=2), "tool", {"tool_call_id": tool_call_id}),
        ]
    return [
        turn(_recap_block(act_recap), base, "assistant", {"tool_calls": call}),
        turn(report, base, "tool", {"tool_call_id": tool_call_id}),
    ]


# ── 占位替换 ──────────────────────────────────────────────────────────────────

def put_close_report(task_id: str, act_recap: str, task_summary: str) -> None:
    """后台先到：把真摘要放进槽，等 finalize close 时取。**不写 memory**。"""
    _close_report[task_id] = (act_recap, task_summary)


def pop_close_report(task_id: str) -> tuple[str, str] | None:
    """取走 close 路径产出的 (act_recap, task_summary)；不存在则返回 None。"""
    return _close_report.pop(task_id, None)


def register_close_synth(task_id: str, tool_call_id: str, scope, outcome: str,
                         raw_fold_scope=None) -> None:
    """finalize 先到时登记：finish 对已合成，待 bg 回调替换 Process Report。

    raw_fold_scope 非 None = close 时是占位 finish 对、末段 raw 未删（task scope），
    bg 替换成功后按它补删；bg 失败/无报告 → 不删（降级 = 保 raw）。"""
    _close_synth[task_id] = (tool_call_id, scope, outcome, raw_fold_scope)


def pop_close_synth(task_id: str) -> tuple | None:
    """bg 回调取走合成登记；不存在则返回 None。"""
    return _close_synth.pop(task_id, None)


async def replace_finish_report(memory, provider_ctx, scope, task_id: str,
                                 tool_call_id: str, act_recap: str, task_summary: str,
                                 outcome: str, title: str, *, final_reply: str = "") -> None:
    """supersede 本 task 的 finish 对占位，按新 act_recap / task_summary 整体重写。
    按 (tool_call_id + origin_task_id) 定位，不再靠 'Process Report:' 文本（spec 2026-06-30 §2.4）。

    title：归属 task 的标题，用于重建 tool 槽的 `[task: …]` 前缀（与 finalize 合成占位时同源，
    见 `_finish_report_prefix`）。本函数整条重写 tool 槽，不传就会把占位里的归属标记抹掉。

    final_reply：延迟折叠路径下末段 raw 此刻才被折，答复要随之补进锚点槽——占位是两槽，
    重写后升成三槽（recap / 答复+finish_task / 报告，见 `build_finish_slots`）。空则
    保持原形态；若占位已是三槽（close 即折路径），沿用已有锚点正文，**不得**被 act_recap 冲掉
    ——按 tool_call_id 找 assistant 命中的正是锚点那条。"""
    from datetime import timedelta

    from ctx_weft.protocols import MemoryAddress, MemoryEvent, MemoryKind, MemoryScope

    turns = await memory.load_view(
        MemoryAddress(session_id=scope.session_id, agent_id=scope.agent_id),
        MemoryScope.AGENT, provider_ctx, kinds=[MemoryKind.CONVERSATION_TURN],
    )
    asst = [r for r in turns
            if r.role == "assistant" and r.metadata.get("origin_task_id") == task_id
            and any(tc.get("id") == tool_call_id for tc in (r.metadata.get("tool_calls") or []))]
    tool = [r for r in turns
            if r.role == "tool" and r.metadata.get("origin_task_id") == task_id
            and r.metadata.get("tool_call_id") == tool_call_id]
    # 三槽占位里 recap 是**不挂 tool_calls** 的那条 assistant（挂着的是锚点）。
    recap_slot = [r for r in turns
                  if r.role == "assistant" and r.metadata.get("origin_task_id") == task_id
                  and not r.metadata.get("tool_calls")]
    if not asst and not tool:
        logger.warning("A1 _replace_finish_report: no finish 对 for task=%s tcid=%s; skip (best-effort)",
                       task_id, tool_call_id)
        return

    first = (recap_slot or asst or tool)[0]
    ts = first.timestamp
    parent_task_id = first.metadata.get("parent_task_id")
    # 已是三槽 → 沿用已有锚点正文（它已含前后两条注解，直接透传会被再包一层）。
    existing_anchor = next((r for r in asst if r.metadata.get("final_reply")), None)

    # 槽位形态与 close 合成同源（`build_finish_slots`）：答复在则三槽、不在则两槽。
    # v2 P3d：占位遗忘 + 新槽写入一次原子 fold（关旧「占位已删而真报告未写」窗口）。
    events = build_finish_slots(
        scope=scope, task_id=task_id, parent_task_id=parent_task_id, title=title,
        outcome=outcome, act_recap=act_recap, task_summary=task_summary,
        final_reply=final_reply, base=ts, tool_call_id=tool_call_id,
    )
    if existing_anchor is not None and not (final_reply or "").strip():
        # 占位已是三槽而本次没带答复：锚点正文原样保留，只换 recap 与报告两槽。
        events = [e for e in events if not e.metadata.get("final_reply")]
        events.insert(1, MemoryEvent(
            kind=MemoryKind.CONVERSATION_TURN, scope=MemoryScope.AGENT, address=scope,
            content=existing_anchor.content, timestamp=ts + timedelta(microseconds=1),
            role="assistant",
            metadata={"origin_task_id": task_id, "parent_task_id": parent_task_id,
                      "tool_calls": existing_anchor.metadata.get("tool_calls"),
                      "final_reply": True},
        ))
    await memory.fold([r.id for r in (*recap_slot, *asst, *tool)], events, provider_ctx)
