"""FinalizeStep：task 收尾——写 memory + blackboard publish + 更新 task 状态。

miniAgents 对齐版：
- memory 内容 = task.outputs + "\\n\\nProcess Report: " + verdict.act_recap（合并写入）
- 新增 BLACKBOARD_PUBLISH：让父 agent 通过 recall_topic(task.id) 读到子任务结果
"""

from __future__ import annotations

import logging
from typing import Any

from ctx_weft.core.loop.driver import LoopContext, LoopState, Step, StepOutcome, make_event
from ctx_weft.core.events import EventType
from ctx_weft.core.utils import as_utc, content_to_text, generate_id, now_utc
from ctx_weft.protocols import MemoryEvent, MemoryEventType, MemoryKind, MemoryLayer, MemoryScope
from ctx_weft.protocols.capability import qualify

logger = logging.getLogger(__name__)

# v2 P3：旧类型清单常量（_OWN_CONV_TYPES/_FINAL_RAW_TYPES）随读侧 kind+role 谓词化删除。

def _dispatch_running_ack(title: str) -> str:
    """派发对 tool 槽的 **running 态**：子任务真正 start 时写，close 时被 `_dispatch_ack` 终态替换。

    与终态同理不含任何子任务产出——此刻也还没有。只说明「在跑」+ 导读下方的内联执行。
    """
    return f"Sub-task '{title}' is running now — its execution follows below."


# 同 agent 派发：派发对 tool 结果（不含任何子任务结果，spec 2026-06-30 §2.5）。
def _dispatch_ack(title: str, outcome: str) -> str:
    """同 agent 派发对的 tool 槽：终态 + 内联导读，**绝不含子任务产出**。

    「不回填真实结果」是结构性的，不是保守：llm_gateway.reorder_tool_results_after_calls 会把
    每条 tool result 强行挪到其 assistant tool_call 之后（OpenAI 兼容端点要求二者相邻，否则
    400），所以本条**必定**渲染在子 body 之前——无论它的 timestamp 是什么。塞进真实产出就成了
    「先结论、后过程」的倒叙，且与内联 body + 嵌套 finish 对重复。

    但本条是在 close 时刻才写的，**已知 outcome**，故可以带两样位置正确的前向信息：
      ① 终态——让 delegate_task 这个调用真正解析出结果，而不是永远停在「started」；
      ② 导读——告诉读者子任务的完整执行内联在下方、以自己的 finish_task 收尾。导读同时解释了
         紧随其后的 user 回合（子任务的 task_prompt，非用户发言），否则易被读成用户插话。
    刻意保持指针级长度：每个派发都会复读一份。
    """
    verdict = "FAILED" if outcome == "fail" else "completed"
    return (
        f"Sub-task '{title}' ran here — outcome: {verdict}. Its execution is inlined "
        f"below, ending with its own {qualify('control:finish_task')}."
    )

# 派发框的叙事工具名（仅出现在重建历史的 tool_calls 里，非可调用能力）。
START_TASK_NAME = qualify("control:start_task")


# 归一可能 naive 的 datetime 为 aware(UTC)——事件重放 / DB 反序列化可能丢 tz，
# 比较前统一，避免 naive 与 aware 直接比较报 TypeError。统一实现见 utils.as_utc。
_as_utc = as_utc


async def _find_dispatch_frame(memory, parent_scope, task, provider_ctx):
    """在 parent scope 只查已存在的派发框（tool_call_id==task.origin_tool_call_id 的 assistant
    回合），不铸新框。找到 → 返回框自己的时间戳（归一 aware UTC）；没有 → None。

    供 `_ensure_dispatch_frame`（find+create）与 `synthesize_cancel_closure`（find-only，
    born-cancel 未铸框时整体跳过、不补铸）共用。
    """
    from ctx_weft.protocols import MemoryAddress, MemoryKind, MemoryLayer
    existing = await memory.load_view(
        MemoryAddress(session_id=parent_scope.session_id, agent_id=parent_scope.agent_id),
        MemoryLayer.AGENT, provider_ctx, kinds=[MemoryKind.CONVERSATION_TURN])
    frame = next(
        (r for r in existing
         if r.role == "assistant"
         and any(tc.get("id") == task.origin_tool_call_id
                 for tc in (r.metadata.get("tool_calls") or []))),
        None,
    )
    if frame is None:
        return None
    # 归一：框可能来自事件重放 / DB 反序列化而丢 tz（naive），直接返回会让调用方拿它与
    # aware 时间比较时炸 TypeError。
    return _as_utc(frame.timestamp)


