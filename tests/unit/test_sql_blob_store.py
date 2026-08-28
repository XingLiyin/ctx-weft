"""SqlMemoryProvider 的 BlobStore 面：契约（内容寻址 / 幂等 / get 不抛）+ 引用表 + 回收。

本文件由 `tests/unit/test_filesystem_blob_store.py` **改挂**而来（裁定 D5：移除
`FilesystemBlobStore`，其契约用例改挂 SQL provider——验的是 `BlobStore` 契约本身，
与实现无关）。九条原用例的去向：

- 七条**是契约**，逐条搬过来（前缀 / 内容寻址 / 幂等 / 往返 / 缺失返 None /
  media_type 先写入者胜 / 非 ``blob:`` 前缀返 None）。
- 两条**是 filesystem 实现专有的**：``put`` / ``get`` 在「workspace 未登记」时的
  失败语义。SQL provider 没有 per-session workspace 登记这件事，该状态不可达，
  故不搬。它们守的契约内核——「``get`` 的失败语义是返 None 而不是抛，
  否则一次取图失败会掀掉整个 LLM 请求」——由本文件的
  ``test_get_is_never_raising_for_malformed_or_missing_refs`` 承接（覆盖不下降）。

⚠️ **那条 Windows UNC 安全护栏的去向**（简报点名要求说明）：原
``test_get_ref_without_blob_prefix_returns_none_not_raise`` 的 docstring 记录了一条
实证攻击面——filesystem 实现把 ref 拼进文件路径，去掉前缀检查后
``"http://example.com/x.png"`` 被 pathlib 解释成 UNC 网络路径并**真的发起了 SMB 外连**
（OSError WinError 64）。**换到 SQL 后该攻击面不存在**：sha 不构造任何路径，只作为
**绑定参数**进 WHERE（SQLAlchemy 绑定，也无注入面）；缺了前缀检查最坏结果是把整个
ref 字符串当 sha 去查、查不到、返 None。

**所以这条测试没有删，而是保留成纯契约条**（前缀不对必须返 None、不抛），
docstring 里改记了「护栏为何不再需要」。删掉它反而会让「``get`` 对垃圾输入必须
返 None」这条契约在本 provider 上失去覆盖。
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import func, select

from ctx_weft.core.content import collect_blob_refs
from ctx_weft.core.runtime import ProviderRegistry
from ctx_weft.protocols import (
    BLOB_REF_PREFIX,
    ImagePart,
    MemoryAddress,
    MemoryEvent,
    MemoryScope,
    NullBlobStore,
    ProviderContext,
    TextPart,
)
from ctx_weft.protocols.memory_compat import MemoryKind
from ctx_weft.providers.memory_blackboard import InMemoryMemoryProvider
from ctx_weft.providers.memory_sql import MemoryBlobModel, MemoryBlobRefModel
from ctx_weft.providers.memory_sql.provider import SqlMemoryProvider, open_sqlite_memory

_SESSION = "s1"
_AGENT = "ag1"
_BASE = datetime(2026, 1, 1, tzinfo=UTC)


@pytest.fixture
async def store(tmp_path: Any):
    """一个 SQLite backed 的 SqlMemoryProvider（同时是 MemoryProvider 与 BlobStore）。"""
    async with open_sqlite_memory(tmp_path / "memory.db") as provider:
        yield provider


def _ctx(tenant: str = "default", task_id: str = "t1") -> ProviderContext:
    return ProviderContext(
        session_id=_SESSION, tenant_id=tenant, task_id=task_id, agent_id=_AGENT)


def _turn_with_refs(*refs: str, task_id: str = "t1", event_id: str | None = None,
                    t: int = 0) -> MemoryEvent:
    """一条引用了若干 blob ref 的对话记录（外部化之后的真实形态）。"""
    parts: list[Any] = [TextPart(text="look at this")]
    parts += [
        ImagePart(data=r, media_type="image/png", source_type="ref", byte_size=64)
        for r in refs
    ]
    return MemoryEvent(
        kind=MemoryKind.CONVERSATION_TURN,
        scope=MemoryScope.TASK,
        address=MemoryAddress(session_id=_SESSION, task_id=task_id, agent_id=_AGENT),
        content=parts,
        timestamp=_BASE + timedelta(seconds=t),
        role="user",
        id=event_id,
    )


async def _blob_shas(store: SqlMemoryProvider) -> set[str]:
    """当前 ``memory_blobs`` 里的全部 sha（直读表——回收断言必须看真实存量）。"""
    async with store._factory() as db:  # 回收是表级效果，只能直读表
        rows = await db.execute(select(MemoryBlobModel.sha))
        return set(rows.scalars().all())


async def _ref_rows(store: SqlMemoryProvider) -> set[tuple[str, str]]:
    async with store._factory() as db:
        rows = await db.execute(
            select(MemoryBlobRefModel.event_id, MemoryBlobRefModel.sha))
        return {(e, s) for e, s in rows.all()}


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


_LATER = datetime.now(UTC) + timedelta(days=365)
"""一个远超任何宽限期的 ``now``，用来让清扫「时间条件」全部成立。

