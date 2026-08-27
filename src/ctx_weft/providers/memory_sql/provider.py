"""SqlMemoryProvider：SQLAlchemy async 的 MemoryProvider 实现（默认 SQLite，postgres 同源）。

协议面 8 方法齐备：写 ingest / fold、读 load_view / recall_topic / recall_semantic、
订阅 subscribe_topic / list_subscriptions、能力 describe。

与 `providers/memory_blackboard/in_memory.py` 是同一套契约的两个实现，
`tests/unit/test_memory_conformance.py` 对两者跑同一套用例。

设计要点：

- **多租户隔离**（协议【多租户隔离契约】）：``tenant`` 列 + 归一
  ``normalize_tenant``（``None`` / 空串 → ``"default"``），**写读两侧同一个函数**。
  比较一律走 ``COALESCE(tenant, 'default')``——存量行的 NULL 因此与显式 ``"default"``
  落同一分区，零数据迁移。隐式跨行扫描（PUBLICATION 覆盖）同样只在本租户分区内进行；
  订阅表唯一键含 tenant。
  按契约第 5 条，``ingest`` 的按 id 幂等与 ``fold`` 的按 id 遗忘**仍是全局 id 命名空间**
  （调用方显式给 id 的操作，不是隐式扫描）——与 in_memory 的划线一致。
- **多模态无损存取**：``content_format`` 判别列（见 models.py）；``list[ContentPart]``
  经 ``content_to_jsonable`` 落 JSON、``content_from_jsonable`` 还原，**持久化层不拍扁**。
- **词汇双读**：新行 ``type`` 列存 kind 字符串，存量行存 legacy 类型字符串；
  load_view 用 ``kind_expansion`` 展开 ``type IN (...)``，返回前经 ``normalize_view``。
  零数据迁移（v2 设计 §6）。
- **fold 原子**：遗忘 + 补偿在单个事务内（SQLite / postgres 天然满足）。

**不实现 BlobStore**——blob 表与回收是 Task C3；conformance 的 blob 用例经
``_supports_blobs`` 探测自动 skip。
"""

from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Literal

from sqlalchemy import and_, func, select, update
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from ctx_weft.core.content import content_from_jsonable, content_to_jsonable
from ctx_weft.core.utils import generate_id
from ctx_weft.protocols import (
    MemoryAddress,
    MemoryEvent,
    MemoryEventType,
    MemoryProvider,
    MemoryProviderInfo,
    MemoryRecord,
    MemoryScope,
    ProviderContext,
    Subscription,
)
from ctx_weft.protocols.memory_compat import (
    MemoryKind,
    kind_expansion,
    kind_of,
    layer_of,
    normalize_view,
    validate_half_address,
)
from ctx_weft.providers.memory_sql.models import (
    Base,
    MemoryEventModel,
    MemorySubscriptionModel,
)

logger = logging.getLogger(__name__)

_DEFAULT_TENANT = "default"
_DEFAULT_KINDS = (MemoryKind.CONVERSATION_TURN, MemoryKind.SUMMARY)

# content_format 判别列的取值（models.py 有完整说明）
_FMT_TEXT = "text"
_FMT_PARTS = "parts"


def normalize_tenant(tenant_id: str | None) -> str:
    """租户归一（协议【多租户隔离契约】第 3 条）：``None`` / 空串 → ``"default"``。

    与 `in_memory.normalize_tenant` 是**同一条规则**——两个 provider 必须一致，
    否则同一套 conformance 断言不可能同时成立。刻意各留一份而不跨包 import：
    provider 之间不该互相依赖（第三方 provider 也只能照契约实现，import 不到）。
    """
    return tenant_id or _DEFAULT_TENANT


def _tenant_expr(column: Any) -> Any:
    """SQL 侧的同一条归一：``COALESCE(tenant, 'default')``。

    存量行（宿主写的，没有 tenant 列值）NULL 因此落默认分区，不迁移即可读。
    """
    return func.coalesce(column, _DEFAULT_TENANT)


