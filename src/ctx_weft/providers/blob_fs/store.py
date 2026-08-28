"""FsBlobStore：文件系统内容寻址 blob 实现（示例 / 可直接用的宿主实现）。"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import logging
import os
import re
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

from ctx_weft.protocols import BLOB_REF_PREFIX, EventBlobStore, MemoryBlobStore, ProviderContext

logger = logging.getLogger(__name__)

# sha256 hexdigest 严格是 64 位小写十六进制。这个正则同时是唯一的路径安全护栏：
# 只允许这个字符集意味着 "." "/" "\\" 一律不可能出现，`..`、绝对路径、UNC
# （如 "http://example.com/x.png" 被 pathlib 当 "\\example.com\x.png" 解析发起 SMB 外连
# 的那类攻击）在拼路径之前就已经被拒绝——不是靠"看起来像"过滤，是靠字符集穷举。
_SHA_RE = re.compile(r"^[0-9a-f]{64}$")


def _safe_sha(sha: str) -> str | None:
    """校验 sha 是否可安全拼进文件路径；非法一律返回 None（调用方据此返回 get() -> None）。"""
    return sha if _SHA_RE.fullmatch(sha) else None


class FsBlobStore(MemoryBlobStore, EventBlobStore):
    """文件系统内容寻址 blob 实现，**同时满足两个契约**——这正是「实现可以偷懒」的形态。

    ⚠️ 一个类满足两个契约，不等于两个契约可以合并。它们各自定义、类型无关，语义会
    各自演进（最明显的是回收锚点：memory 侧是记录 is_superseded，event 侧是事件保留
    策略）。本类只是**碰巧**两边都能用。

    ⚠️ **共用一个实例时，`collect` 的 live_refs 必须同时含两侧的活引用**（spec §9）。
    只喂 memory 侧的活引用会删掉事件流仍需要的字节。这是共用实现自身的责任，core
    不代管——想省心就分开部署两个实例，各按各的策略回收。

    路径安全：sha 经 `_safe_sha` 校验（仅 64 位十六进制）后才拼进路径，`..` / 绝对路径
    / UNC 一律在此被拒，`get` 返回 None。
    """

    def __init__(self, root: Path, *, grace_period: timedelta = timedelta(hours=24)) -> None:
        self._root = Path(root)
        self._grace_period = grace_period

    # ── 路径布局 ──────────────────────────────────────────────────────────────

    def _paths(self, sha: str) -> tuple[Path, Path, Path]:
        """sha -> (子目录, 数据文件, sidecar .meta 文件)。调用方须先经 `_safe_sha` 校验。"""
        subdir = self._root / sha[:2]
        return subdir, subdir / sha, subdir / f"{sha}.meta"

    # ── MemoryBlobStore / EventBlobStore ────────────────────────────────────

    async def put(self, data: bytes, media_type: str, ctx: ProviderContext) -> str:
        """内容寻址存字节，返回 ``blob:<sha256>``；同字节幂等（已存在则只刷新 mtime）。

        已存在时**不重写内容与 media_type**（先写入者胜），但仍刷新 mtime——语义同
        ``SqlMemoryProvider.put``：「最后一次有人声称要用它」，是 `collect` 宽限期
        赖以成立的前提（见该类同名 docstring）。

        首次写入走临时文件 + ``os.replace`` 的原子落盘，避免半截文件被并发的
        `get` 读到；本仓 store 协议全是 async，阻塞 IO 经 `asyncio.to_thread` 执行。
        """
        sha = hashlib.sha256(data).hexdigest()
        await asyncio.to_thread(self._put_sync, sha, data, media_type)
        return f"{BLOB_REF_PREFIX}{sha}"

    def _put_sync(self, sha: str, data: bytes, media_type: str) -> None:
        subdir, data_path, meta_path = self._paths(sha)
        subdir.mkdir(parents=True, exist_ok=True)
        if data_path.exists():
            # 已存在：只 touch mtime，内容与 meta 均不动。
            os.utime(data_path, None)
            if meta_path.exists():
                os.utime(meta_path, None)
            return
        self._atomic_write(subdir, data_path, data)
        self._atomic_write(subdir, meta_path, (media_type or "").encode("utf-8"))

    @staticmethod
    def _atomic_write(subdir: Path, dest: Path, payload: bytes) -> None:
        """先写临时文件再 ``os.replace``：避免半截文件被并发的 `get` 读到。

        临时文件与目标同目录（同一文件系统），`os.replace` 才能保证原子性——
        跨文件系统 rename 在 Windows 上会退化为拷贝，失去原子保证。
        """
        fd, tmp_name = tempfile.mkstemp(dir=subdir, prefix=f".{dest.name}.", suffix=".tmp")
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(payload)
            os.replace(tmp_name, dest)
        except BaseException:
            with contextlib.suppress(OSError):
                os.remove(tmp_name)
            raise

    async def get(self, ref: str, ctx: ProviderContext) -> tuple[bytes, str] | None:
        """按 ref 取回 ``(data, media_type)``；ref 不存在 / 已回收 / 形态不对 → ``None``。

        前缀检查 + `_safe_sha` 校验都在触碰文件系统**之前**完成——sha 校验不只是
        契约条，在文件系统实现里它是唯一的路径安全护栏（见模块级 `_SHA_RE` 注释）。
        `media_type` 缺失（sidecar 丢失或为空）回落 ``"application/octet-stream"``。
        """
        if not ref.startswith(BLOB_REF_PREFIX):
            return None
        sha = _safe_sha(ref[len(BLOB_REF_PREFIX):])
        if sha is None:
            return None
        return await asyncio.to_thread(self._get_sync, sha)

    def _get_sync(self, sha: str) -> tuple[bytes, str] | None:
        _subdir, data_path, meta_path = self._paths(sha)
        try:
            data = data_path.read_bytes()
        except OSError:
            return None
        try:
            media_type = meta_path.read_text(encoding="utf-8").strip()
        except OSError:
            media_type = ""
        return data, media_type or "application/octet-stream"

    # ── 回收 ──────────────────────────────────────────────────────────────────

    async def collect(self, live_refs: set[str], *, now: datetime | None = None) -> int:
        """回收无人引用的 blob 文件，返回删除数。**延迟、幂等的清扫**，不在写路径上。

        判定（两条同时成立才删，宁可漏删不可误删）：

        1. **不在 ``live_refs`` 中**——`live_refs` 里的每个 ref 剥掉前缀、校验合法后
           得到活跃 sha 集合。**共用一个实例（同时注册为 MemoryBlobStore 与
           EventBlobStore）时，调用方必须把两侧的活引用合并后一起传进来**：只喂
           memory 侧会把事件流仍需要的字节当孤儿删掉，反之亦然（本类 docstring
           已警示，这里是它在代码里的落点）。
        2. **mtime 早于 ``now - grace_period``**。这是正确性要求，不是优化——理由
           与 ``SqlMemoryProvider.collect_blobs`` 相同：`put` 与真正建立引用之间
           必然存在一个窗口（中间隔着 HITL park、重试、崩溃后重放），只按
           「无引用」判会在窗口内把还没用上的字节删掉。`put` 对已存在的 sha 会
           刷新 mtime（「最后一次有人声称要用它」），所以重新 put 一份被引用者
           已全部失效的旧字节，也会重新打开这个窗口——宽限期对这种情况同样保护。
        """
        cutoff = (now or datetime.now(UTC)) - self._grace_period
        live_shas = {
            sha for ref in live_refs
            if ref.startswith(BLOB_REF_PREFIX)
            and (sha := _safe_sha(ref[len(BLOB_REF_PREFIX):])) is not None
        }
        deleted = await asyncio.to_thread(self._collect_sync, live_shas, cutoff)
        if deleted:
            logger.info("FsBlobStore.collect: reclaimed %d unreferenced blob(s)", deleted)
        return deleted

    def _collect_sync(self, live_shas: set[str], cutoff: datetime) -> int:
        if not self._root.exists():
            return 0
        deleted = 0
        for subdir in self._root.iterdir():
            if not subdir.is_dir() or not re.fullmatch(r"[0-9a-f]{2}", subdir.name):
                continue
            for entry in subdir.iterdir():
                if not entry.is_file() or not _SHA_RE.fullmatch(entry.name):
                    continue  # 跳过 .meta / 遗留临时文件等非数据文件
                sha = entry.name
                if sha in live_shas:
                    continue
                try:
                    mtime = datetime.fromtimestamp(entry.stat().st_mtime, tz=UTC)
                except OSError:
                    continue
                if mtime >= cutoff:
                    continue
                try:
                    entry.unlink()
                except OSError:
                    continue
                with contextlib.suppress(OSError):
                    (subdir / f"{sha}.meta").unlink()
                deleted += 1
        return deleted
