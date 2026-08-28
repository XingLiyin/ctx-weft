# 缺陷：L0.5 降级让 blob 引用归零，图在一个宽限期后永久丢失

> 状态：**已复现，解法待定**（2026-08-27 立项）
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

## 5. 解法方向（待裁定）

| | 做法 | 问题 |
|---|---|---|
| A | `extract_blob_refs` 也扫占位文本里的 `blob:` | 归一层要知道占位格式，撞 `refs.py`「本仓唯一知道占位长什么样的地方」 |
| B | 补偿记录保留 `ImagePart(source_type="ref")`，加 `demoted=True` 标记 | 引用边自然保住，但 `rehydrate_content` / adapter 会把它当真图取回出网，等于降级白做 |
| C | provider 侧额外正则扫 content 文本 | 判据分叉，正是 §2 那段注释警告的事 |
| **D**（倾向） | `MemoryEvent` 加显式 `blob_refs: list[str]` 字段，写侧填 | 引用不再靠内容形态推断；`fold.py` 降级时把 ref 显式带上。协议多一个字段 |

D 的额外好处：`MemoryProvider` 协议可以把「引用边怎么建」从「provider 去猜内容」变成
「调用方显式声明」，第三方 provider 不必复刻 `_is_ref_part` 的判据。

需要一并想清楚的：

- **存量数据**：已降级过的记录引用边已经丢了，其 blob 可能已被回收。迁移时能否从占位
  文本反解 ref 补建引用边？（一次性脚本，不是常态判据——可以接受 A 的做法。）
- **协议兼容**：`blob_refs` 缺省为空时，provider 是否回落到扫内容？回落会让两条路径并存；
  不回落则所有写侧调用点都必须填。
- **`extract_blob_refs` 的去留**：若走 D，它在 provider 侧还有没有调用方。

## 6. 本次只做

1. 复现测试 `tests/unit/test_l05_demotion_blob_lifecycle.py`，`xfail(strict=True)` 钉住。
   修好后测试自动 XPASS，`strict=True` 会让它转红，提醒删掉标记——防止悄悄修好又悄悄退化。
2. 本文档。

**不改实现**——解法要先裁定（§5）。