# ── 内容编解码（多模态无损存取契约）──────────────────────────────────────────


def _encode_content(content: str | list[Any]) -> tuple[str, str]:
    """``content`` → ``(存储字符串, content_format)``。

    ``str`` 原样存 + ``"text"``；``list[ContentPart]`` 走 ``content_to_jsonable``
    再 ``json.dumps`` + ``"parts"``。**禁止拍扁成纯文本**（契约第 4 条）。

    新行一律写出**非空**的判别列：只有这样 ``NULL`` 才能唯一地表示「存量行」，
    读侧的兼容启发式（见 `_row_content`）才不会误伤新写的纯文本
    （一条正文恰好是 ``"[1, 2]"`` 的用户消息就是反例）。
    """
    jsonable = content_to_jsonable(content)
    if isinstance(jsonable, str):
        return jsonable, _FMT_TEXT
    return json.dumps(jsonable), _FMT_PARTS


def _row_content(raw: str, content_format: str | None) -> str | list[Any]:
    """存储字符串 → ``content``（``_encode_content`` 的逆）。

    ``content_format`` 三态：
    - ``"parts"`` → JSON 解回 ``list[ContentPart]``；
    - ``"text"``  → 纯文本，原样返回；
    - ``NULL``    → **存量行**（宿主 provider 写的，那时还没有判别列）。宿主当时的读法
      就是「试 json.loads，是 list 就当结构化内容」，这里保留同一启发式，
      否则平滑切换后宿主的历史结构化内容会变成一坨 JSON 字符串。
      启发式**只对 NULL 行生效**，新行永不走这条路。
    """
    if content_format == _FMT_PARTS:
        return content_from_jsonable(json.loads(raw))  # type: ignore[return-value]
    if content_format is not None:
        return raw
    try:
        parsed = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return raw
    if isinstance(parsed, list):
        return content_from_jsonable(parsed)  # type: ignore[return-value]
    return raw


def _parse_row_vocab(row: MemoryEventModel) -> tuple[MemoryEventType | None, MemoryKind | None]:
    """``type`` 列 → ``(legacy type | None, kind | None)``。新行存 kind、存量行存 legacy 类型。"""
    try:
        return MemoryEventType(row.type), None
    except ValueError:
        pass
    try:
        return None, MemoryKind(row.type)
    except ValueError:
        return None, None  # 未知词汇（前向兼容）：存储保留、视图不见


def _row_to_record(row: MemoryEventModel) -> MemoryRecord:
    type_, kind = _parse_row_vocab(row)
    try:
        scope = MemoryScope(row.layer)
    except ValueError:
        scope = None
    try:
        meta = json.loads(row.metadata_json or "{}")
    except (json.JSONDecodeError, TypeError):
        meta = {}
    return MemoryRecord(
        id=row.id,
        type=type_,
        content=_row_content(row.content, row.content_format),
        timestamp=row.timestamp,
        role=row.role,  # type: ignore[arg-type]
        topic=row.topic,
        kind=kind,  # legacy 行 None → normalize_view 按 LEGACY_TRIPLE 重打
        scope=scope,
        address=MemoryAddress(
            session_id=row.session_id, task_id=row.task_id, agent_id=row.agent_id),
        metadata={
            **meta,
            "seq_no": row.seq_no,
            "topic_seq_no": row.topic_seq_no,
            "task_id": row.task_id or "",
        },
    )


def _partition_where(address: MemoryAddress, scope: MemoryScope, tenant: str) -> Any:
    """归属分区 WHERE（含租户）：seq 计数与视图查询共用同一口径。

    与 in_memory 的 ``_scope_key`` 一一对应——那边把 tenant 编进计数器 key，
    这里把它编进 WHERE。
    """
    conds = [
        _tenant_expr(MemoryEventModel.tenant) == tenant,
        MemoryEventModel.session_id == address.session_id,
        MemoryEventModel.layer == scope.value,
    ]
    if scope is MemoryScope.TASK:
        conds.append(MemoryEventModel.task_id == address.task_id)
    elif scope is MemoryScope.AGENT:
        conds.append(MemoryEventModel.agent_id == address.agent_id)
    return and_(*conds)


