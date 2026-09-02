"""HitlRegistry：HITL 的纯内存状态机。

**全同步、无 await。** 单线程 asyncio 下，一段没有 await 的代码原子执行，因此
「状态转移 + 取走等待槽」天然互斥，不需要锁——热投递与冷续跑的单一权威转移由
此保证（spec §6）。所有 I/O（发事实、外部化内容）归 `HitlService`。

**完备即构造**：本类的一切查询只读自己内存，绝不回落去查存储。恢复期的完备性
由装填（`load_snapshot`，Task 6）承担（spec §3.1）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Protocol

from ctx_weft.protocols.hitl import (
    Delivery,
    HitlAsk,
    HitlDecision,
    HitlRequestView,
    NoResumeDelivery,
)

if TYPE_CHECKING:
    from ctx_weft.core.hitl.snapshot import HitlSnapshot

#: 装填决定时的占位创建时间——占位项只为回答 `decision_for`，永不出现在 pending 列表里，
#: 故取最小值即可（GC 排序用 resolved_at，装填项无 resolved_at 时回落到它）。
_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)

#: 决定缓存键的第三维——**core 内部键，不进 protocols**：host 不需要知道「谁问的」，
#: 只有 gateway（授权步）与工具 provider（工具步）自己需要区分（Task 4.5）。
HITL_STAGE_AUTHZ = "authz"
HITL_STAGE_TOOL = "tool"


class WaitSlot(Protocol):
    """热等待的会合槽——**只有一个操作**的不透明句柄。

    刻意保持最小：registry 因此只接触一个并发原语，不接触任何 loop 类型，箭头
    仍然朝下（spec §3.2）。一旦往槽里塞更丰富的 loop 对象，反向依赖就回来了。

    返回 True = 已被热投递消费（`claimed`）；False = 投递未被接受（等待方已放弃）。
    """

    def deliver(self, decision: HitlDecision) -> bool: ...


@dataclass
class PendingHitl:
    """core 内部的活记录。**不出 core**——对外只经 `to_view()` 投影。"""

    id: str
    form: str
    session_id: str
    task_id: str
    agent_id: str
    delivery: Delivery
    created_at: datetime
    subject_id: str = ""
    prompt: str = ""
    detail: str = ""
    fields: list[dict[str, Any]] = field(default_factory=list)
    proposal: dict[str, Any] | None = None
    tool_call_id: str = ""                       # 幂等键 + 决定缓存键
    #: 决定缓存键的第三维（连同 session_id、tool_call_id）：`HITL_STAGE_AUTHZ` /
    #: `HITL_STAGE_TOOL`。**只按 tool_call_id 查会让授权步吃掉工具阶段的答复**，见
    #: `find_for_tool_call` docstring。core 内部键，不出 `to_view()`。
    stage: str = ""
    resume_state: dict[str, Any] | None = None   # 不透明，core 永不解读
    reply_as_result: bool = False
    #: 终局决定。**唯一的结局存储**——`resolved` 由它推导，不存第二份。
    decision: HitlDecision | None = None
    resolved_at: datetime | None = None
    slot: WaitSlot | None = None
    #: 本次终局是否被一个活等待槽热消费（`HitlService._commit` 在取槽的同一原子段里
    #: 判定并写入）。默认 `False`——快照装填出来的项永远是冷的（重启后一切皆冷），
    #: 这正是我们要的默认值，不需要装填路径另外清它。调用方（`reply_to_hitl`）据此
    #: 决定要不要触发冷续跑：已被热消费的不得再触发一次，否则同一次应答驱动两跑。
    claimed: bool = False

    @property
    def resolved(self) -> bool:
        return self.decision is not None

    def to_view(self) -> HitlRequestView:
        return HitlRequestView(
            id=self.id, form=self.form, session_id=self.session_id, task_id=self.task_id,
            created_at=self.created_at, agent_id=self.agent_id, subject_id=self.subject_id,
            prompt=self.prompt, detail=self.detail, fields=list(self.fields),
            proposal=self.proposal,
            outcome=self.decision.outcome if self.decision else "",
            resolved_at=self.resolved_at,
        )


class HitlRegistry:
    """登记 / 幂等 / 决定缓存 / 等待槽 / GC。"""

    def __init__(self, max_resolved: int = 1000) -> None:
        #: 已终局项的保留上限：决定缓存只需近期的，超限裁剪最旧者，防止长跑进程无界增长。
        #: pending 永不裁剪。
        self._max_resolved = max_resolved
        self._requests: dict[str, PendingHitl] = {}

    # ── 写 ────────────────────────────────────────────────────────────────────

    def open(
        self,
        ask: HitlAsk,
        *,
        hitl_id: str,
        session_id: str,
        task_id: str,
        agent_id: str = "",
        tool_call_id: str = "",
        stage: str,
        created_at: datetime,
    ) -> PendingHitl:
        """登记一个请求。同 `(session_id, tool_call_id, stage)` 已有记录 → **复用**，不新建
        （幂等，spec §10）。

        空 `tool_call_id` 不作幂等键——`UserTurn` 的冷 park 本就没有 tool_call。
        """
        existing = self.find_for_tool_call(session_id, tool_call_id, stage)
        if existing is not None:
            return existing
        req = PendingHitl(
            id=hitl_id, form=ask.form, session_id=session_id, task_id=task_id,
            agent_id=agent_id, delivery=ask.delivery, created_at=created_at,
            subject_id=ask.subject_id, prompt=ask.prompt, detail=ask.detail,
            fields=list(ask.fields), proposal=ask.proposal, tool_call_id=tool_call_id,
            stage=stage, resume_state=ask.resume_state, reply_as_result=ask.reply_as_result,
        )
        self._requests[hitl_id] = req
        return req

    def attach_slot(self, hitl_id: str, slot: WaitSlot) -> None:
        req = self._requests.get(hitl_id)
        if req is not None:
            req.slot = slot

    def detach_slot(self, hitl_id: str) -> None:
        """驱逐等待槽（热→冷降级）。**驱逐本身永不触发续跑**，唯有应答才触发。"""
        req = self._requests.get(hitl_id)
        if req is not None:
            req.slot = None

    def resolve(
        self, hitl_id: str, decision: HitlDecision, resolved_at: datetime,
    ) -> tuple[PendingHitl, WaitSlot | None] | None:
        """终局转移 + **原子地**取走等待槽。

        返回 `(req, slot)`；已终局或未知 id → `None`（调用方据此保持幂等：不重发事实）。
        整段无 await，故转移与取槽不可能被别的协程插入。
        """
        req = self._requests.get(hitl_id)
        if req is None or req.resolved:
            return None
        req.decision = decision
        req.resolved_at = resolved_at
        slot, req.slot = req.slot, None
        return req, slot

    def gc(self) -> None:
        """裁剪已终局项，pending 永不裁剪。"""
        resolved = [r for r in self._requests.values() if r.resolved]
        if len(resolved) <= self._max_resolved:
            return
        resolved.sort(key=lambda r: r.resolved_at or r.created_at)
        for r in resolved[: len(resolved) - self._max_resolved]:
            self._requests.pop(r.id, None)

    def load_snapshot(self, snapshot: HitlSnapshot) -> int:
        """把折叠结果装填进内存，返回 pending 条数。

        **完备即构造**：装填之后 core 的一切查询只读内存，绝不回落去 scan 事件日志。
        装填的完备性因此是恢复路径的责任（spec §3.1）。

        两条规则：
        - 装填出来的 pending **不带等待槽**——重启后一切皆冷（spec §10）。
        - 已有的**活 pending 优先**：日志里的旧决定不得盖掉一个正在等人的请求，
          否则会把活请求判成「已答过」而跳过。

        **调用方在传入 `snapshot` 之前须先处理 event 侧 blob ref**：
        `snapshot.decisions_for[*][0].message` 仍是事件 blob store 命名空间下的引用，
        见 `HitlSnapshot` docstring 与 `fold_hitl_snapshot` docstring（spec §12.3.3，
        参照 `runtime.py:1943` 的 hydrate + normalize 实现）——本方法不做这一步。
        """
        for hitl_id, req in snapshot.pending.items():
            req.slot = None
            self._requests.setdefault(hitl_id, req)
        for (session_id, tool_call_id, stage), (decision, resume_state) in (
            snapshot.decisions_for.items()
        ):
            live = self.find_for_tool_call(session_id, tool_call_id, stage)
            if live is not None:
                continue                       # 活 pending 或已装填的决定，均不覆盖
            # 折叠若同时给出了那条**已终局请求本身**（`snapshot.resolved`），就装它——
            # 它带着 `task_id` / `delivery` / `form` / `created_at`，`resolved_for_session`
            # 的重排兜底靠的正是 `task_id`（Task 9）。没给（手工构造的快照）则退回只带
            # 决定的占位项，与本分支引入之前逐字节一致。
            meta = snapshot.resolved.get((session_id, tool_call_id, stage))
            if meta is not None:
                meta.slot = None
                meta.decision = decision
                meta.resume_state = resume_state
                if meta.resolved_at is None:
                    meta.resolved_at = _EPOCH
                self._requests.setdefault(meta.id, meta)
                continue
            placeholder = PendingHitl(
                id=f"loaded:{session_id}:{stage}:{tool_call_id}", form="",
                session_id=session_id, task_id="",
                agent_id="", delivery=NoResumeDelivery(), created_at=_EPOCH,
                tool_call_id=tool_call_id, stage=stage, resume_state=resume_state,
                decision=decision,
            )
            self._requests[placeholder.id] = placeholder
        return len(snapshot.pending)

    # ── 读 ────────────────────────────────────────────────────────────────────

    def get(self, hitl_id: str) -> PendingHitl | None:
        return self._requests.get(hitl_id)

    def find_for_tool_call(
        self, session_id: str, tool_call_id: str, stage: str,
    ) -> PendingHitl | None:
        """按 (session, tool_call, stage) 取最近一条；空 tool_call_id → None。

        **三维缺一不可**：只按 tool_call_id 查会让 A 会话的批准替 B 会话里同名 id 的调用
        开门（LLM 的 tool_call id 常是 `call_1` 这类短值——跨会话授权绕过），也会让授权步
        吃掉工具阶段的答复、把它的 modified_arguments 当成工具参数送进 provider（跨阶段
        混淆，Task 4.5）。
        """
        if not tool_call_id:
            return None
        matches = [r for r in self._requests.values()
                   if r.tool_call_id == tool_call_id
                   and r.session_id == session_id
                   and r.stage == stage]
        return max(matches, key=lambda r: r.created_at) if matches else None

    def decision_for(
        self, session_id: str, tool_call_id: str, stage: str,
    ) -> tuple[HitlDecision, dict[str, Any] | None] | None:
        """决定缓存查询：`(decision, resume_state)` 成对返回。

        成对是硬要求——冷路径重入调的是 `resume(ask_id, decision, resume_state, ctx)`，
        丢掉 `resume_state` 就等于要求 provider 重做让出前的工作（spec §7.2）。

        仍 pending（活的等待）→ None：不得把它当成「已答过」。
        """
        req = self.find_for_tool_call(session_id, tool_call_id, stage)
        if req is None or req.decision is None:
            return None
        return req.decision, req.resume_state

    def list_pending(self, session_id: str | None = None) -> list[PendingHitl]:
        return [
            r for r in self._requests.values()
            if not r.resolved and (session_id is None or r.session_id == session_id)
        ]

    def resolved_for_session(self, session_id: str) -> list[PendingHitl]:
        """该 session **已终局**的请求。`list_pending` 的镜像：纯内存、同步、不查存储。

        用途只有一个——恢复期的崩溃窗口兜底（Task 9）：决定已落盘、但进程在续跑之前
        就死了，此时 HITL 已终局而 task 仍 SUSPENDED。不把这些 task 重排，人已经答过
        的会话就永远停在挂起态，症状与「事件被丢」一模一样。

        **口径**：返回的集合与 `load_snapshot` 装填的已终局集合完全一致——装填漏掉的
        请求这里就看不见，兜底也随之失效。这就是「装填的完备性是恢复路径的责任」
        （spec §3.1）在这里第二次成为承重点。
        """
        return [
            r for r in self._requests.values()
            if r.resolved and r.session_id == session_id
        ]
