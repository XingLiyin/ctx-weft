"""SQLAlchemy 表模型：``memory_events`` / ``memory_subscriptions``。

**平滑切换约束（裁定 D4）**：表名与既有列**逐字保持**与宿主
（`IpMasterCoworkPy/src/ipmastercowork/persistence/postgres/models.py` 的
`MemoryEventModel` / `MemorySubscriptionModel`）一致，本模块**只新增 nullable 列**，
故同一份 schema 既能建新库，也能直接读宿主存量行（存量行的新列全为 NULL，读侧给默认值）。

新增的两列（都 nullable）：

- ``memory_events.tenant`` / ``memory_subscriptions.tenant`` —— 多租户隔离分区键
  （协议【多租户隔离契约】）。存量行 NULL → 读侧归一为 ``"default"``，
  **不迁移数据即落默认分区**。
- ``memory_events.content_format`` —— 内容判别列（多模态无损存取契约）：
  ``"text"`` = ``content`` 是纯文本；``"parts"`` = ``content`` 是
  ``content_to_jsonable`` 产物的 JSON；``NULL`` = **存量行**（宿主 provider 写的，
  没有判别列），读侧走兼容启发式，见 `provider._row_content`。

索引取向：既有两个索引（`ix_memory_task` / `ix_memory_agent`）**名字与列定义一字未改**
——改了会与宿主已建的索引对不上。租户维度另起新名的索引，增量 DDL 即可。

⚠️ **唯一一处对宿主 schema 的破坏性要求**：`memory_subscriptions` 的幂等唯一键必须
含 tenant（契约第 4 条），而宿主已有一个**同名**唯一索引
`ix_subscriptions_session_task_topic` 建在 3 列上。本模块改用新名
`ix_subscriptions_tenant_session_task_topic`（4 列）。宿主切换时须
**先 DROP 旧唯一索引**，否则两个租户的同 (session, task, topic) 订阅会撞 IntegrityError。
这是「让两个租户能各有一条订阅」的必要条件，无法靠只加列绕开。
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import (
    Boolean,
    DateTime,
    Index,
    Integer,
    String,
    Text,
    TypeDecorator,
    func,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    """本 provider 自带的 declarative base（宿主可把这两张表映射进自己的 Base）。"""


class UtcDateTime(TypeDecorator):
    """时区保真的 DateTime：入库转 UTC，出库补回 ``tzinfo=UTC``。

    为什么必须自己包一层：**SQLite 没有时区类型**。SQLAlchemy 的 SQLite 方言把
    aware datetime 的 tzinfo 直接丢掉、回读得到 naive datetime——于是
    ``rec.timestamp == 写入时的 aware datetime`` 恒为 False（naive 与 aware 不相等），
    而排序又照常工作，所以这个丢失**不会**在任何排序类断言上暴露，只会在等值比较上
    炸一条。postgres 侧 `TIMESTAMP WITH TIME ZONE` 本就保真，本装饰器在那边是恒等
    变换（bind 时值已是 UTC aware，result 时已带 tzinfo 不再补）。

    naive 输入按 UTC 解释（仓内 memory 事件的时间戳统一来自 ``datetime.now(UTC)``）。
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


class MemoryEventModel(Base):
    """一条 memory 事件。列顺序/类型与宿主 `MemoryEventModel` 对齐（新列在末尾）。"""

    __tablename__ = "memory_events"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    session_id: Mapped[str] = mapped_column(String(64), index=True)
    task_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    agent_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    layer: Mapped[str] = mapped_column(String(16), default="task", index=True)
    type: Mapped[str] = mapped_column(String(64), index=True)
    role: Mapped[str | None] = mapped_column(String(32), nullable=True)
    topic: Mapped[str | None] = mapped_column(String(256), nullable=True, index=True)
    content: Mapped[str] = mapped_column(Text)
    seq_no: Mapped[int] = mapped_column(Integer)
    topic_seq_no: Mapped[int] = mapped_column(Integer, default=0)
    is_superseded: Mapped[bool] = mapped_column(Boolean, default=False)
    metadata_json: Mapped[str] = mapped_column(Text, default="{}")
    timestamp: Mapped[datetime] = mapped_column(UtcDateTime, server_default=func.now())

    # ── 新增（一律 nullable，存量行读得动）──
    tenant: Mapped[str | None] = mapped_column(String(64), nullable=True)
    content_format: Mapped[str | None] = mapped_column(String(16), nullable=True)

    __table_args__ = (
        # 与宿主逐字相同（名字与列都不改）
        Index("ix_memory_task", "session_id", "layer", "task_id", "type"),
        Index("ix_memory_agent", "session_id", "layer", "agent_id", "type"),
        # 租户维度：新名新索引，宿主增量建即可
        Index("ix_memory_tenant_task", "tenant", "session_id", "layer", "task_id"),
        Index("ix_memory_tenant_agent", "tenant", "session_id", "layer", "agent_id"),
        Index("ix_memory_tenant_topic", "tenant", "topic", "topic_seq_no"),
    )


class MemorySubscriptionModel(Base):
    """task 对 topic 的订阅。唯一键含 tenant——见模块 docstring 的 ⚠️。"""

    __tablename__ = "memory_subscriptions"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    session_id: Mapped[str] = mapped_column(String(64), index=True)
    task_id: Mapped[str] = mapped_column(String(64), default="")
    topic: Mapped[str] = mapped_column(String(256))
    cursor: Mapped[int] = mapped_column(Integer, default=0)
    intent: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(UtcDateTime, server_default=func.now())

    # ── 新增（nullable）──
    tenant: Mapped[str | None] = mapped_column(String(64), nullable=True)

    __table_args__ = (
        Index(
            "ix_subscriptions_tenant_session_task_topic",
            "tenant", "session_id", "task_id", "topic",
            unique=True,
        ),
    )
