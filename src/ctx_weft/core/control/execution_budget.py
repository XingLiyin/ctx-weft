"""执行限制与预算计量（spec: execution-limits；change reliability-wp7，方案 §6）。

设计要点：

- **opt-in**：``ExecutionLimits`` 五字段默认全 None/5.0 —— 不配置 = 不新增任何限制
  （既有活限制——act/observe 轮数、context_limit、LLM 自愈预算——独立保留）。
- **计量语义**（方案原文固定）：active time = 实际墙钟 − parked 累计（等 HITL / 等
  子任务不计）；actor turns 按逻辑 LLM 请求计（网络自愈不重计）；monotonic clock；
  跨自动 retry 累计；持久化**已消费量**（恢复续用剩余预算）。
- **诚实边界**：检查点在阶段转换与 ≤1s 周期——崩溃漏记不超过声明周期，**这不是
  严格计费上限**（计费级精度归宿主另接账本）。

历史背景：三个旧字段（``max_turns_per_agent`` / ``timeout_per_step_sec`` /
``Task.timeout_ms``）从未被执行——本模块是它们「配置了限制 = 限制会生效」的
接替者；旧字段只发弃用警告、不激活（loader / SessionRegistry 双发点）。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Callable

from ctx_weft.core.models.discriminators import TaskErrorCode

__all__ = [
    "ExecutionLimits",
    "ExecutionBudget",
    "ExecutionLimitExceeded",
]


@dataclass(frozen=True)
class ExecutionLimits:
    """opt-in 执行限制（spec: execution-limits）。默认全 None = 零行为变化。

    - ``step_active_timeout_sec``：单 step 执行期间活动时间（嵌套 HITL 等待暂停计时）
    - ``task_active_timeout_sec``：task 装配→本次 run 停止的实际墙钟，跨自动 retry 累计
    - ``provider_timeout_sec``：每次 provider 方法/流的活动时间上限（progress 不续命）
    - ``max_actor_turns_per_task``：actor 逻辑 LLM 请求轮数（自愈不重计、retry 累计）
    - ``cleanup_grace_sec``：协作取消后的收尾宽限；超时仍运行 → 标记未终止 + 拒绝
      同会话后续副作用（不假装 asyncio 能终止不合作的 Python 代码）
    """

    step_active_timeout_sec: float | None = None
    task_active_timeout_sec: float | None = None
    provider_timeout_sec: float | None = None
    max_actor_turns_per_task: int | None = None
    cleanup_grace_sec: float = 5.0

    @staticmethod
    def none() -> "ExecutionLimits":
        """显式的「不限制」形态（与字段默认一致，便于宿主清空）。"""
        return ExecutionLimits()

    @property
    def is_unlimited(self) -> bool:
        return (self.step_active_timeout_sec is None
                and self.task_active_timeout_sec is None
                and self.provider_timeout_sec is None
                and self.max_actor_turns_per_task is None)


class ExecutionLimitExceeded(Exception):
    """预算命中（spec: execution-limits）。retriable=False——超限不自愈。"""

    def __init__(self, code: TaskErrorCode, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.retriable = False


#: checkpoint 周期上限（秒）：崩溃漏记的声明边界（方案 §6「不能称为严格计费上限」）
CHECKPOINT_PERIOD_SEC = 1.0


@dataclass
class ExecutionBudget:
    """per-task 预算计量器（mutable；monotonic 可注入便于测试）。

    生命周期：TaskManager 派发时新建（或从 task 投影的 ``budget_consumed`` 恢复）；
    park/resume 在 HITL 与等子任务的挂点调用；check 在 Step 边界与 act 轮边界；
    consumed 经 ``snapshot()`` 写回 task 内存字段并随下一次 TASK_* 事件投影。
    """

    limits: ExecutionLimits
    #: 恢复续算：上次持久化的已消费量（不从零重计——spec 场景「重启恢复续预算」）
    persisted_active_sec: float = 0.0
    persisted_turns: int = 0
    monotonic: Callable[[], float] = time.monotonic   # 直接默认（函数本体，非 factory 调用结果）

    def __post_init__(self) -> None:
        self._run_started: float | None = None
        self._parked_accum: float = 0.0
        self._park_started: float | None = None
        self._turns: int = 0
        #: 预占：派发 actor 请求前先记账（崩溃恢复不超支——方案 §6 原文）
        self._reserved_turn = False

    # ── 生命周期 ────────────────────────────────────────────────────────────────

    def run_started(self) -> None:
        """本次 run 开始计时（重复调用以最后一次为准——resume 重入）。"""
        self._run_started = self.monotonic()

    def park(self) -> None:
        """进入不计时段（HITL 等待 / SUSPENDED 等子任务）。幂等。"""
        if self._park_started is None:
            self._park_started = self.monotonic()

    def resume(self) -> None:
        """离开不计时段。未 park 时 no-op。"""
        if self._park_started is not None:
            self._parked_accum += self.monotonic() - self._park_started
            self._park_started = None

    # ── 计量 ────────────────────────────────────────────────────────────────────

    @property
    def current_run_active_sec(self) -> float:
        """本 run 净活动时长（未开始 → 0；parked 期间冻结在停表值）。"""
        if self._run_started is None:
            return 0.0
        now = self._park_started if self._park_started is not None else self.monotonic()
        return max(0.0, now - self._run_started - self._parked_accum)

    @property
    def total_active_sec(self) -> float:
        """跨 retry 累计的总活动时长（持久化已消费 + 本 run 净活动）。"""
        return self.persisted_active_sec + self.current_run_active_sec

    @property
    def total_turns(self) -> int:
        return self.persisted_turns + self._turns

    def consume_turn(self) -> None:
        """一次逻辑 actor LLM 请求（网络自愈在 stream_llm_resilient 内部，不经过这里）。
        预占在身 → 兑现（不再 +1）；否则计一次。"""
        if self._reserved_turn:
            self._reserved_turn = False
        else:
            self._turns += 1

    def reserve_turn(self) -> None:
        """派发 actor 请求前预占一轮（崩溃恢复不超支）。consume_turn 兑现预占。"""
        if not self._reserved_turn:
            self._turns += 1
            self._reserved_turn = True

    # ── 检查与持久化 ────────────────────────────────────────────────────────────

    def check(self) -> None:
        """检查点：命中任一限制抛 ExecutionLimitExceeded（首超限项）。"""
        lim = self.limits
        if lim.task_active_timeout_sec is not None:
            if self.total_active_sec > lim.task_active_timeout_sec:
                raise ExecutionLimitExceeded(
                    TaskErrorCode.TASK_DEADLINE_EXCEEDED,
                    f"task active time {self.total_active_sec:.1f}s exceeded "
                    f"{lim.task_active_timeout_sec}s (waiting time excluded)")
        if lim.max_actor_turns_per_task is not None:
            if self.total_turns > lim.max_actor_turns_per_task:
                raise ExecutionLimitExceeded(
                    TaskErrorCode.ACTOR_TURN_LIMIT,
                    f"actor turns {self.total_turns} exceeded "
                    f"{lim.max_actor_turns_per_task} (logical LLM requests)")

    def check_step(self, step_active_sec: float) -> None:
        """step 级 deadline：调用方传入本 step 已活动秒数（parked 已由 park/resume 排除）。"""
        lim = self.limits.step_active_timeout_sec
        if lim is not None and step_active_sec > lim:
            raise ExecutionLimitExceeded(
                TaskErrorCode.STEP_DEADLINE_EXCEEDED,
                f"step active time {step_active_sec:.1f}s exceeded {lim}s")

    def snapshot(self) -> dict:
        """已消费量（持久化形态）：随 task 内存字段 + 下一次 TASK_* 事件投影。"""
        return {"active_sec": round(self.total_active_sec, 3),
                "turns": self.total_turns}

    @classmethod
    def restore(cls, limits: ExecutionLimits, consumed: dict | None,
                monotonic: Callable[[], float] = time.monotonic) -> "ExecutionBudget":
        """从投影恢复（spec 场景：进程重启续用剩余预算；漏记 ≤ checkpoint 周期）。"""
        consumed = consumed or {}
        return cls(limits=limits,
                  persisted_active_sec=float(consumed.get("active_sec", 0.0)),
                  persisted_turns=int(consumed.get("turns", 0)),
                  monotonic=monotonic)
