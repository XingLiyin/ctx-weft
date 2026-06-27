"""Task 11: 端到端 golden 测试（情况 1–8 + 边界）。

黄金测试风格：构造 task 层事件序列 → 调 finalize_task_memory（或直接 _synthesize_dispatch_pair）
→ 断言 agent 层重建出的 AGENT_CONVERSATION_TURN 序列（role/内容要点/tool_calls/配对）。

覆盖范围（spec §4 / §5）：
  A1  单段正常 finish（无段摘要，只 finish 对）
  A3  ② 流中打断（段摘要含「被打断」、半截 assistant/cancelled tool 不进胶囊）
  A4  ① 编辑式打断（两 user 锚点相邻，其间无摘要）
  A6  fail 收尾（result="(无最终产出)"，tool "[outcome=fail]"，配对不悬挂）
  A10 short task 不合成胶囊（负向——断言无 AGENT_CONVERSATION_TURN 写入）
  H3  递归嵌套（同 agent 孙任务）
  H4  跨 agent 隔离（parent 看不到子 agent 的段摘要/user 锚点）
  H8  短同 agent 子任务未配对隐去

已在其他 test 文件覆盖（不重复）：
  A2  一次 HITL → test_capsule_interleaved.py::test_interleaved_capsule_order_and_roles
  finish 对结构/配对 → test_capsule_interleaved.py + test_capsule_render.py
  fold → test_root_subtree_fold.py
  gc_subtree → test_gc_preserve_dispatch.py
  H1/H2 同 agent 嵌套 → test_subtask_nesting.py
  H4 部分（bubble 内容） → test_subtask_nesting.py
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from ctx_weft.core.loop.steps.finalize import _synthesize_dispatch_pair, finalize_task_memory
from ctx_weft.core.state.models import NormalTaskSettings, Task
from ctx_weft.protocols import MemoryEvent, MemoryEventType, MemoryScope, ProviderContext
from ctx_weft.protocols.template import LoopConfig
from ctx_weft.providers.memory_blackboard.in_memory import InMemoryMemoryProvider

pytestmark = pytest.mark.asyncio

T = MemoryEventType
_BASE = datetime(2026, 1, 1, tzinfo=UTC)

SESSION = "s1"


# ─── 辅助函数 ──────────────────────────────────────────────────────────────────

def _pctx() -> ProviderContext:
    return ProviderContext(session_id=SESSION, tenant_id="default")


def _task_scope(task_id: str, agent_id: str = "ag1") -> MemoryScope:
    """task 层 scope（含 task_id）。"""
    return MemoryScope(session_id=SESSION, task_id=task_id, agent_id=agent_id)


def _agent_scope(agent_id: str = "ag1") -> MemoryScope:
    """agent 层 scope（task_id=None）。"""
    return MemoryScope(session_id=SESSION, task_id=None, agent_id=agent_id)


def _ev(type_: MemoryEventType, scope: MemoryScope, content: str, t: int,
        role: str | None = None, **meta) -> MemoryEvent:
    return MemoryEvent(
        type=type_, scope=scope, content=content,
        timestamp=_BASE + timedelta(seconds=t), role=role, metadata=meta,
    )


def _make_task(task_id: str = "t1", agent_id: str = "ag1", prompt: str = "初始请求",
               parent_task_id: str | None = None, creator_agent_id: str | None = None,
               outputs: str | None = "最终答复", title: str = "测试任务") -> Task:
    return Task(
        id=task_id, session_id=SESSION, status="FINISHED", tenant_id="default",
        assigned_agent_id=agent_id,
        creator_agent_id=creator_agent_id or agent_id,
        parent_task_id=parent_task_id,
        title=title, description="", user_prompt=prompt,
        settings=NormalTaskSettings(),
        outputs=outputs,
    )


async def _caps(mem: InMemoryMemoryProvider, scope: MemoryScope) -> list:
    """召回 agent 层 AGENT_CONVERSATION_TURN，按 timestamp 升序（oldest first）。"""
    recs = await mem.recall_recent(scope, [T.AGENT_CONVERSATION_TURN], 500, _pctx())
    return list(reversed(recs))


class _FakeTM:
    """最简 TaskManager stub：没有子任务。"""
    def __init__(self, children: dict | None = None):
        self._children = children or {}

    def children_of(self, task_id: str) -> set[str]:
        return self._children.get(task_id, set())


def _state(task: Task, scope: MemoryScope, loop_config: LoopConfig | None = None):
    agent = SimpleNamespace(id=scope.agent_id, loop_config=loop_config or LoopConfig())
    session = SimpleNamespace(id=SESSION, tenant_id="default")
    return SimpleNamespace(
        run_id="run1", sequence_counter=0,
        session=session, scope=scope, task=task, agent=agent,
    )


def _loop_ctx(mem: InMemoryMemoryProvider, tm=None):
    return SimpleNamespace(
        memory=mem, provider_ctx=_pctx(),
        task_manager=tm or _FakeTM(),
    )


# ─── A1: 单段正常 finish（无段摘要，只 finish 对） ──────────────────────────────

async def test_A1_single_segment_finish() -> None:
    """情况 1: task 层只有原始 UP + LLM/TOOL 处理（无后台 observe 产段摘要），close 后
    胶囊 = [user 原始][assistant finish_task(result=outputs)][tool Process Report]。
    无独立段摘要 assistant 回合，共 3 条（user + finish pair）。

    注：LLM/TOOL 在 close 后被 _supersede_own_conversation 软删，不进胶囊；
        _synthesize_dispatch_pair 召回此时只见 USER_PROMPT（段摘要未产出）。
        胶囊 = 仅 finish 对（2 条） + user 锚点（1 条）= 3 条。
    """
    mem = InMemoryMemoryProvider()
    tsc = _task_scope("t1")
    asc = _agent_scope()

    # task 层：原始 UP + 若干 LLM/TOOL（无段摘要）
    await mem.ingest(_ev(T.USER_PROMPT, tsc, "把 auth 从 session 改成 JWT，并补测试", 1, role="user"), _pctx())
    await mem.ingest(_ev(T.LLM_RESPONSE, tsc, "正在改…", 2, role="assistant"), _pctx())
    await mem.ingest(_ev(T.TOOL_RESULT, tsc, "tool ok", 3, role="tool"), _pctx())

    task = _make_task(outputs="已切到 JWT：三处改完，8 测试全过")
    mem_content = "已切到 JWT：三处改完，8 测试全过\n\nProcess Report: 成功。读 session.py 确认 3 处改 jwt，补 8 测试。"

    await _synthesize_dispatch_pair(mem, asc, task, mem_content, "success", _pctx())

    caps = await _caps(mem, asc)

    # 期望：user + finish_assistant + finish_tool = 3 条（LLM_RESPONSE/TOOL_RESULT 已被 supersede 在 step3，
    # 此处调 _synthesize_dispatch_pair 前未 supersede，所以仍能看到它们；但 A1 约定无段摘要）
    # 实际：_synthesize_dispatch_pair 召回 USER_PROMPT + LLM_RESPONSE + TOOL_RESULT（共 3 task 事件）
    # + finish pair（2 条）= 5 条。
    # A1 的语义是「无段摘要」（TASK_COMPACT_SUMMARY 为零），不是无 LLM/TOOL 镜像。
    # 断言核心约束：
    # 1. 第一条是 user（原始 UP 逐字）
    # 2. 最后两条是 finish pair（assistant finish_task + tool Process Report）
    # 3. 无 TASK_COMPACT_SUMMARY 镜像的 role=assistant 段摘要（tool_calls=[] 且 content 含摘要字样的回合）

    assert len(caps) == 5, f"expected exactly 5 capsule turns (UP + LLM + TOOL + finish pair), got {len(caps)}"

    # 首条 user
    assert caps[0].role == "user", f"first turn must be user, got {caps[0].role}"
    assert "JWT" in caps[0].content or "session" in caps[0].content, (
        f"first user turn should contain original prompt; got {caps[0].content!r}"
    )

    # 末两条 finish pair
    finish_asst = caps[-2]
    finish_tool = caps[-1]
    assert finish_asst.role == "assistant", f"finish pair[0] must be assistant, got {finish_asst.role}"
    assert finish_tool.role == "tool", f"finish pair[1] must be tool, got {finish_tool.role}"

    tcs = finish_asst.metadata.get("tool_calls", [])
    assert len(tcs) == 1 and tcs[0]["name"].endswith("finish_task"), (
        f"finish assistant must have finish_task tool_call, got tool_calls={tcs}"
    )
    assert tcs[0]["input"]["result"] == "已切到 JWT：三处改完，8 测试全过", (
        f"finish result must equal task.outputs; got {tcs[0]['input']['result']!r}"
    )
    assert "Process Report:" in finish_tool.content, (
        f"finish tool must contain 'Process Report:'; got {finish_tool.content!r}"
    )

    # tool_call_id 配对完整
    assert finish_tool.metadata.get("tool_call_id") == tcs[0]["id"], (
        "finish pair tool_call_id must match assistant tool_calls[0].id"
    )

    # 无 TASK_COMPACT_SUMMARY 专属段摘要镜像（A1 scenario: 没有后台 observe 产出段摘要）
    # TASK_COMPACT_SUMMARY 镜像后 role=assistant 且 content 含「段」「摘要」等字样；
    # 但 LLM_RESPONSE 也可能镜像为 assistant（tool_calls=[]）—— A1 约定仅无 TASK_COMPACT_SUMMARY。
    # 验证：任何 AGENT_CONVERSATION_TURN 的来源不是 TASK_COMPACT_SUMMARY（没有"段摘要"内容）
    compact_summary_mirrors = [
        c for c in caps
        if c.role == "assistant"
        and not c.metadata.get("tool_calls")
        and ("摘要" in c.content or "段①" in c.content or "段②" in c.content)
        and c not in (finish_asst,)
    ]
    assert len(compact_summary_mirrors) == 0, (
        f"A1: no TASK_COMPACT_SUMMARY mirror turns expected (no background observe in this scenario); "
        f"found {[(c.role, c.content[:60]) for c in compact_summary_mirrors]}"
    )

    # all turns have origin_task_id
    assert all(c.metadata.get("origin_task_id") == "t1" for c in caps), (
        "all capsule turns must have origin_task_id=t1"
    )


# ─── A3: ② 流中打断（段摘要含「被打断」，半截 assistant/cancelled tool 不进胶囊） ──

async def test_A3_mid_stream_interrupt_annotation() -> None:
    """情况 3: 段①含 LLM_RESPONSE(interrupted=True) + cancelled TOOL_RESULT；
    后台 observe 产 TASK_COMPACT_SUMMARY（含「被打断」字样，通过 apply_compact 折叠后幸存）；
    打断后用户新指令为下一 USER_PROMPT 锚点。

    _synthesize_dispatch_pair 快照时 interrupted LLM/cancelled TOOL 被 apply_compact 折进摘要
    后已被 supersede，不出现在胶囊。断言：
    - 段①摘要回合存在且含「被打断」
    - 无独立 interrupted half-assistant / cancelled tool 镜像
    - 打断后 UP 作为下一 user 锚点存在
    - finish pair 完整
    """
    mem = InMemoryMemoryProvider()
    tsc = _task_scope("t1")
    asc = _agent_scope()

    # 段①：interrupted assistant + cancelled tool（模拟流中打断）
    await mem.ingest(_ev(
        T.LLM_RESPONSE, tsc, "正用 PyJWT 写 login() 签发，写到一半…", 1,
        role="assistant", interrupted=True,
    ), _pctx())
    await mem.ingest(_ev(
        T.TOOL_RESULT, tsc, "[INTERRUPTED]", 2,
        role="tool", cancelled=True, status="INTERRUPTED",
    ), _pctx())

    # 模拟后台 observe：apply_compact 折叠 interrupted 段 → 产 TASK_COMPACT_SUMMARY（含被打断标注）
    # 并 supersede interrupted LLM/TOOL
    all_task_recs = await mem.recall_recent(tsc, [T.LLM_RESPONSE, T.TOOL_RESULT], 500, _pctx())
    ids_to_supersede = [r.id for r in all_task_recs]
    summary_event = MemoryEvent(
        type=T.TASK_COMPACT_SUMMARY,
        scope=tsc,
        content="〔段①·被用户打断〕正用 PyJWT 写 login() 签发，写到一半被打断。",
        timestamp=_BASE + timedelta(seconds=2),
        role="user",  # apply_compact 写入时 role=user，_synthesize_dispatch_pair 会纠正为 assistant
        metadata={},
    )
    await mem.ingest(summary_event, _pctx())
    if ids_to_supersede:
        await mem.supersede(ids_to_supersede, _pctx())

    # 原始 UP（打断前）
    up1 = MemoryEvent(
        type=T.USER_PROMPT, scope=tsc, content="把 auth 从 session 改成 JWT，并补测试",
        timestamp=_BASE + timedelta(seconds=0), role="user", metadata={},
    )
    await mem.ingest(up1, _pctx())

    # 打断后新指令 UP
    up2 = MemoryEvent(
        type=T.USER_PROMPT, scope=tsc, content="别用 PyJWT，用 authlib",
        timestamp=_BASE + timedelta(seconds=3), role="user", metadata={},
    )
    await mem.ingest(up2, _pctx())

    task = _make_task(prompt="把 auth 从 session 改成 JWT，并补测试",
                      outputs="改用 authlib 重写签发/校验，3 处改完")
    mem_content = "改用 authlib 重写签发/校验，3 处改完\n\nProcess Report: 成功。改用 authlib 重写签发/校验，3 处改完。"

    await _synthesize_dispatch_pair(mem, asc, task, mem_content, "success", _pctx())

    caps = await _caps(mem, asc)
    roles = [c.role for c in caps]

    # 首条 user（原始 UP）
    assert caps[0].role == "user", f"first turn must be user; roles={roles}"
    assert "JWT" in caps[0].content or "session" in caps[0].content

    # 段①摘要回合（role=assistant，含「被打断」）
    summary_turns = [c for c in caps if c.role == "assistant" and "被打断" in c.content]
    assert len(summary_turns) >= 1, (
        f"expected segment-summary turn with '被打断'; got turns={[(c.role, c.content[:60]) for c in caps]}"
    )

    # 无裸露 interrupted LLM / cancelled TOOL 镜像
    interrupted_raw = [
        c for c in caps
        if c.metadata.get("interrupted") or c.metadata.get("cancelled") or
           c.metadata.get("status") == "INTERRUPTED"
    ]
    assert not interrupted_raw, (
        f"interrupted/cancelled raw turns must not appear in capsule; found {interrupted_raw}"
    )

    # 打断后 UP 锚点存在
    after_interrupt_user = [c for c in caps if c.role == "user" and "authlib" in c.content]
    assert after_interrupt_user, (
        "post-interrupt USER_PROMPT must be preserved as user anchor in capsule"
    )

    # finish pair 完整
    assert caps[-2].role == "assistant"
    assert caps[-1].role == "tool"
    assert "Process Report:" in caps[-1].content


# ─── A4: ① 编辑式打断（两 user 锚点相邻，其间无段摘要） ──────────────────────

async def test_A4_edit_interrupt_adjacent_anchors() -> None:
    """情况 4: task 层 = [USER_PROMPT 原始][USER_PROMPT edit-note][处理][finish]。
    断言胶囊里两条 user 锚点相邻保留，原始在前 edit-note 在后，其间无段摘要 assistant 回合。

    spec §3.7: interrupt_edit_note 产出一条 USER_PROMPT（「上一条…取消…改为」），
    原始 UP 仍在；二者之间无处理段（打断在出 token 前）。
    """
    mem = InMemoryMemoryProvider()
    tsc = _task_scope("t1")
    asc = _agent_scope()

    # 原始 UP（t=0）
    await mem.ingest(_ev(
        T.USER_PROMPT, tsc, "把 auth 模块从 session 改成 JWT", 0, role="user",
    ), _pctx())

    # edit-note UP（t=1）——模拟 interrupt_edit_note 产出的 USER_PROMPT
    await mem.ingest(_ev(
        T.USER_PROMPT, tsc,
        "(上一条已取消) 改为：把 auth 改成 OAuth2，不要 JWT",
        1, role="user",
    ), _pctx())

    # 后续处理（edit 后的处理段——此处无段摘要，直接 finish）
    await mem.ingest(_ev(T.LLM_RESPONSE, tsc, "正在实现 OAuth2…", 2, role="assistant"), _pctx())

    task = _make_task(prompt="把 auth 模块从 session 改成 JWT",
                      outputs="按 OAuth2 实现完成")
    mem_content = "按 OAuth2 实现完成\n\nProcess Report: 成功。按 OAuth2 实现…"

    await _synthesize_dispatch_pair(mem, asc, task, mem_content, "success", _pctx())

    caps = await _caps(mem, asc)

    # 找两条 user 锚点
    user_turns = [c for c in caps if c.role == "user"]
    assert len(user_turns) >= 2, (
        f"expected at least 2 user anchor turns (original + edit-note); got {len(user_turns)}: "
        f"{[(c.role, c.content[:50]) for c in caps]}"
    )

    original = user_turns[0]
    edit_note = user_turns[1]

    assert "JWT" in original.content or "session" in original.content, (
        f"first user turn should be original prompt; got {original.content!r}"
    )
    assert "取消" in edit_note.content or "改为" in edit_note.content, (
        f"second user turn should be edit-note; got {edit_note.content!r}"
    )
    assert "OAuth2" in edit_note.content, (
        f"edit-note should mention new direction (OAuth2); got {edit_note.content!r}"
    )

    # 两条 user 锚点之间无段摘要 assistant（两者在 caps 中相邻）
    orig_idx = caps.index(original)
    edit_idx = caps.index(edit_note)
    between = caps[orig_idx + 1: edit_idx]
    assistant_between = [c for c in between if c.role == "assistant"]
    assert not assistant_between, (
        f"no assistant segment-summary between the two adjacent user anchors (edit-interrupt); "
        f"found {[(c.role, c.content[:40]) for c in assistant_between]}"
    )

    # finish pair 末尾完整
    assert caps[-2].role == "assistant"
    assert caps[-1].role == "tool"
    assert "Process Report:" in caps[-1].content


# ─── A6: fail 收尾（result 占位，tool "[outcome=fail]"，配对完整） ───────────────

async def test_A6_fail_outcome() -> None:
    """情况 6: outcome=fail，task.outputs 为空。
    断言 finish 对 = [assistant finish_task(result="(无最终产出)")][tool "[outcome=fail] Process Report: …"]。
    tool_call 配对不悬挂。
    """
    mem = InMemoryMemoryProvider()
    tsc = _task_scope("t1")
    asc = _agent_scope()

    await mem.ingest(_ev(
        T.USER_PROMPT, tsc, "把 auth 从 session 改成 JWT，并补测试", 0, role="user",
    ), _pctx())

    task = _make_task(outputs=None)  # fail → no outputs
    mem_content = "失败报告"  # 无 outputs，仅 process report

    await _synthesize_dispatch_pair(mem, asc, task, mem_content, "fail", _pctx())

    caps = await _caps(mem, asc)

    finish_asst = caps[-2]
    finish_tool = caps[-1]

    # finish pair roles
    assert finish_asst.role == "assistant", f"finish pair[0] must be assistant, got {finish_asst.role}"
    assert finish_tool.role == "tool", f"finish pair[1] must be tool, got {finish_tool.role}"

    # finish tool_call: result = "(无最终产出)"
    tcs = finish_asst.metadata.get("tool_calls", [])
    assert len(tcs) == 1, f"finish assistant must have exactly 1 tool_call; got {tcs}"
    assert tcs[0]["name"].endswith("finish_task"), f"tool name must end with finish_task; got {tcs[0]['name']}"
    assert tcs[0]["input"]["result"] == "(无最终产出)", (
        f"fail task with no outputs must use '(无最终产出)' placeholder; got {tcs[0]['input']['result']!r}"
    )

    # tool content: "[outcome=fail] Process Report: ..."
    assert "[outcome=fail]" in finish_tool.content, (
        f"fail tool content must have '[outcome=fail]' prefix; got {finish_tool.content!r}"
    )
    assert "Process Report:" in finish_tool.content, (
        f"fail tool content must contain 'Process Report:'; got {finish_tool.content!r}"
    )

    # 配对完整（tool_call_id 匹配）
    assert finish_tool.metadata.get("tool_call_id") == tcs[0]["id"], (
        "fail finish pair: tool_call_id must match assistant tool_calls[0].id"
    )

    # origin_task_id 一致
    assert all(c.metadata.get("origin_task_id") == "t1" for c in caps)


# ─── A10: short task 不合成胶囊（负向） ────────────────────────────────────────

async def test_A10_short_task_no_capsule() -> None:
    """A10 负向：短叶子 task finish 后 _synthesize_dispatch_pair 不被调用，
    agent 层无 AGENT_CONVERSATION_TURN 写入（task 层对话也不被 supersede）。

    判定 short: LLM_RESPONSE turns ≤ turn_cap AND token_est ≤ threshold。
    LoopConfig 默认 short_task_turn_cap=3, short_task_token_threshold=2000。
    此处构造极短对话确保 is_short=True。
    """
    mem = InMemoryMemoryProvider()
    tsc = _task_scope("t1")
    asc = _agent_scope()

    # 极短：1 轮 LLM（< turn_cap），极小 token
    await mem.ingest(_ev(T.USER_PROMPT, tsc, "hello", 0, role="user"), _pctx())
    await mem.ingest(_ev(T.LLM_RESPONSE, tsc, "hi", 1, role="assistant"), _pctx())

    task = _make_task(outputs="hi", prompt="hello")
    # task 无 parent → is_own_root=True；但 is_short_leaf=True → _synthesize_dispatch_pair 不调

    loop_config = LoopConfig()  # 默认 short_task_turn_cap=2, short_task_token_threshold=5000

    await finalize_task_memory(
        mem, _state(task, tsc, loop_config),
        task, "hi\n\nProcess Report: 极短任务", "success", _loop_ctx(mem),
    )

    # 断言：agent 层无 AGENT_CONVERSATION_TURN（短 task 不合成胶囊）
    agent_turns = await mem.recall_recent(asc, [T.AGENT_CONVERSATION_TURN], 500, _pctx())
    assert agent_turns == [], (
        f"short task must NOT synthesize capsule (AGENT_CONVERSATION_TURN); "
        f"found {[(r.role, r.content[:40]) for r in agent_turns]}"
    )

    # task 层对话也不被 supersede（short task 维持 OPEN，不清对话）
    task_recs = await mem.recall_recent(tsc, [T.USER_PROMPT, T.LLM_RESPONSE], 500, _pctx())
    assert task_recs, (
        "short task: task-layer records must NOT be superseded (task stays logically OPEN)"
    )


# ─── H3: 递归嵌套（同 agent 孙任务） ──────────────────────────────────────────

async def test_H3_recursive_nesting_grandchild() -> None:
    """H3: 同 agent 子任务再委派同 agent 孙任务（recursion depth=2）。
    断言孙胶囊平铺嵌在父 agent scope，各层 origin_task_id 归属正确，深度 ≤ 2 层均可见。

    场景：root(ag1) → child(ag1) → grandchild(ag1)
    grandchild close → 胶囊落 parent(child) scope = ag1 scope（同 agent）
                       且 bubble_to parent(child) = "Sub-task '孙子任务' scheduled."
    child close → 胶囊（含 grandchild 子胶囊）落 parent(root) scope = ag1 scope
    root 也在同 ag1 scope，所有胶囊都在 ag1 agent scope，靠 origin_task_id 区分。
    """
    mem = InMemoryMemoryProvider()

    # ── Grandchild（孙）──
    gc_id = "gc1"
    gc_parent_id = "child1"
    gc_scope = _task_scope(gc_id, "ag1")

    # 给孙任务写足够多 token 以脱离 short 判定
    await mem.ingest(_ev(T.USER_PROMPT, gc_scope, "孙子任务：收集数据", 1, role="user"), _pctx())
    for i in range(3):
        await mem.ingest(_ev(T.LLM_RESPONSE, gc_scope, "x " * 4000, i + 2, role="assistant"), _pctx())

    gc_task = _make_task(
        task_id=gc_id, agent_id="ag1",
        creator_agent_id="ag1", parent_task_id=gc_parent_id,
        prompt="孙子任务：收集数据", outputs="孙子收集完毕",
        title="孙子任务",
    )
    gc_task.origin_tool_call_id = "oc_gc"

    gc_mem_content = "孙子收集完毕\n\nProcess Report: 孙任务成功"
    await finalize_task_memory(
        mem, _state(gc_task, gc_scope),
        gc_task, gc_mem_content, "success", _loop_ctx(mem),
    )

    # ── Child（子）──
    child_id = "child1"
    child_parent_id = "root1"
    child_scope = _task_scope(child_id, "ag1")

    await mem.ingest(_ev(T.USER_PROMPT, child_scope, "子任务：研究 X", 10, role="user"), _pctx())
    for i in range(3):
        await mem.ingest(_ev(T.LLM_RESPONSE, child_scope, "y " * 4000, 11 + i, role="assistant"), _pctx())

    child_task = _make_task(
        task_id=child_id, agent_id="ag1",
        creator_agent_id="ag1", parent_task_id=child_parent_id,
        prompt="子任务：研究 X", outputs="子任务完成",
        title="子任务",
    )
    child_task.origin_tool_call_id = "oc_child"
    # child 知道自己有孙任务
    child_tm = _FakeTM({child_id: {gc_id}})

    child_mem_content = "子任务完成\n\nProcess Report: 子任务成功"
    await finalize_task_memory(
        mem, _state(child_task, child_scope),
        child_task, child_mem_content, "success", _loop_ctx(mem, child_tm),
    )

    # 断言：child close 后，parent scope (task_id=root1, ag1) 有子胶囊 AGENT_CONVERSATION_TURN
    parent_scope = _task_scope(child_parent_id, "ag1")
    parent_agent_scope = _agent_scope("ag1")  # agent scope (task_id=None)

    all_child_caps = await mem.recall_recent(
        parent_scope, [T.AGENT_CONVERSATION_TURN], 500, _pctx(),
    )
    child_caps_by_origin = {r.metadata.get("origin_task_id") for r in all_child_caps}

    # child 的胶囊（origin=child1）写入 parent scope
    assert "child1" in child_caps_by_origin, (
        f"child capsule (origin=child1) must appear in parent scope; found origins={child_caps_by_origin}"
    )

    # 孙胶囊的 AGENT_CONVERSATION_TURN（origin=gc1）
    # grandchild 作为 child 的子任务，其胶囊通过 child close 写入 child 的 parent scope
    # 但孙胶囊在 child close 时已写入 child.parent_scope（root scope），检查 grandchild origin
    gc_caps = [r for r in all_child_caps if r.metadata.get("origin_task_id") == gc_id]
    assert gc_caps, (
        f"grandchild capsule (origin=gc1) must be visible in root parent scope via nested finalize; "
        f"found origins={child_caps_by_origin}"
    )

    # 各层 origin_task_id 正确（child1/gc1 两层均在）
    assert "child1" in child_caps_by_origin and gc_id in child_caps_by_origin, (
        f"both child and grandchild origins expected; got {child_caps_by_origin}"
    )

    # gc 的胶囊 user 锚点存在
    gc_user_turns = [r for r in gc_caps if r.role == "user"]
    assert gc_user_turns, "grandchild capsule must include user anchor turn"
    assert "孙子任务" in gc_user_turns[0].content, (
        f"grandchild user anchor must contain original prompt; got {gc_user_turns[0].content!r}"
    )

    # gc 的 finish pair 存在（finish_task tool 内容含 Process Report）
    gc_tool_turns = [r for r in gc_caps if r.role == "tool"]
    assert gc_tool_turns, "grandchild capsule must include finish-pair tool turn"
    assert "Process Report:" in gc_tool_turns[0].content, (
        f"grandchild finish tool must contain 'Process Report:'; got {gc_tool_turns[0].content!r}"
    )


# ─── H4: 跨 agent 隔离（parent 看不到子 agent 段摘要/user 锚点） ────────────────

async def test_H4_cross_agent_isolation() -> None:
    """情况 8 / H4: root 委派跨 agent 子任务（use_subagent=True，creator≠assigned）。

    (a) parent scope 的 TASK_DISPATCH_RESULT content = mem_content（含 Process Report:）
        而非 "Sub-task '…' scheduled."
    (b) parent agent scope 无 origin_task_id=child.id 的 AGENT_CONVERSATION_TURN
        （子 agent 内部交互对 parent 不可见）
    (c) 子 agent scope（ag2）有独立完整胶囊（user 锚点 + 段摘要/LLM + finish pair）

    这验证 spec §3.9 跨 agent = 黑盒。
    """
    mem = InMemoryMemoryProvider()

    child_id = "c_cross"
    child_agent = "ag2"
    parent_id = "p1"
    parent_agent = "ag1"

    child_scope = _task_scope(child_id, child_agent)

    # 子 agent 的 task 层对话（子 agent 内部，parent 不可见）
    # 须有足够内容令 is_short_leaf=False（LLM_RESPONSE turns > short_task_turn_cap or token > threshold）
    await mem.ingest(_ev(T.USER_PROMPT, child_scope, "收集数据：2020-2024 X 领域", 1, role="user"), _pctx())
    for i in range(3):
        await mem.ingest(_ev(T.LLM_RESPONSE, child_scope, "z " * 4000, i + 2, role="assistant"), _pctx())
    # 模拟后台 observe 已折段摘要（TASK_COMPACT_SUMMARY），LLM_RESPONSE 被 supersede
    # 这里为简化直接写摘要+留 LLM_RESPONSE（不 supersede，保持对话可 snapshot）
    await mem.ingest(_ev(
        T.TASK_COMPACT_SUMMARY, child_scope,
        "〔子段①〕检索 5 源、下载并清洗…", 5, role="user",  # apply_compact stores as role=user
    ), _pctx())

    child_task = _make_task(
        task_id=child_id, agent_id=child_agent,
        creator_agent_id=parent_agent,  # cross-agent: creator ≠ assigned
        parent_task_id=parent_id,
        prompt="收集数据：2020-2024 X 领域", outputs="收集到 5 份数据集",
        title="收集数据",
    )
    child_task.origin_tool_call_id = "oc_cross"

    child_mem_content = "收集到 5 份数据集，已清洗。\n\nProcess Report: 成功，覆盖 5 年。"

    await finalize_task_memory(
        mem, _state(child_task, child_scope),
        child_task, child_mem_content, "success", _loop_ctx(mem),
    )

    parent_scope_task = _task_scope(parent_id, parent_agent)
    parent_scope_agent = _agent_scope(parent_agent)
    child_scope_agent = _agent_scope(child_agent)

    # (a) parent TASK_DISPATCH_RESULT = mem_content（含 Process Report:）
    parent_results = await mem.recall_recent(
        parent_scope_task, [T.TASK_DISPATCH_RESULT], 200, _pctx(),
    )
    cross_results = [r for r in parent_results if r.metadata.get("child_task_id") == child_id]
    assert cross_results, "cross-agent child must bubble TASK_DISPATCH_RESULT to parent scope"
    assert "Process Report:" in cross_results[0].content, (
        f"cross-agent bubble content must be mem_content (with Process Report:); "
        f"got {cross_results[0].content!r}"
    )
    assert cross_results[0].content == child_mem_content, (
        f"cross-agent bubble must equal full mem_content; "
        f"got {cross_results[0].content!r}"
    )

    # (b) parent agent scope 无 origin=child.id 的 AGENT_CONVERSATION_TURN（隔离）
    parent_agent_turns = await mem.recall_recent(
        parent_scope_agent, [T.AGENT_CONVERSATION_TURN], 200, _pctx(),
    )
    child_in_parent = [r for r in parent_agent_turns if r.metadata.get("origin_task_id") == child_id]
    assert not child_in_parent, (
        f"cross-agent child capsule must NOT appear in parent agent scope; "
        f"found {[(r.role, r.content[:40]) for r in child_in_parent]}"
    )

    # (c) 子 agent scope 有独立完整胶囊（cross_agent = is_own_root → _synthesize_dispatch_pair）
    child_agent_turns = await mem.recall_recent(
        child_scope_agent, [T.AGENT_CONVERSATION_TURN], 200, _pctx(),
    )
    child_turns_by_origin = [r for r in child_agent_turns if r.metadata.get("origin_task_id") == child_id]
    assert child_turns_by_origin, (
        f"cross-agent child must have its own capsule in child agent scope (ag2); "
        f"found {[(r.role, r.content[:40]) for r in child_agent_turns]}"
    )

    # 子胶囊含 user 锚点
    child_user = [r for r in child_turns_by_origin if r.role == "user"]
    assert child_user, "child capsule in child agent scope must include user anchor"
    assert "收集数据" in child_user[0].content, (
        f"child capsule user anchor must contain original prompt; got {child_user[0].content!r}"
    )

    # 子胶囊含 finish pair（tool Process Report）
    child_finish_tool = [r for r in child_turns_by_origin if r.role == "tool"]
    assert child_finish_tool, "child capsule must include finish-pair tool turn"
    assert "Process Report:" in child_finish_tool[0].content

    # (c) 隔离验证：parent 召回看不到子 agent 内部段摘要（TASK_COMPACT_SUMMARY 镜像不在 parent）
    # 子 agent 的 TASK_COMPACT_SUMMARY 被镜像为 AGENT_CONVERSATION_TURN 仅在 child agent scope
    child_summary_in_parent = [
        r for r in parent_agent_turns
        if r.role == "assistant" and "检索" in (r.content or "")
    ]
    assert not child_summary_in_parent, (
        f"parent must NOT see child agent's segment summary; "
        f"found {[(r.role, r.content[:40]) for r in child_summary_in_parent]}"
    )


# ─── H8: 短同 agent 子任务未配对隐去 ──────────────────────────────────────────

async def test_H8_short_same_agent_child_dispatch_unpaired_hidden() -> None:
    """H8 边界: 短同 agent 子任务 → do_bubble=False（不 bubble）→ TASK_DISPATCH 无对应
    TASK_DISPATCH_RESULT → agent_recall 按「未配对 dispatch 隐去」（agent_recall.py:118）。

    场景：parent 在 agent scope 有 TASK_DISPATCH（oc_short），
    短 child close 时 do_bubble=False，parent scope 无 TASK_DISPATCH_RESULT。
    断言 AgentRecallSource.fetch 不渲染孤立 TASK_DISPATCH（隐去，避免悬空 tool_call）。
    """
    from ctx_weft.core.assembler.assembler import AssemblerDeps, ContextRequest
    from ctx_weft.core.assembler.sources.agent_recall import AgentRecallSource

    mem = InMemoryMemoryProvider()

    parent_agent = "ag1"
    parent_id = "p1"
    child_id = "c_short"

    # parent 的 agent scope 有一个 TASK_DISPATCH（派发了短子任务）
    parent_agent_scope = _task_scope(parent_id, parent_agent)
    tc_short = "oc_short"
    await mem.ingest(_ev(
        T.TASK_DISPATCH, parent_agent_scope, "", 10,
        role="assistant", tool_call_id=tc_short, tool_name="control__delegate_task",
        arguments={"title": "短子任务", "description": "x"},
    ), _pctx())

    # 短 child：极短对话（确保 is_short=True）
    child_scope = _task_scope(child_id, parent_agent)
    await mem.ingest(_ev(T.USER_PROMPT, child_scope, "短子任务", 5, role="user"), _pctx())
    await mem.ingest(_ev(T.LLM_RESPONSE, child_scope, "ok", 6, role="assistant"), _pctx())

    child_task = _make_task(
        task_id=child_id, agent_id=parent_agent,
        creator_agent_id=parent_agent, parent_task_id=parent_id,
        prompt="短子任务", outputs="ok", title="短子任务",
    )
    child_task.origin_tool_call_id = tc_short
    # 短 child → finalize_task_memory → short=True → do_bubble=False

    await finalize_task_memory(
        mem, _state(child_task, child_scope),
        child_task, "ok\n\nProcess Report: 短任务", "success", _loop_ctx(mem),
    )

    # 断言：parent scope 无 TASK_DISPATCH_RESULT（短 child 不 bubble）
    parent_results = await mem.recall_recent(
        parent_agent_scope, [T.TASK_DISPATCH_RESULT], 200, _pctx(),
    )
    short_result = [r for r in parent_results if r.metadata.get("tool_call_id") == tc_short]
    assert not short_result, (
        f"short same-agent child must NOT bubble TASK_DISPATCH_RESULT; found {short_result}"
    )

    # 断言 AgentRecallSource.fetch 隐去未配对的 TASK_DISPATCH（不渲染孤立 dispatch）
    agent_scope = _agent_scope(parent_agent)
    deps = AssemblerDeps(memory=mem, knowledge_providers=[], provider_ctx=_pctx())
    req = ContextRequest(
        purpose="act",
        scope=agent_scope,
        task=None, agent=None, session=None, template=None,
        bound_capabilities=[],
    )
    blocks = [b async for b in AgentRecallSource().fetch(req, deps)]

    # 孤立 TASK_DISPATCH 的 tool_call_id 不应出现在任何渲染的 block
    dispatch_tcs_in_blocks = [
        tc
        for b in blocks
        for tc in b.metadata.get("tool_calls", [])
        if tc.get("id") == tc_short
    ]
    assert not dispatch_tcs_in_blocks, (
        f"unpaired TASK_DISPATCH (tool_call_id={tc_short}) must be hidden (not rendered); "
        f"found in blocks: {dispatch_tcs_in_blocks}"
    )
