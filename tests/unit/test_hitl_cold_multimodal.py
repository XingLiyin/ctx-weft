"""冷 HITL 决定的 event → memory ref 转换。

`fold_cold_hitl_decision` 从事件日志还原 `req.message`，而事件 payload 里存的是
**event 侧的 ref**（EventBlobStore 的命名空间）。这个 req 会被 ask_user / approval
消费并最终进 memory——直接透传就是往 memory 里写一个永远打不开的引用。
"""

import pytest

from ctx_weft.protocols import ImagePart, ProviderContext, TextPart


class _EventBlobs:
    """event 侧 store：按 event ref 存字节。"""
    can_externalize = True

    def __init__(self, data=None):
        self._data = data or {}

    async def put(self, data, media_type, ctx):
        raise NotImplementedError                       # 本测试不写入 event 侧

    async def get(self, ref, ctx):
        raw = self._data.get(ref)
        return (raw, "image/png") if raw is not None else None


class _MemoryBlobs:
    """memory 侧 store：put 产出自己命名空间里的 ref。"""
    can_externalize = True

    def __init__(self):
        self.puts = []

    async def put(self, data, media_type, ctx):
        self.puts.append(data)
        return "blob:" + "m" * 64

    async def get(self, ref, ctx):
        return None


EVENT_REF = "blob:" + "e" * 64
MEMORY_REF = "blob:" + "m" * 64
PNG = b"\x89PNG\r\n\x1a\n" + b"payload"


def _msg_with_event_ref():
    return [TextPart(text="看这个"),
            ImagePart(data=EVENT_REF, media_type="image/png", source_type="ref")]


@pytest.fixture
def cold_runtime(monkeypatch):
    """造一个只够跑 _cold_hitl_decision 的壳：桩掉事件读取与 fold，注入两个 blob store。"""
    from types import SimpleNamespace

    from ctx_weft.core.runtime import CtxWeftRuntime
    from ctx_weft.protocols.hitl import HITL_OUTCOME_ACCEPTED, HitlRequest

    def _make(*, event_blobs, folded_message):
        rt = object.__new__(CtxWeftRuntime)          # 不跑 __init__（要一整套 provider）
        memory_blobs = _MemoryBlobs()
        rt.providers = SimpleNamespace(
            get_event_blob_store=lambda: event_blobs,
            get_memory_blob_store=lambda: memory_blobs,
        )
        rt.event_store = SimpleNamespace(
            read_session_events_of_types=lambda sid, types: _aio([]),
        )

        def _fold(events, tool_call_id):
            if folded_message is None:
                return None
            req = HitlRequest(id="h1", form="question", session_id="s1", task_id="t1",
                              tool_call_id=tool_call_id)
            req.resolve(HITL_OUTCOME_ACCEPTED)
            req.message = folded_message
            return req

        monkeypatch.setattr("ctx_weft.core.control.reducers.fold_cold_hitl_decision", _fold)
        monkeypatch.setattr(CtxWeftRuntime, "_tenant_for_session",
                            lambda self, sid: _aio("tn"))
        return rt, memory_blobs

    return _make


async def _aio(value):
    return value


@pytest.mark.asyncio
async def test_cold_decision_converts_event_ref_to_memory_ref(cold_runtime):
    """🔴 本任务存在的理由：还原出来的 event ref 必须被换成 memory ref。"""
    rt, _ = cold_runtime(event_blobs=_EventBlobs({EVENT_REF: PNG}),
                         folded_message=_msg_with_event_ref())
    req = await rt._cold_hitl_decision("s1", "tc1")

    images = [p for p in req.message if not hasattr(p, "text")]
    assert len(images) == 1
    assert images[0].data == MEMORY_REF, "必须是 memory 侧 ref，不是 event 侧的"
    assert images[0].source_type == "ref"


@pytest.mark.asyncio
async def test_cold_decision_degrades_loudly_when_event_blob_is_gone(cold_runtime, caplog):
    """event blob 取不回 → 降级成确定性文本占位，并记 error；绝不让解不开的 ref 流下去。"""
    rt, _ = cold_runtime(event_blobs=_EventBlobs({}),      # 字节没了
                         folded_message=_msg_with_event_ref())
    req = await rt._cold_hitl_decision("s1", "tc1")

    assert all(hasattr(p, "text") for p in req.message), "不得留下任何 ImagePart"
    assert "[image unavailable: image/png]" in "".join(p.text for p in req.message)


@pytest.mark.asyncio
async def test_plain_text_decision_is_untouched(cold_runtime):
    """纯文本零成本直通：返回的就是折叠出来的那个对象（Global Constraint 第一条）。"""
    rt, _ = cold_runtime(event_blobs=_EventBlobs({}), folded_message="use postgres")
    req = await rt._cold_hitl_decision("s1", "tc1")
    assert req.message == "use postgres"


@pytest.mark.asyncio
async def test_no_decision_returns_none(cold_runtime):
    """没有可用决定时照旧返回 None，转换逻辑不得把它变成别的东西。"""
    rt, _ = cold_runtime(event_blobs=_EventBlobs({}), folded_message=None)
    assert await rt._cold_hitl_decision("s1", "tc1") is None
