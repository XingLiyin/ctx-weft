"""providers/_sqlalchemy.py：两个 SQL 包共用的类型与工厂。

这里只钉两件事：UtcDateTime 的时区保真（SQLite 会丢 tzinfo），以及共享模块不被
上层包 eager import（sqlalchemy 是可选依赖）。
"""

from __future__ import annotations

import importlib
from datetime import UTC, datetime, timedelta, timezone

import pytest

from ctx_weft.protocols import load_records
from ctx_weft.providers._sqlalchemy import UtcDateTime, make_session_factory


def test_utc_datetime_bind_converts_aware_to_utc():
    td = UtcDateTime()
    shanghai = timezone(timedelta(hours=8))
    got = td.process_bind_param(datetime(2026, 8, 29, 20, 0, tzinfo=shanghai), None)
    assert got == datetime(2026, 8, 29, 12, 0, tzinfo=UTC)


def test_utc_datetime_bind_treats_naive_as_utc():
    td = UtcDateTime()
    got = td.process_bind_param(datetime(2026, 8, 29, 12, 0), None)
    assert got == datetime(2026, 8, 29, 12, 0, tzinfo=UTC)


def test_utc_datetime_result_restores_tzinfo():
    """SQLite 回读得到 naive datetime——不补 tzinfo 的话等值比较恒 False，
    而排序照常工作，所以这个丢失只会在等值断言上炸一条。"""
    td = UtcDateTime()
    got = td.process_result_value(datetime(2026, 8, 29, 12, 0), None)
    assert got == datetime(2026, 8, 29, 12, 0, tzinfo=UTC)


def test_utc_datetime_handles_none():
    td = UtcDateTime()
    assert td.process_bind_param(None, None) is None
    assert td.process_result_value(None, None) is None


def test_make_session_factory_returns_engine_and_factory(tmp_path):
    engine, factory = make_session_factory(f"sqlite+aiosqlite:///{tmp_path / 'x.db'}")
    assert engine is not None
    assert callable(factory)


def test_shared_module_not_eager_imported_by_providers():
    """providers/__init__.py 不得拉 sqlalchemy——缺可选依赖的宿主要能 import providers。"""
    mod = importlib.import_module("ctx_weft.providers")
    assert not hasattr(mod, "UtcDateTime")
    assert not hasattr(mod, "make_session_factory")


# ── tenant 列必须装得下宿主定义的租户 id（2026-10-08）─────────────────────────
#
# `tenant` 是**宿主定义的不透明字符串**，core 不解释它（`ProviderContext.tenant_id` 只写
# 「多租户预留」）。实际宿主（NetliveCoworkPy）用的是 `"{surrogate_id}:{cowork_id}"`，两段
# 拼起来远超 64。
#
# **这个坑只能靠模型声明来测**：装不下时 Postgres 抛 `value too long for character
# varying(64)`，而 SQLite 不认长度、静默存进去——于是行为测试在 SQLite 上永远绿，而生产
# 的 Postgres 必炸。所以这里断言的是列的声明，不是写入行为。

#: 与宿主 `memory_events.tenant` 对齐的约定值。
#:
#: ⚠ 两段的**理论**上限其实是 257（宿主 SessionModel 里 surrogate_id 与 cowork_id 各
#: `String(128)`，加一个分隔符），所以 256 差 1 —— 宿主那边也是 256，同样差这 1。实际
#: id 远短于 128，这个裕量从没被触碰过；记在这里是为了别把 256 当成算出来的数，它是约定。
#: 真要较真就两边一起放宽，而宿主建表时用的是它自己那份，core 单方面放宽买不到东西。
_HOST_TENANT_MIN = 256


def _column_length(model, column: str) -> int:
    return model.__table__.columns[column].type.length


def test_memory_tenant_columns_fit_a_two_segment_host_tenant():
    from ctx_weft.providers.memory.sql.models import (
        MemoryEventModel, MemorySubscriptionModel,
    )

    for model in (MemoryEventModel, MemorySubscriptionModel):
        got = _column_length(model, "tenant")
        assert got >= _HOST_TENANT_MIN, (
            f"{model.__tablename__}.tenant 只有 {got} 字符，装不下宿主的 "
            f"'{{surrogate}}:{{cowork}}'（需 ≥ {_HOST_TENANT_MIN}）。"
            "Postgres 会在写入时直接抛 value too long，SQLite 静默存进去 —— "
            "同一份代码本地全绿、上生产必炸"
        )


def test_event_tenant_column_fits_a_two_segment_host_tenant():
    from ctx_weft.providers.events.store.sql.models import EventModel

    got = _column_length(EventModel, "tenant_id")
    assert got >= _HOST_TENANT_MIN, (
        f"events.tenant_id 只有 {got} 字符 —— 同 memory 那两列的理由"
    )


@pytest.mark.asyncio
async def test_a_long_tenant_survives_a_round_trip(tmp_path):
    """端到端兜一道：长 tenant 写得进、读得回，且隔离仍然成立。

    在 SQLite 上它测不出**列宽**（不认长度，那由上面两条声明断言守），但它能挡住「长
    tenant 在读写链路上被截断/归一掉」这类与列宽无关的失误——比较一律走
    `COALESCE(tenant,'default')`，任何一侧少截几个字符都会让隔离静默失效。
    """
    from datetime import UTC, datetime

    from ctx_weft.protocols.context import ProviderContext
    from ctx_weft.protocols.memory import (
        MemoryAddress, MemoryEvent, MemoryKind, MemoryScope,
    )
    from ctx_weft.providers.memory.sql.provider import open_sqlite_memory

    tenant = f"{'s' * 120}:{'c' * 120}"          # 241 字符，远超 64、仍在 256 内
    addr = MemoryAddress(session_id="ses_1", task_id="tsk_1", agent_id="agt_1")
    ctx = ProviderContext(session_id="ses_1", tenant_id=tenant)
    event = MemoryEvent(
        kind=MemoryKind.CONVERSATION_TURN, scope=MemoryScope.TASK, address=addr,
        content="hi", timestamp=datetime(2026, 10, 8, tzinfo=UTC), role="user",
    )

    # 它接的是**路径**，URL 由它自己拼（同 tests/integration/_crash_worker.py 的用法）
    async with open_sqlite_memory(tmp_path / "m.db") as provider:
        await provider.ingest(event, ctx)

        got = await load_records(provider, addr, MemoryScope.TASK, ctx)
        assert [r.content for r in got] == ["hi"], "长 tenant 的行必须读得回来"

        # 换一个租户读同一个地址 → 读不到（隔离没因为长度而失效）
        other = ProviderContext(session_id="ses_1", tenant_id="someone-else")
        assert await load_records(provider, addr, MemoryScope.TASK, other) == []

        # 只差最后一个字符的 tenant 同样读不到（截断类失误的判据）
        almost = ProviderContext(session_id="ses_1", tenant_id=tenant[:-1])
        assert await load_records(provider, addr, MemoryScope.TASK, almost) == []
