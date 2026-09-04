"""顶层 util：时间、id、事件封套，以及一条跨层的契约字符串。

**为什么这三样在一起**：`new_event` 就是用 `generate_id` + `now_utc` 造出来的，
三者本就同源。改造前 id/时间住在 `core/utils.py`（一个 389 行的杂货铺），事件封套
住在 `core/event_envelope.py`，而后者唯一的依赖就是前者。

`core/utils.py` 的其余部分已按消费者归位：
    token 估算                 -> `core/estimate.py`
    内容渲染与图片计量          -> `core/content.py`
    JSON Schema 提取           -> `core/capabilities/schema.py`
    `PROGRESS_SO_FAR_HEADING`  -> 内联进唯一消费者 `assembler/sources/_history.py`

**为什么这几样没能下放**：`generate_id` / `now_utc` 有十余个消费者，横跨 core 的
每个包并且 `providers/` 也在引。放进任何一个包都会逼其余的反向引它。
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

from ulid import ULID

from ctx_weft.protocols.events import EVENT_TYPES, Event

if TYPE_CHECKING:
    from ctx_weft.protocols.events import EventBus

__all__ = [
    "SUBTASKS_REVIEW_HEADING",
    "as_utc",
    "emit_event",
    "generate_id",
    "new_event",
    "now_utc",
]


# observe prompt 里「可 review 子任务清单」段的标题前缀：composer 渲染、
# report_task_outcome 的 task_reviews schema 引用（跨层字符串契约，勿散写字面量）。
#
# **住在顶层 util 而不是 assembler**：它的两个消费者是 `assembler/composer.py` 与
# `capabilities/control_tools.py`，而 `assembler -> capabilities` 已存在（composer 引
# 控制工具的限定名），放进 assembler 会让 capabilities 反向引它、成环。
SUBTASKS_REVIEW_HEADING = "## Your sub-tasks"


def now_utc() -> datetime:
    """UTC current time."""
    return datetime.now(UTC)


def as_utc(dt: datetime) -> datetime:
    """把可能 naive 的 datetime 归一为 aware(UTC)——事件重放 / DB 反序列化可能丢 tz
    （Postgres timestamp-without-tz、裸 isoformat 等），naive 与 aware 直接比较会抛
    TypeError。统一在此把无 tz 者按 UTC 补齐，供跨来源 datetime 排序 / 比较前调用。"""
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=UTC)


def generate_id(prefix: str) -> str:
    """Generate a ULID-based primary key (time-sortable + globally unique).

    Format: {prefix}_{ulid}, e.g. ses_01H8K9XPYJ7DRT2RY3JFXSF7M2
    """
    return f"{prefix}_{ULID()}"


# CJK 表意字 / 假名 / 谚文 / 全角标点等：这些脚本 len//4 会严重低估（真实约 0.6~1 token/字），
# 单列出来按更保守的每字 1.5 token 估。ASCII 起始都 < 0x3000，findall 走 C 级、对大文本仍快。


def new_event(
    event_type: str,
    *,
    session_id: str,
    tenant_id: str,
    origin: str,
    run_id: str | None = None,
    sequence: int = 0,
    task_id: str | None = None,
    agent_id: str | None = None,
    payload: dict | None = None,
    metadata: dict | None = None,
    timestamp: "datetime | None" = None,
    causation_id: str | None = None,
) -> Event:
    """构造一个 Event，自动分配 id 与时间戳。

    `run_id` / `sequence` 的默认值就是「无 run 语境」的形态——那是 session / agent /
    task 级事实的**定义特征**，不是随手挑的缺省。run 级调用方（`make_event`）显式传。

    未知事件类型 → `ValueError`：V1 严格白名单（设计文档 §14.3）。这是**唯一**一份
    校验；直接构造 `Event` 的路径此前会绕过它，故两个 `_emit` 各补了一份。
    """
    if event_type not in EVENT_TYPES:
        raise ValueError(f"Unknown event type: {event_type}; not in EVENT_TYPES")
    return Event(
        id=generate_id("evt"),
        run_id=run_id,
        sequence=sequence,
        session_id=session_id,
        type=event_type,
        timestamp=timestamp or now_utc(),
        tenant_id=tenant_id,
        task_id=task_id,
        agent_id=agent_id,
        origin=origin,
        payload=payload or {},
        metadata=metadata or {},
        causation_id=causation_id,
    )


async def emit_event(bus: "EventBus | None", event_type: str, **kwargs) -> None:
    """构造并发出。``bus`` 为 None → no-op。

    None-tolerant 是 `TaskManager._emit` 的既有语义（TM 允许在没有总线的情况下被
    构造，大量单测依赖这一点），在这里统一承担。校验先于发射：坏类型不会先落一半。
    """
    ev = new_event(event_type, **kwargs)
    if bus is None:
        return
    await bus.emit(ev)
