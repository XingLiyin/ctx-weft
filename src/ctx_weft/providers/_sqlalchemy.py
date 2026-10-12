"""两个 SQL provider 包（`memory/sql` 与 `events/store/sql`）共用的 SQLAlchemy 基建。

私有模块，**不进 `providers/__init__.py`**——`providers/` 下的可选依赖边界靠「谁也不
eager import 它」维持：`import ctx_weft.providers` 在缺 sqlalchemy 时必须照常工作。
先例见 `providers/_encoding.py` / `_tooldecl.py` / `_script_runner.py`。
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import DateTime, TypeDecorator
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

__all__ = ["UtcDateTime", "make_session_factory"]


class UtcDateTime(TypeDecorator):
    """时区保真的 DateTime：入库转 UTC，出库补回 ``tzinfo=UTC``。

    为什么必须自己包一层：**SQLite 没有时区类型**。SQLAlchemy 的 SQLite 方言把
    aware datetime 的 tzinfo 直接丢掉、回读得到 naive datetime——于是
    ``rec.timestamp == 写入时的 aware datetime`` 恒为 False（naive 与 aware 不相等），
    而排序又照常工作，所以这个丢失**不会**在任何排序类断言上暴露，只会在等值比较上
    炸一条。postgres 侧 `TIMESTAMP WITH TIME ZONE` 本就保真，本装饰器在那边是恒等
    变换（bind 时值已是 UTC aware，result 时已带 tzinfo 不再补）。

    naive 输入按 UTC 解释（仓内事件与 memory 记录的时间戳统一来自 ``datetime.now(UTC)``）。
    """

    impl = DateTime(timezone=True)
    cache_ok = True

    def process_bind_param(self, value: datetime | None, dialect: object) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value.astimezone(UTC)

    def process_result_value(self, value: datetime | None, dialect: object) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value.astimezone(UTC)


def make_session_factory(url: str, **engine_kwargs: Any) -> Any:
    """``(engine, session_factory)``。url 例：``sqlite+aiosqlite:///path/app.db``。

    两个 SQL 包共用同一个 engine 是受支持的部署形态（单文件 SQLite）——各自的
    ``Base.metadata.create_all`` 跑两次即可，`Base` 刻意不共用（见各包 models 的注释）。
    """
    engine = create_async_engine(url, **engine_kwargs)
    return engine, async_sessionmaker(engine, expire_on_commit=False)
