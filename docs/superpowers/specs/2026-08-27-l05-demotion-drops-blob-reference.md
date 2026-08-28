# 缺陷：L0.5 降级让 blob 引用归零，图在一个宽限期后永久丢失

> 状态：**已修复**（2026-08-27）
> 影响：`feat/multimodal` 分支，Phase 4 引入
> 复现测试：`tests/unit/test_l05_demotion_blob_lifecycle.py`（`xfail(strict=True)`）
> 与双 blob store 设计**无关**——这是 memory 侧内部的问题，两个 store 分开之后依然存在。

---

## 1. 现象

L0.5 降级（`media.demote_for_budget`）把真图换成含 ref 的文本占位并落库，承诺模型之后
可用 `media:get_image` 取回。实测：降级之后该 blob 的活引用归零，`collect_blobs` 在
宽限期（默认 24h）后删掉字节，`get_image` 只能返回「字节取不到」的说明文本。

复现输出（宽限期设 0 以剔除时间因素）：

```
1. put  -> blob:ba3634a3…
2. ingest 后 collect_blobs -> 删了 0 条          ← 活引用在，正确
   get 字节: 拿到
3. L0.5 降级 1 张图（真图 -> 含 ref 的文本占位，落库）
   视图里的占位: [image blob:ba3634a3863bc5109b1ff57fe0bc8eeb…
4. 降级后 collect_blobs -> 删了 1 条              ← 缺陷
5. media:get_image 要的字节: None —— 图永久丢失
```

## 2. 根因

引用边只认**结构化的** ref part：

```python
# SqlMemoryProvider.ingest
for ref in extract_blob_refs(event.content):
    db.add(MemoryBlobRefModel(event_id=event_id, sha=ref[len(BLOB_REF_PREFIX):]))
```

`extract_blob_refs` 的判据是 `_is_ref_part`——要求 `source_type == "ref"` 或 `data` 以
`blob:` 开头。而 L0.5 的补偿记录（`media/fold.py::_rebuild`）产出的是：

```python
TextPart(text=encode_image_placeholder(ref, media_type))
```

`TextPart` 两条判据都不满足，ref 从结构化字段掉进了**自由文本**。于是：

1. 原记录被 `fold` 标 `is_superseded = True` → 它的引用边失活；
2. 补偿记录写入**零条**引用边；
3. 该 sha 的活引用归零，`collect_blobs` 的第二条判据满足；
4. 过宽限期后字节被删。

`ingest` 那段注释精确预言了这个失败模式，只是没料到触发方式：

> 判据一旦分叉，「哪些 blob 还活着」就会和「出网时哪些 part 会被 rehydrate」对不上，
> 而那正好是「回收删掉了还在用的图」的成因。

这里不是判据分叉，是**占位形态跳出了判据的视野**——L0.5 主动把 `ImagePart(ref)` 降成
`TextPart`，引用边跟着蒸发。

## 3. 为什么现有测试没抓到

- `test_sql_blob_store.py` 测 blob 层契约（put 幂等 / get 不抛 / 回收判据），不涉及 L0.5；
- `test_media_fold.py` 测降级逻辑（选谁降、同刻组保序、占位格式），不涉及回收；
- `test_media_get_image.py` 测取回，但用的是**未经回收**的 store。

**没有任何用例把「降级 → 回收 → 取回」串起来跑。** 本次补的复现测试正是这条链。

## 4. 影响评估

L0.5 被排在 L1/L2/L3 之前的三条理由（子设计 §6）中，第三条是：

> **可逆**——占位仍在原位，模型随时 `media:get_image` 取回。L1/L2/L3 折的是记录本身，
> 一旦执行位置就没了；所以先花可逆的额度。

这个前提在一个宽限期后不成立。用户看到的表现是：一天前的图，模型说「我去把它取回来」，
然后拿到一句「字节取不到」。而占位文本仍在、ref 仍在、`_locate` 仍能命中——**失败发生
在最后一步，且之前的每一步都表现正常**，诊断上很不友好。

## 5. 解法：让 ref 永远以结构化形式可采集（已裁定 2026-08-27）