class SqlMemoryProvider(MemoryProvider):
    """SQLAlchemy async MemoryProvider。SQLite 是默认后端，postgres 同一份代码。"""

    name = "sql_memory"

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._factory = session_factory

    # ── 写（2）────────────────────────────────────────────────────────────────

    async def ingest(self, event: MemoryEvent, ctx: ProviderContext) -> str:
        async with self._factory() as db, db.begin():
            if event.id is not None and await self._id_exists(db, event.id):
                # record-id 契约：已存在（含 superseded）= no-op，不比对内容、不推进计数器
                return event.id
            return await self._ingest_in_tx(db, event, ctx)

    async def fold(
        self,
        supersede_ids: list[str],
        replacements: list[MemoryEvent],
        ctx: ProviderContext,
    ) -> list[str]:
        """原子「遗忘 + 补偿」：**单个事务**内标 superseded + 写 replacements。

        已 superseded / 不存在的 id 跳过（幂等）；replacement 带 id 时按 record-id 契约幂等。
        """
        new_ids: list[str] = []
        async with self._factory() as db, db.begin():
            if supersede_ids:
                await db.execute(
                    update(MemoryEventModel)
                    .where(
                        MemoryEventModel.id.in_(supersede_ids),
                        MemoryEventModel.is_superseded.is_(False),
                    )
                    .values(is_superseded=True)
                )
                await db.flush()
            for ev in replacements:
                if ev.id is not None and await self._id_exists(db, ev.id):
                    new_ids.append(ev.id)
                    continue
                new_ids.append(await self._ingest_in_tx(db, ev, ctx))
        return new_ids

    @staticmethod
    async def _id_exists(db: AsyncSession, event_id: str) -> bool:
        """按 id 存在性（**不看 tenant**——契约第 5 条：id 命名空间仍是全局的）。"""
        result = await db.execute(
            select(MemoryEventModel.id).where(MemoryEventModel.id == event_id))
        return result.scalar_one_or_none() is not None

    async def _ingest_in_tx(
        self, db: AsyncSession, event: MemoryEvent, ctx: ProviderContext
    ) -> str:
        """ingest 内核（须在事务内调用）；fold 复用它以保证原子性。"""
        event_id = event.id or generate_id("mev")
        # v2 归一：kind 解析失败 = 死类型（存储保留、视图不见）；scope 恒可解析
        try:
            kind: MemoryKind | None = kind_of(event.type, event.kind)
        except ValueError:
            kind = None
        scope = layer_of(event.type, event.scope)
        tenant = normalize_tenant(ctx.tenant_id)
        addr = event.address
        assert addr is not None  # MemoryEvent.__post_init__ 已强制

        result = await db.execute(
            select(func.coalesce(func.max(MemoryEventModel.seq_no), 0))
            .where(_partition_where(addr, scope, tenant))
        )
        seq_no = (result.scalar_one() or 0) + 1

        topic_seq = 0
        if event.topic:
            if kind is MemoryKind.PUBLICATION:
                # 黑板覆盖语义：同 topic 旧发布标 superseded，只留最新一条。
                # **只在本租户分区内扫**——否则 B 一发布就把 A 的黑板行标掉，
                # 而 B 连那行都读不到（能销毁读不到的数据，比泄漏更差）。
                await db.execute(
                    update(MemoryEventModel)
                    .where(
                        _tenant_expr(MemoryEventModel.tenant) == tenant,
                        MemoryEventModel.topic == event.topic,
                        MemoryEventModel.type.in_(
                            kind_expansion(MemoryKind.PUBLICATION, MemoryScope.SESSION)),
                        MemoryEventModel.is_superseded.is_(False),
                    )
                    .values(is_superseded=True)
                )
            # topic_seq 刻意**不按租户分区**：与 in_memory 的 `_topic_seq` 同口径
            # （台账 L20 已记为已知弱侧信道）。过滤在读侧做，正确性不受影响。
            result2 = await db.execute(
                select(func.coalesce(func.max(MemoryEventModel.topic_seq_no), 0))
                .where(MemoryEventModel.topic == event.topic)
            )
            topic_seq = (result2.scalar_one() or 0) + 1

        content_str, content_format = _encode_content(event.content)  # type: ignore[arg-type]
        db.add(MemoryEventModel(
            id=event_id,
            session_id=addr.session_id,
            task_id=addr.task_id,
            agent_id=addr.agent_id,
            layer=scope.value,
            # 新行存 kind 字符串；legacy 构造（type 给定）原样存 legacy 类型字符串
            type=str(event.type) if event.type is not None else str(event.kind),
            role=event.role,
            topic=event.topic,
            content=content_str,
            content_format=content_format,
            seq_no=seq_no,
            topic_seq_no=topic_seq,
            is_superseded=False,
            metadata_json=json.dumps(event.metadata),
            timestamp=event.timestamp,
            tenant=tenant,
        ))
        await db.flush()
        return event_id

    # ── 读（3）────────────────────────────────────────────────────────────────

    async def load_view(
        self,
        address: MemoryAddress,
        scope: MemoryScope,
        ctx: ProviderContext,
        kinds: list[MemoryKind] | None = None,
    ) -> list[MemoryRecord]:
        """全量幸存视图，``(timestamp, seq_no)`` 升序，半址过滤，租户隔离。"""
        validate_half_address(address, scope)
        wanted = list(kinds) if kinds is not None else list(_DEFAULT_KINDS)
        type_strs: set[str] = set()
        for k in wanted:
            type_strs |= kind_expansion(k, scope)

        tenant = normalize_tenant(ctx.tenant_id)
        conds = [
            _tenant_expr(MemoryEventModel.tenant) == tenant,
            MemoryEventModel.session_id == address.session_id,
            MemoryEventModel.layer == scope.value,
            MemoryEventModel.is_superseded.is_(False),
            MemoryEventModel.type.in_(type_strs),
        ]
        if scope is MemoryScope.TASK:
            if address.task_id is not None:
                conds.append(MemoryEventModel.task_id == address.task_id)
                if address.agent_id is not None:  # 全址时防御性校验 agent 归属
                    conds.append(MemoryEventModel.agent_id == address.agent_id)
            else:  # 跨 task 聚合
                conds.append(MemoryEventModel.agent_id == address.agent_id)
        elif scope is MemoryScope.AGENT:
            conds.append(MemoryEventModel.agent_id == address.agent_id)

        async with self._factory() as db:
            result = await db.execute(
                select(MemoryEventModel)
                .where(*conds)
                .order_by(MemoryEventModel.timestamp.asc(), MemoryEventModel.seq_no.asc())
            )
            rows = list(result.scalars().all())
        return normalize_view([_row_to_record(r) for r in rows])

    async def recall_topic(
        self,
        topic: str,
        since: int,
        ctx: ProviderContext,
    ) -> tuple[list[MemoryRecord], int]:
        tenant = normalize_tenant(ctx.tenant_id)
        async with self._factory() as db:
            result = await db.execute(
                select(MemoryEventModel)
                .where(
                    _tenant_expr(MemoryEventModel.tenant) == tenant,  # 租户隔离
                    MemoryEventModel.topic == topic,
                    MemoryEventModel.topic_seq_no > since,
                    MemoryEventModel.is_superseded.is_(False),
                )
                .order_by(MemoryEventModel.topic_seq_no.asc())
            )
            rows = list(result.scalars().all())
        records = [_row_to_record(r) for r in rows]
        new_cursor = rows[-1].topic_seq_no if rows else since
        return records, new_cursor

    async def recall_semantic(
        self,
        query: str,
        scope: MemoryAddress,
        top_k: int,
        ctx: ProviderContext,
    ) -> list[MemoryRecord]:
        """未实现向量检索（``describe().supports_semantic=False``）→ 返空。

        真要接向量库时，**预过滤必须带 tenant**（契约类 docstring），否则相似度会
        直接把别的租户的内容捞出来。
        """
        return []

    # ── 订阅（2）──────────────────────────────────────────────────────────────

    async def subscribe_topic(
        self,
        session_id: str,
        topic: str,
        intent: Literal[
            "subtask", "predecessor", "long_term_background", "long_term_project_log"],
        ctx: ProviderContext,
        task_id: str = "",
    ) -> str:
        """幂等键 ``(tenant, session_id, task_id, topic)``——tenant 必须在键里，
        否则同 session_id 的另一个租户会撞进幂等分支、拿到别人的订阅与游标。"""
        tenant = normalize_tenant(ctx.tenant_id)
        async with self._factory() as db, db.begin():
            existing = await db.execute(
                select(MemorySubscriptionModel).where(
                    _tenant_expr(MemorySubscriptionModel.tenant) == tenant,
                    MemorySubscriptionModel.session_id == session_id,
                    MemorySubscriptionModel.task_id == task_id,
                    MemorySubscriptionModel.topic == topic,
                )
            )
            row = existing.scalars().first()
            if row is not None:
                return row.id  # 幂等：保留原订阅（含游标），不重置
            sub_id = generate_id("sub")
            db.add(MemorySubscriptionModel(
                id=sub_id,
                session_id=session_id,
                task_id=task_id,
                topic=topic,
                cursor=0,
                intent=intent,
                tenant=tenant,
            ))
        return sub_id

    async def list_subscriptions(
        self,
        session_id: str,
        ctx: ProviderContext,
        task_id: str | None = None,
    ) -> list[Subscription]:
        tenant = normalize_tenant(ctx.tenant_id)
        stmt = select(MemorySubscriptionModel).where(
            _tenant_expr(MemorySubscriptionModel.tenant) == tenant,  # 租户隔离
            MemorySubscriptionModel.session_id == session_id,
        )
        if task_id is not None:
            # 该 task 自己的订阅 + session 级订阅（task_id == ""）
            stmt = stmt.where(MemorySubscriptionModel.task_id.in_([task_id, ""]))
        async with self._factory() as db:
            result = await db.execute(stmt.order_by(MemorySubscriptionModel.created_at.asc()))
            rows = list(result.scalars().all())
        return [
            Subscription(
                session_id=r.session_id,
                topic=r.topic,
                cursor=r.cursor,
                intent=r.intent,  # type: ignore[arg-type]
                task_id=r.task_id,
            )
            for r in rows
        ]

    # ── 能力 ──────────────────────────────────────────────────────────────────

    async def describe(self, ctx: ProviderContext) -> MemoryProviderInfo:
        return MemoryProviderInfo(
            name=self.name,
            supports_semantic=False,
            supports_topic=True,
            archives_superseded=True,
        )


# ── 建库/开库便利函数 ─────────────────────────────────────────────────────────


def make_session_factory(url: str, **engine_kwargs: Any) -> Any:
    """``(engine, session_factory)``。url 例：``sqlite+aiosqlite:///path/mem.db``。"""
    engine = create_async_engine(url, **engine_kwargs)
    return engine, async_sessionmaker(engine, expire_on_commit=False)


@asynccontextmanager
async def open_sqlite_memory(
    db_path: str | Path,
) -> AsyncIterator[SqlMemoryProvider]:
    """开一个 SQLite backed 的 provider（建表 → yield → dispose）。

    测试与单机部署用。宿主接 postgres 时自带 engine / migration，直接构造
    ``SqlMemoryProvider(session_factory)`` 即可，不必走这里。
    """
    engine, factory = make_session_factory(f"sqlite+aiosqlite:///{db_path}")
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        yield SqlMemoryProvider(factory)
    finally:
        await engine.dispose()