async def _ensure_dispatch_frame(memory, parent_scope, task, provider_ctx):
    """在 parent scope 铸一条 tool_call id==task.origin_tool_call_id 的 assistant 派发框，
    返回它 == 配对 tool result 应锚定的时间戳 = task 真正开始执行的时刻（started_at）。

    **框与 result 同锚 started_at**：二者共用同一时间戳 → 按 (timestamp, seq_no) 排序时严格相邻
    （框先写 seq 小、result 后写 seq 大），且落在「任务开始执行」这条时间线上（而非派发时刻）。
    started_at 晚于 actor 派发那一轮、早于子 body（body 由 driver 在启动后才 ingest USER_PROMPT），
    故「派发框 → Task started/result → 子 body → finish 对」顺序天然成立、胶囊连续。回退 created_at
    （历史/无 started_at）再回退 now，统一归一为 aware(UTC)。

    delegate_task 与 delegate_plan 子统一走此铸框路径（gateway 不再为 delegate_task eager 写框——
    eager 框只能带派发时刻，无法落在 started_at 时间线上，见 capability_gateway._record_invocation）。
    框的 tool name 取 task.origin_tool_name（保真）：delegate_task 子 = 真名 control__delegate_task
    （actor 确实调过）；delegate_plan 子 = None → 回退 START_TASK_NAME 叙事名（无 per-child 真实调用）。
    幂等：若同 id 的框已存在（终态 finalize 单入本不会重入，此为防御），直接返回 ts、不重复铸。
    origin_task_id 留父（delegate_task 的父 = 派发 task；delegate_plan 子 = 留 plan task）。

    find + create：find 部分委托 `_find_dispatch_frame`（与 cancel closure 的 find-only 用法共用）。
    """
    found = await _find_dispatch_frame(memory, parent_scope, task, provider_ctx)
    if found is not None:
        # 幂等：框已铸（常态——ensure_dispatch_frame_at_start 在子 start 时就铸好了）。
        # **返回框自己的 ts，而不是按当下 started_at 重算的 ts**：TaskManager 每次派发都刷新
        # task.started_at，故 retry 过的子任务在 close 时算出的是**末次**派发时刻，而框停在首次。
        # 用重算值会把终态 ack 写到框之外（漂进子 body 中间），断掉「框与 result 同锚、严格相邻」
        # 的不变量——渲染虽有 reorder_tool_results_after_calls 兜底，但那是把正确性转嫁给 legalize。
        return found
    # 框与 result 的公共锚点：task 真正启动执行的时刻。归一为 aware(UTC)：started_at/created_at
    # 可能来自事件重放而为 naive（历史无 started_at 时回退 created_at 再回退 now）。
    ts = _as_utc(task.started_at or task.created_at or now_utc())
    await memory.ingest(
        MemoryEvent(
            kind=MemoryKind.CONVERSATION_TURN, layer=MemoryLayer.AGENT, scope=parent_scope,
            content="", timestamp=ts, role="assistant",
            metadata={"origin_task_id": task.parent_task_id,
                      "parent_task_id": task.parent_task_id,
                      "tool_calls": [{"id": task.origin_tool_call_id,
                                      "name": task.origin_tool_name or START_TASK_NAME,
                                      "input": {"title": task.title,
                                                "description": task.description or ""}}]},
        ),
        provider_ctx,
    )
    return ts


def _parent_scope_of(state, task) -> MemoryScope:
    """派发方（parent task + creator agent）的 scope——派发框/result 的落点。"""
    return MemoryScope(
        session_id=state.scope.session_id,
        task_id=task.parent_task_id,
        agent_id=task.creator_agent_id,
    )