**根本判断：这不是机制选型问题，是 ref 逃出了结构化表示。** 换成引用计数或 owner set
也救不了——`retain` 的时候同样得先知道补偿记录引用了哪个 sha，而那个 ref 当时只存在于
一段自由文本里。真正要修的是「让 ref 不逃」；修完之后，现有的 mark-and-sweep 就是最省
的机制，一行 SQL 都不用改。

### 5.1 blob store 保持纯 CAS

```python
class MemoryBlobStore(ABC):      # EventBlobStore 同形
    can_externalize -> bool
    put(data, media_type, ctx) -> ref
    get(ref, ctx) -> tuple[bytes, str] | None
```

**不加 `retain` / `release`，不记任何引用。** 它只回答「这个 sha 的字节是什么」。

选 mark-and-sweep 而非 owner set / 引用计数的理由：retain/release 的**配对正确性**是
分布式系统里最易出 bug 的地方——漏 release 永久泄漏、多 release 数据丢失，两者都难以
事后发现。mark-and-sweep 没有配对，活引用集合随时可从当前状态重新推导，错了下一轮自愈。
Git（`git gc` 从 refs 遍历）、IPFS（pinning）、Docker registry 都是这个取向。

### 5.2 `MemoryEvent` / `MemoryRecord` 加 `blob_refs` 字段 ← 关键

```python
blob_refs: list[str] = field(default_factory=list)
```

**L0.5 补偿记录填上它降级掉的那些 ref。** 于是记录里同时有两样东西，各服务一条路径：

| 载体 | 给谁看 | 谁解析 |
|---|---|---|
| `content` 里的文本占位 | **模型**（照着它调 `media:get_image`） | `refs.py`（仍是唯一知道占位格式的地方） |
| `blob_refs` 里的结构化 ref | **GC** | mark 函数 |

两条路径互不解析对方的格式。其它写侧一行不用改——普通 `ImagePart(source_type="ref")`
仍被自动采集。

### 5.3 mark 判据收成一个函数

```python
def collect_blob_refs(event) -> list[str]:
    """结构化 ref part ∪ event.blob_refs。不解析任何文案。"""
```

取代 provider 里直接调 `extract_blob_refs`。判据从此**只看结构化字段**，不随占位文案
演进而失效——这正是 §2 那段注释担心的「判据分叉」，只是修在了正确的层次。

### 5.4 sweep 与宽限期原样保留

`SqlMemoryProvider.collect_blobs` 的 SQL **一个字都不改**。`memory_blob_refs` 仍是 mark
结果的物化（ingest 时写，避免 sweep 去解析 JSON 全表扫），改的只是**写入它的判据**
变完整了。

宽限期的论证不受影响：它解决的是 `put` 与首次 ingest 之间的窗口，与 mark 判据正交
（同 Git 的 `gc.pruneExpire`）。

### 5.5 event 侧同构

`EventBlobStore` 同样是纯 CAS。事件侧 payload 里的 ref 已经是结构化 dict
（`{"type":"image","source_type":"ref","data":"blob:…"}`），host 直接扫即可。清理策略
由 host 按事件保留策略定，与 memory 侧独立（见 `2026-08-27-dual-blob-store-design.md` §9）。

### 5.6 无存量迁移

`feat/multimodal` 尚未上线，不存在已降级的生产数据。**不写迁移脚本。**

## 6. 已完成

复现测试 `tests/unit/test_l05_demotion_blob_lifecycle.py`，缺陷用例已转 XPASS，
`strict=True` 触发后删掉了标记。两个对照组把失败精确定位在引用边，而非降级逻辑或占位格式。

## 7. 已实施

实施落点：
- `protocols/memory.py`：`MemoryEvent` / `MemoryRecord` 各加 `blob_refs: list[str]` 字段（Task 1）
- `core/content.py`：`collect_blob_refs(event)` 为 mark 的单一真源（Task 2）
- `media/fold.py::_rebuild`：补偿记录的 `blob_refs` 填上降级掉的 ref（Task 3）
- `providers/memory_sql.py`：
  - mark 函数改用 `collect_blob_refs`（Task 4）
  - `_declared_refs` 反查补偿记录的 `blob_refs`，`load_view` 回显（Task 4）

SQL、宽限期、blob 协议均不动。
