import json
from datetime import datetime, timezone

from ctx_weft.core.content import content_to_jsonable
from ctx_weft.core.control.converters import task_from_projection
from ctx_weft.core.control.reducers import reduce_events, serialize_view
from ctx_weft.core.events.types import Event, EventType
from ctx_weft.core.orchestrator.task_manager import _task_payload
from ctx_weft.core.state.models import Task
from ctx_weft.core.utils import now_utc
from ctx_weft.protocols import ImagePart, TextPart


def _content():
    return [TextPart(text="看这张图"), ImagePart(data="ZGF0YQ==", media_type="image/png")]


def _ts():
    return datetime(2026, 8, 23, tzinfo=timezone.utc)


def _ev(seq: int, type_: str, **payload) -> Event:
    """与 tests/unit/test_snapshot_recovery.py 同一构造惯例。"""
    task_id = payload.pop("task_id", None)
    return Event(
        id=f"evt_{seq:04d}", run_id="run_1", sequence=seq, session_id="s1",
        type=type_, timestamp=_ts(), task_id=task_id, payload=payload,
    )


# ── 写侧：payload 必须是可 json 化的形态 ──────────────────────────────────

def test_task_payload_is_json_serializable():
    """ContentPart 是普通 dataclass，直接进 payload 会让宿主的 json 持久化炸掉。"""
    task = Task(
        id="tsk_1", session_id="s1", status="ACTIVE", tenant_id="default",
        assigned_agent_id="a1", creator_agent_id="a1",
        title="T", description="d", user_prompt=_content(), created_at=now_utc(),
    )
    payload = _task_payload(task)
    json.dumps(payload)  # 不抛即通过
    assert payload["task"]["user_prompt"] == content_to_jsonable(_content())


def test_task_payload_plain_text_unchanged():
    task = Task(
        id="tsk_1", session_id="s1", status="ACTIVE", tenant_id="default",
        assigned_agent_id="a1", creator_agent_id="a1",
        title="T", description="d", user_prompt="纯文本", created_at=now_utc(),
    )
    assert _task_payload(task)["task"]["user_prompt"] == "纯文本"


# ── 读侧：事件回放 → 投影 → 运行时模型，完整还原 ─────────────────────────

def test_task_user_prompt_survives_event_replay():
    """跨重启的核心保证。"""
    events = [
        _ev(1, EventType.SESSION_CREATED, user_prompt="看这张图",
            template_id="tmpl_a", root_agent_id="agt_root"),
        _ev(2, EventType.TASK_CREATED, task={
            "id": "tsk_1", "status": "ACTIVE", "title": "T1",
            "assigned_agent_id": "agt_root", "creator_agent_id": "agt_root",
            "user_prompt": content_to_jsonable(_content()),
        }),
    ]
    view = reduce_events(events, run_id="s1")
    task = task_from_projection(view.tasks["tsk_1"])
    assert task.user_prompt == _content(), "多模态 prompt 必须经事件回放完整还原"


def test_snapshot_roundtrip_preserves_parts():
    """快照路径（serialize_view）与事件回放路径必须同样无损。"""
    events = [
        _ev(1, EventType.SESSION_CREATED, user_prompt="看这张图",
            template_id="tmpl_a", root_agent_id="agt_root"),
        _ev(2, EventType.TASK_CREATED, task={
            "id": "tsk_1", "status": "ACTIVE", "title": "T1",
            "assigned_agent_id": "agt_root", "creator_agent_id": "agt_root",
            "user_prompt": content_to_jsonable(_content()),
        }),
    ]
    view = reduce_events(events, run_id="s1")
    blob = serialize_view(view)
    json.dumps(blob)  # 快照必须可 json 化
    from ctx_weft.core.control.reducers import deserialize_view
    restored = deserialize_view(blob)
    assert task_from_projection(restored.tasks["tsk_1"]).user_prompt == _content()