async def _put_dispatch_result(memory, parent_scope, task, content: str, ts, provider_ctx,
                               *, replace: bool) -> None:
    """写派发对的 tool 槽（按 origin_tool_call_id 与框配对）。

    replace=False（start）：已有配对 result 时 no-op —— retry/resume 会重跑 driver.run，
      不能每轮都 churn 一遍、更不能改写原锚点。
    replace=True（close）：先 supersede 旧的 running ack 再写终态。**必须替换而非新增**：
      同一 tool_call_id 若有两条 active result，reorder_tool_results_after_calls 会把两条
      都排到框之后，于是「在跑」和「已完成」并列出现。
    """
    from ctx_weft.protocols import MemoryAddress, MemoryKind, MemoryLayer
    recs = await memory.load_view(
        MemoryAddress(session_id=parent_scope.session_id, agent_id=parent_scope.agent_id),
        MemoryLayer.AGENT, provider_ctx, kinds=[MemoryKind.CONVERSATION_TURN])
    stale = [r.id for r in recs
             if r.role == "tool" and r.metadata.get("tool_call_id") == task.origin_tool_call_id]
    if stale and not replace:
        return
    # v2 P3d：旧 ack 遗忘 + 终态写入一次原子 fold（stale 空 = 纯写入）
    await memory.fold(stale, [
        MemoryEvent(
            kind=MemoryKind.CONVERSATION_TURN, layer=MemoryLayer.AGENT, scope=parent_scope,
            content=content, timestamp=ts, role="tool",
            metadata={"origin_task_id": task.parent_task_id,
                      "tool_call_id": task.origin_tool_call_id},
        ),
    ], provider_ctx)


async def ensure_dispatch_frame_at_start(state, ctx) -> None:
    """子任务**真正开始执行**时铸派发框 + running ack（由 driver.run 调用，紧邻 _persist_user_prompt）。

    **为什么在 start、而不是派发时刻**：gateway 的 `_record_invocation` 早于工具执行，那时子任务
    的生死未定——pause 弃子（`TaskManager.abandon_pending`）、`_pause_abandon` 的 staged 静默丢弃、
    `delegate_task` 自身抛异常，都会让子任务胎死腹中，而框已落库、成为永远 pending 的孤儿。其中
    staged 丢弃连 task id 都不返回、也不发 TASK_CANCELED，根本无从清理。改在 start 铸后：
      - 没跑起来的子任务从不写框 → 零清理，不需要任何取消路径配合（弃子后父会 resume，
        `abandon_pending` 不置 session CANCELED，所以「说在跑却什么都没有」的谎会被读到）；
      - `started_at` 此刻已存在 → 框直接生在正确锚点，无需 close 时重定位。

    **为什么不能等到 close**：执行期间唯一的读者是子任务自己（同 agent 时父挂起、不装配）。
    有了框 + running ack，子任务才看得见自己的来历；否则它只看到一条凭空出现的 user 回合
    （自己的 task_prompt），容易读成用户插话。跨 agent 子任务的 agent scope 不同、召回不到本框，
    与其 lean 隔离形态一致。

    幂等：框由 `_ensure_dispatch_frame` 查重，result 由 `_put_dispatch_result(replace=False)` 查重。
    """
    task = state.task
    if not (task.parent_task_id and task.origin_tool_call_id):
        return  # root task：无派发方，没有框可铸
    parent_scope = _parent_scope_of(state, task)
    ts = await _ensure_dispatch_frame(ctx.memory, parent_scope, task, ctx.provider_ctx)
    await _put_dispatch_result(
        ctx.memory, parent_scope, task, _dispatch_running_ack(task.title), ts, ctx.provider_ctx,
        replace=False,
    )


