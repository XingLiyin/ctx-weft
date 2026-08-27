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

- **blob 并入 memory**（裁定 D4，Task C3）：本 provider 同时实现 ``BlobStore``，
  字节落 ``memory_blobs``、引用边落 ``memory_blob_refs``（与 ingest 同事务），
  回收走延迟幂等的 ``collect_blobs``。**永不在 fold 里同步删字节**。
"""

from __future__ import annotations

import hashlib
import json
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal

from sqlalchemy import and_, delete, func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from ctx_weft.core.content import (
    content_from_jsonable,
    content_to_jsonable,
    extract_blob_refs,
)
from ctx_weft.core.utils import generate_id
from ctx_weft.protocols import (
    BLOB_REF_PREFIX,
    BlobStore,
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
    MemoryBlobModel,
    MemoryBlobRefModel,
    MemoryEventModel,
    MemorySubscriptionModel,
)

logger = logging.getLogger(__name__)

_DEFAULT_TENANT = "default"
_DEFAULT_KINDS = (MemoryKind.CONVERSATION_TURN, MemoryKind.SUMMARY)

# content_format 判别列的取值（models.py 有完整说明）
_FMT_TEXT = "text"
_FMT_PARTS = "parts"

#: blob 回收的默认宽限期。取值理由见 ``SqlMemoryProvider.collect_blobs``。
_DEFAULT_BLOB_GRACE = timedelta(hours=24)


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


class SqlMemoryProvider(MemoryProvider, BlobStore):
    """SQLAlchemy async MemoryProvider **兼 BlobStore**。SQLite 是默认后端，postgres 同一份代码。

    ── 【blob 与租户】三处取向（Task C3，简报要求逐处表态）────────────────────

    1. **回收侧：不按 tenant 过滤活引用（看全表）。** 这是安全要求而非选择：
       内容寻址会跨租户去重（同字节 → 同 sha → 同一行），只看本租户的活引用，
       租户 A 的一次 fold 就会删掉租户 B 仍在引用的那一行，B 的图永久丢失。
    2. **``get`` 侧：不校验 tenant。** 理由三条——
       (a) 与已立的【多租户隔离契约】第 5 条**同一条划线**：契约区分「隐式跨行扫描」
           （必须按租户分区：load_view / recall_topic / 订阅表 / PUBLICATION 覆盖）与
           「调用方显式给 id 的操作」（保持全局 id 命名空间：``ingest`` 按 id 幂等、
           ``fold`` 按 id 遗忘）。``get(ref)`` 正是后者——调用方指名了一个 id，
           不是「扫出别人的行」。
       (b) 与第 1 条**必须自洽**：既然一份字节跨租户共享同一行，行上就没有「属于谁」
           这件事可校验；硬按「首个 put 者」记个 owner 再校验，会让第二个租户
           取不回自己合法引用的图 —— ``get`` 返 None → ``rehydrate`` 降级成
           ``[image unavailable]`` → **图永久丢失**，正是本任务要避开的方向。
       (c) sha 是 SHA-256 内容哈希：能说出 sha 意味着已持有该内容（256-bit 原像），
           故校验能挡住的只有「已经知道答案的人」。
       **残余风险（如实记）**：这是一条 sha 可探测的存在性侧信道——持有同一份字节的
       租户能确认「系统里已有这份字节」。要治只能放弃跨租户去重（见第 3 条），
       宿主若有此要求，改法是给 blob 表加 tenant 列并进主键。
    3. **``put`` 侧：跨租户同 sha 共享一行。** 内容寻址去重的全部价值都在这里
       （一张图被 N 个租户上传只占一份字节），且这是第 2 条自洽的前提。
       ``media_type`` 冲突沿用 filesystem 实现的既有语义：**先写入者胜**——
       ``put`` 本就是「已存在则不重写」的幂等写，避免「同一份数据的类型取决于
       谁最后 put」这种依赖调用顺序、难以复现的行为。
       **但 ``created_at`` 每次 put 都刷新**（见 ``collect_blobs`` 的宽限期讨论）。
    """

    name = "sql_memory"

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        blob_grace_period: timedelta = _DEFAULT_BLOB_GRACE,
    ) -> None:
        self._factory = session_factory
        self._blob_grace = blob_grace_period

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
        # blob 引用边：**与事件行同一个事务**。判据复用归一层，不在这里另写 isinstance——
        # 判据一旦分叉，「哪些 blob 还活着」就会和「出网时哪些 part 会被 rehydrate」
        # 对不上，而那正好是「回收删掉了还在用的图」的成因。
        for ref in extract_blob_refs(event.content):
            db.add(MemoryBlobRefModel(
                event_id=event_id, sha=ref[len(BLOB_REF_PREFIX):]))
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

    # ── BlobStore（裁定 D4：字节也归 memory）──────────────────────────────────

    async def put(self, data: bytes, media_type: str, ctx: ProviderContext) -> str:
        """内容寻址存字节，返回 ``blob:<sha256>``；同字节幂等（已存在则不重写内容）。

        **不看 tenant**——跨租户同 sha 共享一行（类 docstring 第 3 条）。
        ``media_type`` 冲突：先写入者胜。

        **但 ``created_at`` 每次都刷新**，包括命中已有行的幂等 put。这不是记账癖好，
        是 ``collect_blobs`` 的宽限期赖以成立的前提：宽限期保护的是「已 put、尚未
        ingest」这个窗口，而这个窗口在**重新 put 一份旧字节**时会再次打开
        （典型场景：一张图先被 E1 引用、E1 被 fold 掉、同一张图又出现在新一轮对话里 →
        put 命中老行 → 若 ``created_at`` 还停在几天前，清扫会在 ingest 之前把它删掉，
        留下悬空 ref）。刷新后 ``created_at`` 的语义是「最后一次有人声称要用它」。
        """
        sha = hashlib.sha256(data).hexdigest()
        now = datetime.now(UTC)
        async with self._factory() as db:
            try:
                async with db.begin():
                    row = await db.get(MemoryBlobModel, sha)
                    if row is None:
                        db.add(MemoryBlobModel(
                            sha=sha, media_type=media_type or "",
                            data=data, created_at=now))
                    else:
                        row.created_at = now  # 先写入者胜：内容与 media_type 都不动
            except IntegrityError:
                # 并发 put 同一份字节：另一方已插入。补做一次 touch 即可，
                # 内容寻址保证那一行的字节与本次完全相同。
                async with db.begin():
                    await db.execute(
                        update(MemoryBlobModel)
                        .where(MemoryBlobModel.sha == sha)
                        .values(created_at=now)
                    )
        return f"{BLOB_REF_PREFIX}{sha}"

    async def get(self, ref: str, ctx: ProviderContext) -> tuple[bytes, str] | None:
        """按 ref 取回 ``(data, media_type)``；ref 不存在 / 已回收 / 形态不对 → ``None``。

        **不看 tenant**——理由见类 docstring 第 2 条（``get(ref)`` 是「调用方显式给 id」，
        与契约第 5 条同一条划线；且跨租户共享一行时按 owner 校验会让第二个租户
        取不回自己合法引用的图）。

        前缀检查保留（非 ``blob:`` 开头一律 None）。⚠️ 它在本实现里**只是契约条**，
        不再是安全护栏：filesystem 实现里 ref 会被拼进文件路径，缺前缀检查时
        ``"http://example.com/x.png"`` 被 pathlib 当 UNC 网络路径**真的发起 SMB 外连**
        （见 tests/unit/test_sql_blob_store.py 的同名用例）。SQL 侧不构造任何路径，
        sha 只作为**绑定参数**进 WHERE，该攻击面不存在。

        DB 层异常**不吞**：契约要求「不存在/已回收返 None」，不是「任何故障都装没事」。
        """
        if not ref.startswith(BLOB_REF_PREFIX):
            return None
        sha = ref[len(BLOB_REF_PREFIX):]
        if not sha:
            return None
        async with self._factory() as db:
            row = await db.get(MemoryBlobModel, sha)
            if row is None:
                return None
            return bytes(row.data), row.media_type or "application/octet-stream"

    async def collect_blobs(self, *, now: datetime | None = None) -> int:
        """回收无人引用的 blob 字节，返回删除行数。**延迟、幂等的清扫**，不在写路径上。

        判定（两条同时成立才删，**取舍一律偏保守：宁可漏删，不可误删**）：

        1. **没有活引用**：``memory_blob_refs`` JOIN ``memory_events`` 后，
           不存在任何 ``is_superseded = 0`` 的引用者。**不按 tenant 过滤**——
           必须看见全部租户的活引用，否则租户 A 的 fold 会删掉租户 B 仍在用的同 sha 行
           （类 docstring 第 1 条）。内容寻址的去重天然被覆盖：一张图被三条记录引用、
           其中两条 fold 掉，第三条仍是活引用 → 不删。跨会话继承同理
           （``_copy_memory_for_inherit`` ingest 的是含相同 ref 的**新记录**）。
        2. **``created_at`` 早于宽限期**。这是**正确性要求，不是优化**：
           入口顺序是 ``validate → normalize(put 拿 ref) → …一路往下… → ingest``，
           故必然存在「blob 行已写入、还没有任何 event 行引用它」的窗口，
           天真的查询会在窗口内把还没用上的图删掉。

        **宽限期对两类 blob 一视同仁**（简报把「从未被引用」与「引用已全部失效」
        分成两类、只对前者加宽限期；本实现对后者也加）。理由：后者同样有窗口——
        ``put`` 命中一份**旧**字节时（其引用者已全部 fold），新的 put→ingest 窗口
        照样打开，只按第 1 条判会立刻删掉它。既然 ``put`` 会刷新 ``created_at``，
        统一按「最后一次有人声称要用它」计时既更简单、又严格更保守。
        代价只是「fold 之后字节多留一个宽限期」——泄漏磁盘，是安全方向。

        **宽限期取值：默认 24 小时**（``blob_grace_period`` 可覆盖）。取值理由：
        - 下界由**真实窗口**定：put→ingest 在进程内是毫秒级，但中间隔着 HITL
          park（可等人几小时）、重试、以及「会话崩溃后由宿主重放」。分钟级不够。
        - 上界由**泄漏成本**定：孤儿最多堆积一个宽限期的量，一天的上传量对
          任何宿主都是可接受的磁盘占用。
        - ``created_at`` 与 ``now`` 可能来自不同机器的时钟（宿主多进程 + DB 服务器），
          小时级窗口对分钟级时钟漂移完全免疫，分钟级窗口不是。
        - 这一侧的错误是不对称的：删早了 = 悬空 ref → rehydrate 降级成
          ``[image unavailable]`` → **图永久丢失**；删晚了 = 磁盘多占一天。

        **绝不在 ``fold`` 里同步删。** fold 是原子的「遗忘 + 补偿」，删字节不能加入
        同一事务。崩溃发生在 fold 与清扫之间 → blob 被孤立（泄漏），
        **而不是产生悬空 ref**。宿主按需定时调用本方法（幂等，可随时重跑）。
        """
        cutoff = (now or datetime.now(UTC)) - self._blob_grace
        live_shas = (
            select(MemoryBlobRefModel.sha)
            .join(MemoryEventModel, MemoryEventModel.id == MemoryBlobRefModel.event_id)
            .where(MemoryEventModel.is_superseded.is_(False))
        )
        async with self._factory() as db, db.begin():
            result = await db.execute(
                delete(MemoryBlobModel).where(
                    MemoryBlobModel.created_at < cutoff,
                    MemoryBlobModel.sha.not_in(live_shas),
                )
            )
        deleted = int(result.rowcount or 0)
        if deleted:
            logger.info("collect_blobs: reclaimed %d unreferenced blob(s)", deleted)
        return deleted

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
    *,
    blob_grace_period: timedelta = _DEFAULT_BLOB_GRACE,
) -> AsyncIterator[SqlMemoryProvider]:
    """开一个 SQLite backed 的 provider（建表 → yield → dispose）。

    测试与单机部署用。宿主接 postgres 时自带 engine / migration，直接构造
    ``SqlMemoryProvider(session_factory)`` 即可，不必走这里。
    """
    engine, factory = make_session_factory(f"sqlite+aiosqlite:///{db_path}")
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        yield SqlMemoryProvider(factory, blob_grace_period=blob_grace_period)
    finally:
        await engine.dispose()
