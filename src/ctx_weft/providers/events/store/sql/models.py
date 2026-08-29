"""SQLAlchemy 表模型：``events`` / ``event_snapshots``。

**`Base` 与 `memory/sql` 的刻意不共用。** 共用会让只想建 events 表的宿主被迫连 memory
表一起建，而两个包的可选依赖边界本就是分开的。单文件 SQLite 部署照样可以共享同一个
engine——各自跑一次 ``Base.metadata.create_all`` 即可。

表名与列名与参考宿主
（``IpMasterCoworkPy/src/ipmastercowork/persistence/postgres/models.py``）保持一致，
但**补了两列**（那边漏了、导致往返有损）：

- ``events.schema_version`` —— `Event.schema_version` 是给 reducer 分支用的。不存的话
  第一次 bump 版本时会静默把新事件读成旧版本。
- ``event_snapshots.run_id`` —— 参考实现的 ``load_latest_snapshot`` 硬编码 ``run_id=""``。
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import Index, Integer, String, Text, func
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from ctx_weft.providers._sqlalchemy import UtcDateTime


class Base(DeclarativeBase):
    """本包自带的 declarative base（宿主可把这两张表映射进自己的 Base）。"""


class EventModel(Base):
    __tablename__ = "events"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    run_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    session_id: Mapped[str] = mapped_column(String(64), index=True)
    task_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    agent_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    tenant_id: Mapped[str] = mapped_column(String(64), default="default")
    type: Mapped[str] = mapped_column(String(128), index=True)
    sequence: Mapped[int] = mapped_column(Integer)
    payload_json: Mapped[str] = mapped_column(Text, default="{}")
    metadata_json: Mapped[str] = mapped_column(Text, default="{}")
    causation_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # 存量行（参考宿主写的）没有这一列 → 读侧给默认值 1，零迁移。
    schema_version: Mapped[int] = mapped_column(Integer, default=1)
    timestamp: Mapped[datetime] = mapped_column(UtcDateTime, server_default=func.now())

    __table_args__ = (
        # list_active_session_ids 按 (session_id, type) 收窄，再按 id 升序重放。
        Index("ix_events_session_type", "session_id", "type"),
    )


class SnapshotModel(Base):
    __tablename__ = "event_snapshots"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    session_id: Mapped[str] = mapped_column(String(64), index=True)
    run_id: Mapped[str] = mapped_column(String(64), default="")
    last_event_id: Mapped[str] = mapped_column(String(64))
    last_event_sequence: Mapped[int] = mapped_column(Integer)
    state_blob_json: Mapped[str] = mapped_column(Text, default="{}")
    snapshot_reason: Mapped[str] = mapped_column(String(64), default="")
    created_at: Mapped[datetime] = mapped_column(UtcDateTime, server_default=func.now())