有了它，回收断言就**只剩活性条件在起作用**——「不该删的没被删」不再可能因为
「什么都还没过期」而假绿。每条这类用例都另带一个**确实会被删掉的对照 blob**。
"""


# ══════════════════════════════════════════════════════════════════════════════
# BlobStore 契约（自 test_filesystem_blob_store.py 改挂）
# ══════════════════════════════════════════════════════════════════════════════


async def test_put_returns_ref_with_blob_prefix(store: SqlMemoryProvider) -> None:
    ref = await store.put(b"hello world", "image/png", _ctx())
    assert ref.startswith(BLOB_REF_PREFIX)


async def test_put_is_content_addressed_same_bytes_same_ref(
        store: SqlMemoryProvider) -> None:
    ref1 = await store.put(b"same bytes", "image/png", _ctx())
    ref2 = await store.put(b"same bytes", "image/png", _ctx())
    assert ref1 == ref2


async def test_put_is_idempotent_only_one_row_stored(store: SqlMemoryProvider) -> None:
    """幂等的可观测形态：落盘从「只有一个文件」变成「只有一行」。"""
    await store.put(b"dedup me", "image/jpeg", _ctx())
    await store.put(b"dedup me", "image/jpeg", _ctx())
    async with store._factory() as db:
        n = (await db.execute(select(func.count()).select_from(MemoryBlobModel))).scalar_one()
    assert n == 1


async def test_get_returns_original_bytes_and_media_type(
        store: SqlMemoryProvider) -> None:
    ref = await store.put(b"round trip payload", "image/webp", _ctx())
    assert await store.get(ref, _ctx()) == (b"round trip payload", "image/webp")


async def test_get_nonexistent_ref_returns_none_not_raise(
        store: SqlMemoryProvider) -> None:
    assert await store.get(f"{BLOB_REF_PREFIX}nonexistent", _ctx()) is None


async def test_put_conflicting_media_type_first_writer_wins(
        store: SqlMemoryProvider) -> None:
    """相同 bytes、不同 media_type：内容寻址 → ref 相同。语义选择：先写入者胜。

    理由与 filesystem 实现逐字相同（沿用既有语义，避免「同一份数据被谁最后 put
    就变成谁的类型」这种依赖调用顺序、难以复现的行为）。
    """
    ref1 = await store.put(b"ambiguous type", "image/png", _ctx())
    ref2 = await store.put(b"ambiguous type", "image/jpeg", _ctx())
    assert ref1 == ref2
    assert await store.get(ref1, _ctx()) == (b"ambiguous type", "image/png")


async def test_get_ref_without_blob_prefix_returns_none_not_raise(
        store: SqlMemoryProvider) -> None:
    """非 ``blob:`` 前缀的字符串不是本 store 的 ref——返回 None，不抛。

    **这条在 filesystem 实现里曾是一条安全护栏**（去掉前缀检查后
    ``"http://example.com/x.png"`` 被 pathlib 当 UNC 路径、真的发起 SMB 外连）。
    SQL 侧不构造任何路径、sha 只作绑定参数，那个攻击面不存在——本条因此降级为
    **纯契约条**：形态不对的 ref 必须返 None 而不是抛。理由详见模块 docstring。
    """
    assert await store.get("http://example.com/x.png", _ctx()) is None
    assert await store.get("", _ctx()) is None


async def test_get_is_never_raising_for_malformed_or_missing_refs(
        store: SqlMemoryProvider) -> None:
    """承接原 filesystem 两条「workspace 未登记」用例守的契约内核：``get`` 恒不抛。

    rehydrate 跑在 gateway 的出网路径上——``get`` 若抛异常，一次取图失败会掀掉
    整个 LLM 请求，而不是退化成「这张图取不回来」。故此处必须钉死。
    """
    for bad in ["", "blob:", "blob:" + "z" * 200, "not-a-ref", BLOB_REF_PREFIX + "亂碼"]:
        assert await store.get(bad, _ctx()) is None


# ══════════════════════════════════════════════════════════════════════════════
# 引用表：与 ingest 同事务写入
# ══════════════════════════════════════════════════════════════════════════════


async def test_ingest_records_blob_refs_in_same_transaction(
        store: SqlMemoryProvider) -> None:
    """简报覆盖 1：ingest 含 ref 的 content → 引用表写入，边与内容里的 ref 一一对应。"""
    ref_a = await store.put(b"image-a", "image/png", _ctx())
    ref_b = await store.put(b"image-b", "image/png", _ctx())
    eid = await store.ingest(_turn_with_refs(ref_a, ref_b), _ctx())

    assert await _ref_rows(store) == {
        (eid, _sha(b"image-a")), (eid, _sha(b"image-b"))}


async def test_ingest_without_refs_writes_no_ref_rows(store: SqlMemoryProvider) -> None:
    """纯文本 / inline base64 的记录不产生引用边——不接 BlobStore 的宿主路径零副作用。"""
    await store.ingest(_turn_with_refs(), _ctx())
    await store.ingest(
        MemoryEvent(
            kind=MemoryKind.CONVERSATION_TURN, scope=MemoryScope.TASK,
            address=MemoryAddress(session_id=_SESSION, task_id="t1", agent_id=_AGENT),
            content="just text", timestamp=_BASE, role="user"),
        _ctx())
    assert await _ref_rows(store) == set()


async def test_ingest_registers_declared_blob_refs(store: SqlMemoryProvider) -> None:
    """blob_refs 声明的 ref 也要建引用边——只扫 content 会漏掉 L0.5 的占位。

    这是「声明式 ref」形态：content 里只有降级后的文本占位（结构化字段看不出
    这里曾经有张图），ref 靠 ``MemoryEvent.blob_refs`` 显式声明传递
    （见 `_rebuild` 的补偿事件构造，Task 3）。``_turn_with_refs`` 造不出这种
    形态（它总生成结构化 ImagePart），故这里手写 MemoryEvent。
    """
    ref = await store.put(b"declared-only", "image/png", _ctx())
    eid = await store.ingest(
        MemoryEvent(
            kind=MemoryKind.CONVERSATION_TURN,
            scope=MemoryScope.TASK,
            address=MemoryAddress(session_id=_SESSION, task_id="t1", agent_id=_AGENT),
            role="user",
            timestamp=_BASE,
            content=[TextPart(text=f"[image {ref} media_type=image/png]")],
            blob_refs=[ref],
        ),
        _ctx(),
    )
    assert await _ref_rows(store) == {(eid, _sha(b"declared-only"))}
    assert await store.collect_blobs(now=_LATER) == 0, "有声明的引用，不该回收"
    assert await store.get(ref, _ctx()) is not None


async def test_load_view_restores_blob_refs(store: SqlMemoryProvider) -> None:
    """读侧回显——否则第二次降级时第一次的 ref 无人认领（`_rebuild` 的累积逻辑）。"""
    ref = await store.put(b"declared-only", "image/png", _ctx())
    await store.ingest(
        MemoryEvent(
            kind=MemoryKind.CONVERSATION_TURN,
            scope=MemoryScope.TASK,
            address=MemoryAddress(session_id=_SESSION, task_id="t1", agent_id=_AGENT),
            role="user",
            timestamp=_BASE,
            content=[TextPart(text=f"[image {ref} media_type=image/png]")],
            blob_refs=[ref],
        ),
        _ctx(),
    )
    recs = await store.load_view(
        MemoryAddress(session_id=_SESSION, task_id="t1", agent_id=_AGENT),
        MemoryScope.TASK, _ctx(), kinds=[MemoryKind.CONVERSATION_TURN])
    assert recs[0].blob_refs == [ref]


async def test_structural_refs_are_not_duplicated_in_blob_refs(
        store: SqlMemoryProvider) -> None:
    """结构化 ref 已在 content 里，回显时不再重复塞进 blob_refs（否则 `_rebuild`
    的累积逻辑会把结构化 ref 也滚进补偿记录，一轮轮越滚越多）。"""
    ref = await store.put(b"structural-only", "image/png", _ctx())
    await store.ingest(_turn_with_refs(ref), _ctx())
    recs = await store.load_view(
        MemoryAddress(session_id=_SESSION, task_id="t1", agent_id=_AGENT),
        MemoryScope.TASK, _ctx(), kinds=[MemoryKind.CONVERSATION_TURN])
    assert recs[0].blob_refs == []
    assert collect_blob_refs(recs[0]) == [ref], "仍能从 content 采到"


# ══════════════════════════════════════════════════════════════════════════════
# 回收：collect_blobs
# ══════════════════════════════════════════════════════════════════════════════


async def test_fold_does_not_delete_bytes_synchronously(
        store: SqlMemoryProvider) -> None:
    """fold 是原子的「遗忘 + 补偿」，**绝不在其中同步删字节**。

    崩溃发生在 fold 与清扫之间应导致「blob 被孤立（泄漏）」，而不是「悬空 ref」——
    泄漏是安全方向，悬空会让 rehydrate 永久降级成 ``[image unavailable]``。
    """
    ref = await store.put(b"only-ref", "image/png", _ctx())
    eid = await store.ingest(_turn_with_refs(ref), _ctx())
    await store.fold([eid], [], _ctx())
    assert await _blob_shas(store) == {_sha(b"only-ref")}, "fold 不得动字节"
    assert await store.get(ref, _ctx()) is not None


async def test_collect_deletes_blob_after_sole_referencer_folded(
        store: SqlMemoryProvider) -> None:
    """简报覆盖 2：fold 掉唯一引用者后，清扫才真的删掉该 blob。"""
    ref = await store.put(b"only-ref", "image/png", _ctx())
    eid = await store.ingest(_turn_with_refs(ref), _ctx())
    await store.fold([eid], [], _ctx())

    assert await store.collect_blobs(now=_LATER) == 1
    assert await _blob_shas(store) == set()
    assert await store.get(ref, _ctx()) is None


async def test_collect_keeps_blob_with_one_live_referencer_among_three(
        store: SqlMemoryProvider) -> None:
    """简报覆盖 3（一图多引）：三条记录引用同 sha，fold 掉两条 → **不删**。

    **对照组是本条的命根子**：同一次清扫里另有一个真孤儿 ``dead`` 被删掉，
    且返回值恰为 1。没有它，一个「什么都不删」的实现能让「shared 还在」全部通过。
    """
    shared = await store.put(b"shared-image", "image/png", _ctx())
    await store.put(b"nobody-wants-me", "image/png", _ctx())  # 对照：真孤儿
    e1 = await store.ingest(_turn_with_refs(shared, event_id="e1"), _ctx())
    e2 = await store.ingest(_turn_with_refs(shared, event_id="e2"), _ctx())
    await store.ingest(_turn_with_refs(shared, event_id="e3"), _ctx())
    await store.fold([e1, e2], [], _ctx())

    deleted = await store.collect_blobs(now=_LATER)

    assert deleted == 1, "对照组失败：真孤儿没被删——本条的『shared 还在』会是假绿"
    assert _sha(b"nobody-wants-me") not in await _blob_shas(store)
    assert await store.get(shared, _ctx()) == (b"shared-image", "image/png"), (
        "还有一条活引用（e3）的 blob 被删了——内容寻址的去重被回收破坏")


async def test_collect_keeps_freshly_put_blob_not_yet_ingested(
        store: SqlMemoryProvider) -> None:
    """简报覆盖 4（时序窗口）：刚 ``put`` 还没 ``ingest`` 的 blob，宽限期内**不得删**。

    ``put`` 发生在 ``ingest`` 之前，故必然存在「blob 行已写入、尚无 event 引用它」
    的窗口。天真的查询会在窗口里把还没用上的图删掉。

    **对照组**：同一次清扫里，一个 ``created_at`` 被人为改到宽限期之外的真孤儿
    被删掉——证明这条断言不是「什么都不删」的重言式。
    """
    fresh = await store.put(b"just-put-me", "image/png", _ctx())
    await store.put(b"long-abandoned", "image/png", _ctx())  # 对照：超期孤儿
    await _age_blob(store, _sha(b"long-abandoned"), days=30)

    deleted = await store.collect_blobs()  # 真实 now：fresh 落在宽限期内

    assert deleted == 1, "对照组失败：超期孤儿没被删——『fresh 还在』会是假绿"
    assert _sha(b"long-abandoned") not in await _blob_shas(store)
    assert await store.get(fresh, _ctx()) == (b"just-put-me", "image/png"), (
        "刚 put 还没 ingest 的 blob 被回收删掉了——图在还没用上时就没了")


async def test_collect_deletes_true_orphan_past_grace_period(
        store: SqlMemoryProvider) -> None:
    """简报覆盖 5：超过宽限期、从未被任何 event 引用的真孤儿 → 删。"""
    ref = await store.put(b"never-used", "image/png", _ctx())
    assert await store.collect_blobs() == 0, "宽限期内不该删"

    assert await store.collect_blobs(now=_LATER) == 1
    assert await store.get(ref, _ctx()) is None


async def test_collect_does_not_delete_blob_referenced_by_another_tenant(
        store: SqlMemoryProvider) -> None:
    """简报覆盖 6（跨租户）：租户 A fold 掉自己的引用后，B 仍在引用同 sha → **不得删**。

    内容寻址跨租户去重（同字节 → 同 sha → 同一行），故回收**必须看见所有租户的
    活引用**；只看本租户会让 A 的一次 fold 删掉 B 还在用的图。

    对照组同上：同一次清扫里一个真孤儿被删掉，且删除数恰为 1。
    """
    ctx_a, ctx_b = _ctx("tenant-a"), _ctx("tenant-b")
    shared = await store.put(b"cross-tenant-image", "image/png", ctx_a)
    assert await store.put(b"cross-tenant-image", "image/png", ctx_b) == shared, (
        "跨租户同 sha 必须共享一行（put 侧取向）")
    orphan = await store.put(b"orphan-bytes", "image/png", ctx_a)

    ea = await store.ingest(_turn_with_refs(shared, event_id="ea"), ctx_a)
    await store.ingest(_turn_with_refs(shared, event_id="eb"), ctx_b)
    await store.fold([ea], [], ctx_a)

    deleted = await store.collect_blobs(now=_LATER)

    assert deleted == 1, "对照组失败：真孤儿没被删——跨租户断言会是假绿"
    assert await store.get(orphan, ctx_a) is None
    assert await store.get(shared, ctx_b) == (b"cross-tenant-image", "image/png"), (
        "租户 A 的 fold 删掉了租户 B 仍在引用的 blob")


async def test_collect_is_idempotent(store: SqlMemoryProvider) -> None:
    """清扫可随时重跑：第二遍删 0 条，不抛。"""
    await store.put(b"never-used", "image/png", _ctx())
    assert await store.collect_blobs(now=_LATER) == 1
    assert await store.collect_blobs(now=_LATER) == 0


async def test_reput_of_stale_blob_reopens_the_grace_window(
        store: SqlMemoryProvider) -> None:
    """``put`` 刷新 ``created_at``：重新 put 一份**旧**字节会重新打开 put→ingest 窗口。

    场景：图先被 e1 引用 → e1 被 fold → 同一张图又出现在新一轮对话里 → put 命中老行。
    若 ``created_at`` 还停在几天前，清扫会在新的 ingest 之前把它删掉，留下悬空 ref。
    （简报只对「从未被引用」加宽限期；这条就是「引用已全部失效」也必须加的实证。）

    对照组：不重新 put 的那份同样超期的 blob 被删掉了。
    """
    ref = await store.put(b"reused-image", "image/png", _ctx())
    other = await store.put(b"not-reused", "image/png", _ctx())
    e1 = await store.ingest(_turn_with_refs(ref, event_id="e1"), _ctx())
    await store.ingest(_turn_with_refs(other, event_id="e2"), _ctx())
    await store.fold([e1, "e2"], [], _ctx())
    await _age_blob(store, _sha(b"reused-image"), days=30)
    await _age_blob(store, _sha(b"not-reused"), days=30)

    await store.put(b"reused-image", "image/png", _ctx())  # 新一轮：命中老行，刷新时间

    deleted = await store.collect_blobs()

    assert deleted == 1, "对照组失败：没重新 put 的超期 blob 应被删"
    assert _sha(b"not-reused") not in await _blob_shas(store)
    assert await store.get(ref, _ctx()) == (b"reused-image", "image/png"), (
        "重新 put 过的 blob 在新的 ingest 落地前被删了——会留下悬空 ref")


async def _age_blob(store: SqlMemoryProvider, sha: str, *, days: int) -> None:
    """把某个 blob 的 ``created_at`` 往前拨——制造「确实超期」的对照，无需 sleep。"""
    from sqlalchemy import update
    async with store._factory() as db, db.begin():
        await db.execute(
            update(MemoryBlobModel)
            .where(MemoryBlobModel.sha == sha)
            .values(created_at=datetime.now(UTC) - timedelta(days=days)))


async def test_grace_period_is_configurable(tmp_path: Any) -> None:
    """宽限期是构造参数：宿主可按自己的 put→ingest 时延调，默认 24h。

    ``now`` 必须显式传，不能靠 ``collect_blobs()`` 自己读钟——本机实测
    ``datetime.now()`` 的粒度约 **0.5 ms**（相邻两次调用 100% 返回同值），
    而 ``put`` 与 ``collect`` 是背靠背两个 await，极易落在同一个 tick 内。
    那时 ``created_at == cutoff``，回收判据的**严格** ``<`` 不成立 → 一个都删不掉。
    实测该 flake 在全量跑里 4 次错 2 次。

    严格 ``<`` 是实现刻意的取舍（「宁可漏删，不可误删」），**不该为迁就测试放宽**；
    racy 的是「同一 tick 内既 put 又判过期」这个测法。故用 ``now`` 把时间轴钉死。
    """
    async with open_sqlite_memory(
            tmp_path / "m.db", blob_grace_period=timedelta(0)) as s:
        await s.put(b"zero grace", "image/png", _ctx())
        # grace=0 → cutoff = now；给一个明确晚于 created_at 的 now，判定不再依赖时钟粒度。
        assert await s.collect_blobs(now=datetime.now(UTC) + timedelta(seconds=1)) == 1
        # 反向对照：同一份数据、grace 足够长时**不该**被回收——
        # 否则「删了 1 个」可能只是因为回收无条件删，而非宽限期真的可配。
    async with open_sqlite_memory(
            tmp_path / "m.db", blob_grace_period=timedelta(days=1)) as s2:
        await s2.put(b"long grace", "image/png", _ctx())
        assert await s2.collect_blobs(now=datetime.now(UTC) + timedelta(seconds=1)) == 0


# ══════════════════════════════════════════════════════════════════════════════
# runtime 解析：显式注册 > memory provider > NullBlobStore
# ══════════════════════════════════════════════════════════════════════════════


async def test_registry_resolves_blob_store_to_sql_memory(
        store: SqlMemoryProvider) -> None:
    """简报覆盖 7（上半）：接了 SQL memory 时 ``get_blob_store()`` 解析到它本身。"""
    reg = ProviderRegistry()
    reg.register_memory(store)
    assert reg.get_blob_store() is store


def test_registry_falls_back_to_null_store_for_plain_memory() -> None:
    """简报覆盖 7（下半）+ 覆盖 8：纯内存 provider 据裁定 D6 不支持 blob → NullBlobStore。"""
    reg = ProviderRegistry()
    reg.register_memory(InMemoryMemoryProvider())
    got = reg.get_blob_store()
    assert isinstance(got, NullBlobStore)
    assert not got.can_externalize
    assert got is reg.get_blob_store(), "重复调用应返回同一个 NullBlobStore 实例"


def test_registry_without_any_memory_still_returns_null_store() -> None:
    """覆盖 8：没有 memory provider 时的宿主行为与本任务前逐字节一致。"""
    reg = ProviderRegistry()
    assert isinstance(reg.get_blob_store(), NullBlobStore)


async def test_explicit_registration_wins_over_memory_provider(
        store: SqlMemoryProvider) -> None:
    """显式 ``register_blob_store()`` 优先级最高——宿主想接别的存储时不被 memory 抢走。"""
    reg = ProviderRegistry()
    reg.register_memory(store)
    null = NullBlobStore()
    reg.register_blob_store(null)
    assert reg.get_blob_store() is null


async def test_memory_registered_after_first_lookup_is_still_resolved(
        store: SqlMemoryProvider) -> None:
    """接线顺序无关：先 ``get_blob_store()``（拿到 Null）、后 ``register_memory()``，
    再取仍须解析到 memory——回落结果**不得**被缓存进 ``_blob_store``。"""
    reg = ProviderRegistry()
    assert isinstance(reg.get_blob_store(), NullBlobStore)
    reg.register_memory(store)
    assert reg.get_blob_store() is store
