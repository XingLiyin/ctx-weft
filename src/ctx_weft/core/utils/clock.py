"""UTC 时钟。

单独成模块的理由与 `ids.py` 相同：它是全仓入度最高的那几个符号之一（十余个消费者，
横跨 core 每个包，`providers/` 也在引），下放进任何一个包都会逼其余的反向引它。
"""

from __future__ import annotations

from datetime import UTC, datetime

__all__ = ["as_utc", "now_utc"]


def now_utc() -> datetime:
    """UTC current time."""
    return datetime.now(UTC)


def as_utc(dt: datetime) -> datetime:
    """把可能 naive 的 datetime 归一为 aware(UTC)——事件重放 / DB 反序列化可能丢 tz
    （Postgres timestamp-without-tz、裸 isoformat 等），naive 与 aware 直接比较会抛
    TypeError。统一在此把无 tz 者按 UTC 补齐，供跨来源 datetime 排序 / 比较前调用。"""
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=UTC)