def _finish_report_prefix(title: str, outcome: str) -> str:
    """finish 对 tool 槽的前缀：`[task: <title>] ` 标明归属（+ fail 标记）。

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
    title = (title or "").strip()
    if title:
        parts.append(f"[task: {title}]")
    if outcome == "fail":
        parts.append("[outcome=fail]")
    elif outcome == "cancelled":
        parts.append("[outcome=cancelled]")
    return "".join(f"{p} " for p in parts)


def _finish_tool_text(task_summary: str, act_recap: str, outcome: str) -> str:
    """finish 对 tool 槽内容 = task_summary（process report）。R2 兜底：空则退 act_recap，
    再空给占位。**不掺 outputs**——最终输出在 finish_task 的 result 入参，tool 槽不重复它。
    绝不返回空串（避免空 tool 回合 / 400）。"""
    for cand in (task_summary, act_recap):
        if cand and cand.strip():
            return cand
    return "(无最终产出)" if outcome == "fail" else "(本段无更多总结)"


def _descendant_task_ids(root_id: str, task_manager) -> set[str]:
    """BFS over children_of → 该 task 名下所有后代 task_id（不含自身）。"""
    if task_manager is None:
        return set()
    out: set[str] = set()
    stack = [root_id]
    while stack:
        cur = stack.pop()
        for cid in task_manager.children_of(cur):
            if cid not in out:
                out.add(cid)
                stack.append(cid)
    return out


async def _is_short_leaf(memory, scope, task, loop_config, ctx, has_descendants: bool) -> bool:
    """叶子(无后代) 且 对话 token ≤ threshold 且 LLM_RESPONSE 轮次 ≤ turn_cap → short。"""
    if has_descendants:
        return False  # 非叶（委派过子任务）永不 short
    from ctx_weft.protocols import MemoryKind, MemoryLayer
    # v2 P3a：全 task 层视图（对话+摘要+audit = 旧 _OWN_CONV_TYPES 五类型）一次取回，
    # assistant 轮次与 token 估算共用。
    records = await memory.load_view(
        scope, MemoryLayer.TASK, ctx.provider_ctx,
        kinds=[MemoryKind.CONVERSATION_TURN, MemoryKind.SUMMARY, MemoryKind.TOOL_AUDIT])
    n_assistant = sum(
        1 for r in records
        if r.kind is MemoryKind.CONVERSATION_TURN and r.role == "assistant"
    )
    if n_assistant > loop_config.short_task_turn_cap:
        return False
    text = " ".join(
        content_to_text(r.content) if not isinstance(r.content, str) else r.content
        for r in records
    )
    return ctx.llm.tokenizer.count(text) <= loop_config.short_task_token_threshold


async def finalize_task_memory(memory, state, task, mem_content: str, outcome: str, ctx,
                               *, act_recap: str, task_summary: str,
                               has_llm_summary: bool = True) -> list:
    """finish 时调用：算 short / descendants 后委派 _close_one。返回事件列表。

    task-resident（spec 2026-06-28）：short 不再 gate 合成/supersede——每个结束 task 都写
    finish 对、body 留 task 层。`short` 仅算出后透传给 _close_one（Task 2 用于 body raw-vs-压末段）。

    has_llm_summary（spec 2026-07-20 延迟折叠）：close 时刻 finish 对内容是否已是 LLM 真摘要
    （= verdict.reported）。False = 规则 observe 占位 → 末段 raw 不在 close 时删，推迟到
    bg 替换真摘要落地后补删（默认 True 保守 = 旧行为，close 即折）。
    """
    descendants = _descendant_task_ids(task.id, ctx.task_manager)
    short = await _is_short_leaf(
        memory, state.scope, task, state.agent.loop_config, ctx, bool(descendants),
    )
    return await _close_one(
        memory, state, task, mem_content, outcome, ctx,
        short=short, act_recap=act_recap, task_summary=task_summary,
        has_llm_summary=has_llm_summary,
    )


async def _supersede_final_raw_segment(memory, scope, provider_ctx) -> None:
    """长任务 close：supersede task 层**末段** raw（active LLM_RESPONSE/TOOL_INVOCATION/
    TOOL_RESULT），保留 USER_PROMPT + TASK_COMPACT_SUMMARY 锚点（spec 2026-06-28 §3.2）。

    段作用域（2026-07-21）：末段 = 最后一条 active USER_PROMPT 之后。此前假设「active raw
    即末段」（中间段在各自边界已折），但短段免折（background_observe.is_short_segment）会让
    前段 raw 以 active 状态残留——它们无胶囊代表，删了即信息丢失（其 UP 失去回答位），故保留
    （「短 → 原文成胶囊」）。无 UP（防御）→ 全删（旧行为）。

    调用时机（spec 2026-07-20 修订的不变量：末段 raw 与「真实 Process Report」至少存其一）：
    finish 对已承载 LLM 真摘要 → close 时同步删；占位 finish 对 → 推迟到 bg 替换真摘要后
    补删（background_observe close 回调）。**不另产新 TASK_COMPACT_SUMMARY**（避免与 finish
    对重复）。幂等：raw 已删则 no-op。
    """
    from ctx_weft.protocols import MemoryKind, MemoryLayer
    # v2 P3a：升序视图（对话 + audit，SUMMARY 锚点天然不在），段界 = 末条 role=user 回合。
    view = await memory.load_view(
        scope, MemoryLayer.TASK, provider_ctx,
        kinds=[MemoryKind.CONVERSATION_TURN, MemoryKind.TOOL_AUDIT])
    ids: list[str] = []
    for r in view:
        if r.kind is MemoryKind.CONVERSATION_TURN and r.role == "user":
            ids = []  # 新段界：只删末段
            continue
        ids.append(r.id)
    if ids:
        await memory.fold(ids, [], provider_ctx)  # 纯遗忘（v2 P3d）


async def _close_one(memory, state, task, mem_content: str, outcome: str, ctx,
                     *, short: bool, act_recap: str, task_summary: str,
                     has_llm_summary: bool = True) -> list:
    """close 主体（task-resident，spec 2026-06-28）：bubble / 写 finish 对（不镜像 body）。

    每个结束 task 无条件写 finish 对、body 留 task 层（不 GC 子树）。长任务额外 supersede 末 raw
    段（短任务留全 raw）——`short` 决定 body raw-vs-压末段（spec §3.2）。返回事件列表。

    延迟折叠（spec 2026-07-20）：`has_llm_summary=False`（规则 observe 占位）时末段 raw 不在
    close 时删，`raw_fold_scope` 随 finish 对合成登记下去，bg 替换真摘要后补删；bg 失败则
    raw 永久保留（降级 = 保 raw，信息不丢）。
    """
    # Terminal finalize is single-entry by construction (retry is non-terminal; restore reschedules
    # only non-terminal tasks; re-dispatch uses a new task id), so the residue/bubble writes here
    # need no idempotency guard. [2026-06-23]
    events: list[Any] = []
    same_agent = task.creator_agent_id == task.assigned_agent_id
    cross_agent = bool(task.parent_task_id) and not same_agent
    is_own_root = (task.parent_task_id is None) or cross_agent
    # raw 所在层恒为本 task 的 task scope（≠ 嵌套 finish 对的 parent scope）
    raw_fold_scope = state.scope if (not short and not has_llm_summary) else None

    # 1) bubble 到 parent scope（dispatch marker 所在 scope）
    if task.parent_task_id and task.origin_tool_call_id and mem_content:
        parent_scope = _parent_scope_of(state, task)
        # 框通常已由 ensure_dispatch_frame_at_start（子 start 时）铸好，_ensure_dispatch_frame 在此
        # 幂等命中；仅对没走过 start 钩子的路径（旧数据 / start 与 close 之间崩溃）才真正补铸。
        # tool 槽用 replace=True：start 时写的是 running 态，此处须**替换**为终态而非新增一条。
        if cross_agent:
            # 跨 agent（spec 2026-06-28 §2.3）：dispatch result 写成 agent 层普通 conversation turn
            # （tool 回合），与 start_task / delegate 框靠 tool_call_id 配对、时间戳对齐保证相邻。
            report_prefix = "[outcome=fail] " if outcome == "fail" else ""
            frame_ts = await _ensure_dispatch_frame(memory, parent_scope, task, ctx.provider_ctx)
            await _put_dispatch_result(
                memory, parent_scope, task, f"{report_prefix}{mem_content}", frame_ts,
                ctx.provider_ctx, replace=True,
            )
            events.append(make_event(
                state, EventType.MEMORY_INGESTED,
                payload={"memory_event_type": MemoryEventType.AGENT_CONVERSATION_TURN.value,
                         "source": "dispatch_result", "content_length": len(mem_content)},
            ))
        elif same_agent:
            # 同 agent（spec 2026-06-30 §2.5）：配对 tool result 换终态文案，时间戳对齐框 → 严格
            # 相邻、排在子 body 之前。子真实产出由内联胶囊 body + 嵌套 finish 对承载。
            frame_ts = await _ensure_dispatch_frame(memory, parent_scope, task, ctx.provider_ctx)
            await _put_dispatch_result(
                memory, parent_scope, task, _dispatch_ack(task.title, outcome), frame_ts,
                ctx.provider_ctx, replace=True,
            )
            # 嵌套合成子自己的 finish 对（写进共享 agent scope，@close 时刻）
            await _synthesize_dispatch_pair(
                memory, parent_scope, task, act_recap, task_summary, outcome, ctx.provider_ctx,
                raw_fold_scope=raw_fold_scope)

    # 2) 自身 finish 对：own root（session 根或跨 agent 根）close 时无条件在 own scope 合成
    if is_own_root and mem_content:
        await _synthesize_dispatch_pair(memory, state.scope, task, act_recap, task_summary, outcome, ctx.provider_ctx,
                                        raw_fold_scope=raw_fold_scope)
        events.append(make_event(
            state, EventType.MEMORY_INGESTED,
            payload={"memory_event_type": MemoryEventType.AGENT_CONVERSATION_TURN.value,
                     "source": "root_finish_pair", "content_length": len(mem_content)},
        ))

    # task-resident（spec 2026-06-28 §3.2）：body 留 task 层、不 GC 子树。
    # 长任务 supersede 末 raw 段（保留 USER_PROMPT/TASK_COMPACT_SUMMARY 锚点）；短任务留全 raw。
    # 仅当 finish 对已承载 LLM 真摘要时同步删；占位（has_llm_summary=False）由 raw_fold_scope
    # 走延迟折叠——slot 命中在 _synthesize_dispatch_pair 内已补删，登记路径等 bg 替换后补删。
    if not short and has_llm_summary:
        await _supersede_final_raw_segment(memory, state.scope, ctx.provider_ctx)
    return events


async def synthesize_cancel_closure(memory, session_id: str, task, provider_ctx, reason_text: str) -> None:
    """统一取消胶囊闭合（Task 14）：所有 CANCELED 终态的公共收尾——父 ack 终态化 +
    `[outcome=cancelled]` finish 对。镜像 `_close_one` 的分支，但没有 LoopState，用
    `session_id` + task 字段直接推导 scope。

    调用点（TaskManager → runtime 侧 `_finalize_cancel_memory`）：
      - `cancel_all` 清队（reason="user_cancel"）；
      - 熔断清场对已启动的挂起/排队任务（reason="failure_threshold"）；
      - `on_task_finished(CANCELED)` funnel：在途协作取消终态坐实后（reason=task.error or 通用文案）。

    分支（同 `_close_one`）：
      ① task 有 parent_task_id + origin_tool_call_id → 父 scope 的派发对 tool 槽终态化替换为
         取消文案。**find-only**：找不到框（born-cancel，子任务从未真正 start 过、从未铸框）→
         整体跳过、不补铸（`_find_dispatch_frame`，不用 `_ensure_dispatch_frame`）。
      ② 同 agent（`creator_agent_id == assigned_agent_id`）→ 嵌套合成子自己的 finish 对，写进
         ①的父 scope（同 agent 共享 scope）。
      ③ 跨 agent 子任务 / root（`parent_task_id is None`）→ 在自己的 agent scope 合成 finish 对：
         `MemoryScope(session_id, task.id, task.assigned_agent_id or task.creator_agent_id)`。
    finish 对 `register_bg=False`：这不是正常 close 流程产生的，没有对应的后台 observe 会来替换它，
    登记只会累积永不消费的状态（同熔断收尾路径的既有惯例）。
    """
    same_agent = task.creator_agent_id == task.assigned_agent_id
    cross_agent = bool(task.parent_task_id) and not same_agent
    is_own_root = (task.parent_task_id is None) or cross_agent

    act_recap = "Task was cancelled before completion."
    task_summary = (
        f"Cancelled ({reason_text}) — no final output was produced; "
        f"the partial execution above is all that ran."
    )

    if task.parent_task_id and task.origin_tool_call_id:
        parent_scope = MemoryScope(
            session_id=session_id, task_id=task.parent_task_id, agent_id=task.creator_agent_id,
        )
        frame_ts = await _find_dispatch_frame(memory, parent_scope, task, provider_ctx)
        if frame_ts is None:
            # born-cancel：子任务从未真正 start（未走 ensure_dispatch_frame_at_start）、无框可闭——
            # 整体跳过，不补铸空框（补铸出的框只会带取消时刻，与「派发框=started_at」的不变量矛盾）。
            return
        ack_text = (
            f"Sub-task '{task.title}' was cancelled before completion ({reason_text}); "
            f"its partial execution below is incomplete."
        )
        await _put_dispatch_result(
            memory, parent_scope, task, ack_text, frame_ts, provider_ctx, replace=True,
        )
        if same_agent:
            await _synthesize_dispatch_pair(
                memory, parent_scope, task, act_recap, task_summary, "cancelled", provider_ctx,
                register_bg=False,
            )
            return

    if is_own_root:
        own_scope = MemoryScope(
            session_id=session_id, task_id=task.id,
            agent_id=task.assigned_agent_id or task.creator_agent_id,
        )
        await _synthesize_dispatch_pair(
            memory, own_scope, task, act_recap, task_summary, "cancelled", provider_ctx,
            register_bg=False,
        )


async def _synthesize_dispatch_pair(memory, scope, task, act_recap: str, task_summary: str,
                                    outcome: str, provider_ctx, *, register_bg: bool = True,
                                    raw_fold_scope=None) -> None:
    """close 合成 agent 层 finish 对（spec 2026-06-30 两段化）：
    assistant{content=act_recap + finish_task 调用} / tool{content=task_summary 综合总结}。
    own-root：占位先写，bg close observe 产新两段后经 _replace_finish_report 替换（A1）。

    register_bg=False（熔断收尾等 runtime 侧一次性合成路径）：跳过 pop_close_report /
    register_close_synth / _replace_finish_report 整段 bg-observe 联动——这条 finish 对不是
    正常 close 流程产生的、没有对应的后台 observe 会来替换它，登记只会累积永不消费的状态。

    raw_fold_scope（spec 2026-07-20 延迟折叠）：非 None = close 时占位、末段 raw 尚未删，
    此 scope 即 raw 所在 task 层。slot 命中（bg 真摘要已到）→ 替换后立即补删；否则随
    register_close_synth 登记，bg 替换成功后补删。register_bg=False 路径忽略（取消/熔断
    收尾从不折 raw）。
    """
    from ctx_weft.core.loop.steps.background_observe import (
        pop_close_report, register_close_synth, _replace_finish_report,
    )
    base = now_utc()
    tool_call_id = generate_id("tcall")
    # 反转契约（spec 2026-07-01）：答复正文由「内联的 task 层 body / blackboard mem_content」承载，
    # 故 finish 对的 assistant 槽用 act_recap（过程复述，≠ 答复），避免与内联 body 的答复重复；
    # finish_task 退化为无参收尾标记（不再把答复塞进 input.result）。tool 槽 = task_summary（process report）。
    report_prefix = _finish_report_prefix(task.title, outcome)
    summary_text = _finish_tool_text(task_summary, act_recap, outcome)

    await memory.ingest(
        MemoryEvent(
            kind=MemoryKind.CONVERSATION_TURN, layer=MemoryLayer.AGENT, scope=scope,
            content=act_recap, timestamp=base, role="assistant",
            metadata={"origin_task_id": task.id, "parent_task_id": task.parent_task_id,
                      "tool_calls": [{"id": tool_call_id,
                                      "name": qualify("control:finish_task"),
                                      "input": {}}]},
        ),
        provider_ctx,
    )
    await memory.ingest(
        MemoryEvent(
            kind=MemoryKind.CONVERSATION_TURN, layer=MemoryLayer.AGENT, scope=scope,
            content=f"{report_prefix}{summary_text}", timestamp=base, role="tool",
            metadata={"origin_task_id": task.id, "parent_task_id": task.parent_task_id,
                      "tool_call_id": tool_call_id},
        ),
        provider_ctx,
    )

    if register_bg:
        bg = pop_close_report(task.id)
        if bg is not None:
            bg_recap, bg_summary = bg
            await _replace_finish_report(memory, provider_ctx, scope, task.id, tool_call_id,
                                         bg_recap, bg_summary, outcome, task.title or "")
            if raw_fold_scope is not None:
                # 真摘要已落地（slot 命中替换完成）→ 立即补删末段 raw（延迟折叠的即时分支）
                await _supersede_final_raw_segment(memory, raw_fold_scope, provider_ctx)
        else:
            register_close_synth(task.id, tool_call_id, scope, outcome, raw_fold_scope)


class FinalizeStep(Step):
    name = "finalize"

    async def execute(self, state: LoopState, ctx: LoopContext) -> StepOutcome:
        task = state.task
        verdict = state.verdict
        outcome = verdict.task_outcome if verdict else "fail"
        summary = verdict.act_recap if verdict else ""            # → task.process_report（success/fail finish 对；retry 不再写，进度由段摘要承载）
        task_summary = verdict.task_summary if verdict else ""    # → 汇报给 parent 的 process report
        events: list[Any] = []

        # retry 超过上限 → 降级 fail（不再重试）。专属 error_code 区分「程序按重试上限
        # 熔断」与 observer 主动判死；死因 = 最后一轮 retry 判决暂存的受阻原因（task.error，
        # report_task_outcome 判 retry 时写入），机械退出轮没有判决则为空。
        retry_exhausted = outcome == "retry" and task.retry_count >= task.max_retries
        if retry_exhausted:
            outcome = "fail"
            task.status = "FAILED"
            task.observer_outcome = "fail"
            task.error_code = "TASK_FAILED_RETRY_EXHAUSTED"

        terminal = outcome in ("success", "fail")
        # 汇报给 parent（blackboard + cross_agent bubble）= 最终输出 + task_summary（process report 作用）；
        # task_summary 空时回退 act_recap。
        mem_content = _build_memory_content(task.outputs, task_summary or summary)

        # 1) 统一 close：bubble / 自身残留 / 软删自身对话 / GC 子树（spec 2026-06-23）。
        # has_llm_summary=verdict.reported：规则 observe 占位 close 不即折末段 raw，
        # 等 bg 真摘要落地后补删（spec 2026-07-20 延迟折叠）。
        if terminal and mem_content:
            events.extend(await finalize_task_memory(
                ctx.memory, state, task, mem_content, outcome, ctx,
                act_recap=summary, task_summary=task_summary,
                has_llm_summary=bool(verdict and verdict.reported),
            ))

        # 2) 按 outcome 分派（task.status 已由 ObserveStep 设置）
        if outcome == "success":
            task.finished_at = now_utc()
            task.process_report = summary
            events.append(make_event(
                state, EventType.TASK_FINISHED,
                payload={"outcome": "success", "summary": summary, "outputs": task.outputs},
            ))
        elif outcome == "fail":
            task.finished_at = now_utc()
            task.process_report = summary
            events.append(make_event(
                state, EventType.TASK_FAILED,
                payload={
                    "error_code": ("TASK_FAILED_RETRY_EXHAUSTED" if retry_exhausted
                                   else "TASK_FAILED_BY_OBSERVER"),
                    # 真死因：task.error = observer 的 task_failure_reason（判 fail 的根因，
                    # 或判 retry 暂存的本轮受阻原因——耗尽降级时用）。无死因（规则 observe
                    # 判死等）置空——act_recap 是过程复述，不冒充死因；host 拿 error_message
                    # 当 session_notice.reason_text 展示，空串由 host 按 error_code 补固定文案。
                    "error_message": task.error or "",
                    "retry_count": task.retry_count,
                },
            ))
        elif outcome == "retry":
            # retry 反馈由 observe 前台段折写的 TASK_COMPACT_SUMMARY 承载（spec 2026-07-01 §3.1）；
            # 不再写 process_report/process_report_at（旧 Progress So Far 字段路径已废）。
            # 机械退出（max_turns/context_limit）也归到这里：重排再跑，受 max_retries 兜底。
            task.outputs = None
            task.retry_count += 1
            events.append(make_event(
                state, EventType.TASK_REQUEUED,
                payload={"outcome": "retry", "summary": summary, "retry_count": task.retry_count},
            ))

        # 3) success 时发布 BLACKBOARD，供任何 agent 按 task_id 精确召回
        if outcome == "success" and mem_content:
            await ctx.memory.ingest(
                MemoryEvent(
                    kind=MemoryKind.PUBLICATION, layer=MemoryLayer.SESSION,
                    scope=state.scope,
                    content=mem_content,
                    timestamp=now_utc(),
                    role="assistant",
                    topic=task.id,
                    metadata={"task_id": task.id, "title": task.title, "outcome": outcome,
                              "parent_task_id": task.parent_task_id},
                ),
                ctx.provider_ctx,
            )
            events.append(make_event(
                state, EventType.BLACKBOARD_PUBLISHED,
                payload={"topic": task.id, "content_length": len(mem_content), "parent_task_id": task.parent_task_id},
            ))

        events.append(make_event(
            state, EventType.TASK_FINALIZED,
            payload={"task_id": task.id, "outcome": outcome},
        ))

        return StepOutcome(next_step=None, state_patch={}, events=events)


def _output_text(outputs: Any) -> str:
    """Extract the plain answer text from task.outputs (str or [{type:text,text:...}])."""
    if isinstance(outputs, list):
        return next(
            (p.get("text", "") for p in outputs if isinstance(p, dict) and p.get("type") == "text"),
            "",
        )
    if isinstance(outputs, str):
        return outputs
    return ""


def _build_memory_content(outputs: Any, summary: str) -> str:
    """合并 task outputs 和 observer summary，对齐 miniAgents _write_execution_memory。

    格式："{output_text}\\n\\nProcess Report: {summary}"
    只有 summary 时："{summary}"
    """
    parts = [p for p in [_output_text(outputs), summary] if p]
    return "\n\nProcess Report: ".join(parts)
