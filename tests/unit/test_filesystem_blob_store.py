"""FilesystemToolsProvider 的 BlobStore 实现：内容寻址、幂等、get 不抛。"""

import asyncio

import pytest

from ctx_weft.protocols.context import ProviderContext
from ctx_weft.protocols.filesystem import BLOB_REF_PREFIX
from ctx_weft.providers.capability_filesystem.provider import FilesystemToolsProvider


def _provider(tmp_path):
    p = FilesystemToolsProvider()
    p.register_session("s1", str(tmp_path))
    return p


def _ctx() -> ProviderContext:
    return ProviderContext(session_id="s1")


def test_put_returns_ref_with_blob_prefix(tmp_path):
    p = _provider(tmp_path)
    ref = asyncio.run(p.put(b"hello world", "image/png", _ctx()))
    assert ref.startswith(BLOB_REF_PREFIX)


def test_put_is_content_addressed_same_bytes_same_ref(tmp_path):
    p = _provider(tmp_path)
    ref1 = asyncio.run(p.put(b"same bytes", "image/png", _ctx()))
    ref2 = asyncio.run(p.put(b"same bytes", "image/png", _ctx()))
    assert ref1 == ref2


def test_put_is_idempotent_only_one_file_on_disk(tmp_path):
    p = _provider(tmp_path)
    asyncio.run(p.put(b"dedup me", "image/jpeg", _ctx()))
    asyncio.run(p.put(b"dedup me", "image/jpeg", _ctx()))

    blobs_dir = tmp_path / "blobs"
    written = [f for f in blobs_dir.rglob("*") if f.is_file()]
    # 内容文件 + 可能的 .meta 伴生文件；不管选哪种存储方式，内容文件本身只应有一份。
    content_files = [f for f in written if not f.name.endswith(".meta")]
    assert len(content_files) == 1


def test_get_returns_original_bytes_and_media_type(tmp_path):
    p = _provider(tmp_path)
    ref = asyncio.run(p.put(b"round trip payload", "image/webp", _ctx()))
    result = asyncio.run(p.get(ref, _ctx()))
    assert result == (b"round trip payload", "image/webp")


def test_get_nonexistent_ref_returns_none_not_raise(tmp_path):
    p = _provider(tmp_path)
    result = asyncio.run(p.get(f"{BLOB_REF_PREFIX}nonexistent", _ctx()))
    assert result is None


def test_put_without_registered_workspace_raises():
    p = FilesystemToolsProvider()
    with pytest.raises(RuntimeError):
        asyncio.run(p.put(b"no workspace", "image/png", ProviderContext(session_id="unregistered")))


def test_put_conflicting_media_type_first_writer_wins(tmp_path):
    """相同 bytes、不同 media_type：内容寻址 → ref 相同。语义选择：先写入者胜。

    理由：put 幂等意味着「已存在则不重写」——沿用这条既有语义即可覆盖 media_type，
    不需要额外判断分支；且避免了「同一份数据被谁最后 put 就变成谁的类型」这种
    依赖调用顺序、难以复现的行为。
    """
    p = _provider(tmp_path)
    ref1 = asyncio.run(p.put(b"ambiguous type", "image/png", _ctx()))
    ref2 = asyncio.run(p.put(b"ambiguous type", "image/jpeg", _ctx()))
    assert ref1 == ref2

    result = asyncio.run(p.get(ref1, _ctx()))
    assert result == (b"ambiguous type", "image/png")


def test_get_without_registered_workspace_returns_none_not_raise():
    """get 与 put 的失败语义刻意不对称：put 抛，get 返回 None。

    这条契约是 Task 3 的 rehydrate 依赖的——rehydrate 跑在 gateway 的出网路径上，
    若 get 在此抛异常，一个未登记的 workspace 会掀掉整个 LLM 请求，而不是退化成
    「这张图取不回来」。故此处必须钉死。
    """
    p = FilesystemToolsProvider()
    result = asyncio.run(p.get(f"{BLOB_REF_PREFIX}whatever", ProviderContext(session_id="unregistered")))
    assert result is None


def test_get_ref_without_blob_prefix_returns_none_not_raise(tmp_path):
    """非 blob: 前缀的字符串不是本 store 的 ref——返回 None，不抛、不当成 sha 去拼路径。

    前缀检查不只是整洁性代码，它挡的是一条真实的 Windows 攻击面。变异验证时把这条
    检查去掉后，"http://example.com/x.png" 被当成 sha 拼进路径，pathlib 将
    //example.com/x.png 解释为 UNC 网络路径并**真的发起了网络访问**
    （OSError WinError 64）。即：缺了前缀检查，一个受污染的 ref 字符串能让
    blob 读取变成任意 SMB 外连。故此测试是安全护栏，删改需谨慎。
    """
    p = _provider(tmp_path)
    assert asyncio.run(p.get("http://example.com/x.png", _ctx())) is None
    assert asyncio.run(p.get("", _ctx())) is None
