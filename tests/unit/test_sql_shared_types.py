"""providers/_sqlalchemy.py：两个 SQL 包共用的类型与工厂。

这里只钉两件事：UtcDateTime 的时区保真（SQLite 会丢 tzinfo），以及共享模块不被
上层包 eager import（sqlalchemy 是可选依赖）。
"""

from __future__ import annotations

import importlib
from datetime import UTC, datetime, timedelta, timezone

import pytest

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
