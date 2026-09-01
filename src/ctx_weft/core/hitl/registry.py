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
    resume_state: dict[str, Any] | None = None   # 不透明，core 永不解读
    reply_as_result: bool = False
    #: 终局决定。**唯一的结局存储**——`resolved` 由它推导，不存第二份。
    decision: HitlDecision | None = None
    resolved_at: datetime | None = None
    slot: WaitSlot | None = None

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
        created_at: datetime,
    ) -> PendingHitl:
        """登记一个请求。同 `tool_call_id` 已有记录 → **复用**，不新建（幂等，spec §10）。

        空 `tool_call_id` 不作幂等键——`UserTurn` 的冷 park 本就没有 tool_call。
        """
        existing = self.find_for_tool_call(tool_call_id)
        if existing is not None:
            return existing
        req = PendingHitl(
            id=hitl_id, form=ask.form, session_id=session_id, task_id=task_id,
            agent_id=agent_id, delivery=ask.delivery, created_at=created_at,
            subject_id=ask.subject_id, prompt=ask.prompt, detail=ask.detail,
            fields=list(ask.fields), proposal=ask.proposal, tool_call_id=tool_call_id,
            resume_state=ask.resume_state, reply_as_result=ask.reply_as_result,
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
        for tool_call_id, (decision, resume_state) in snapshot.decisions_for.items():
            live = self.find_for_tool_call(tool_call_id)
            if live is not None:
                continue                       # 活 pending 或已装填的决定，均不覆盖
            placeholder = PendingHitl(
                id=f"loaded:{tool_call_id}", form="", session_id="", task_id="",
                agent_id="", delivery=NoResumeDelivery(), created_at=_EPOCH,
                tool_call_id=tool_call_id, resume_state=resume_state,
                decision=decision,
            )
            self._requests[placeholder.id] = placeholder
        return len(snapshot.pending)

    # ── 读 ────────────────────────────────────────────────────────────────────

    def get(self, hitl_id: str) -> PendingHitl | None:
        return self._requests.get(hitl_id)

    def find_for_tool_call(self, tool_call_id: str) -> PendingHitl | None:
        """按 tool_call_id 取最近一条记录；空 id → None。"""
        if not tool_call_id:
            return None
        matches = [r for r in self._requests.values() if r.tool_call_id == tool_call_id]
        if not matches:
            return None
        return max(matches, key=lambda r: r.created_at)

    def decision_for(self, tool_call_id: str) -> tuple[HitlDecision, dict[str, Any] | None] | None:
        """决定缓存查询：`(decision, resume_state)` 成对返回。

        成对是硬要求——冷路径重入调的是 `resume(ask_id, decision, resume_state, ctx)`，
        丢掉 `resume_state` 就等于要求 provider 重做让出前的工作（spec §7.2）。

        仍 pending（活的等待）→ None：不得把它当成「已答过」。
        """
        req = self.find_for_tool_call(tool_call_id)
        if req is None or req.decision is None:
            return None
        return req.decision, req.resume_state

    def list_pending(self, session_id: str | None = None) -> list[PendingHitl]:
        return [
            r for r in self._requests.values()
            if not r.resolved and (session_id is None or r.session_id == session_id)
        ]
