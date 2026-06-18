"""TaskQueue.drain_pending: remove and return all pending task ids."""

from ctx_weft.core.orchestrator.task_queue import QueueEntry, TaskQueue


def test_drain_pending_returns_and_clears_entries():
    q = TaskQueue()
    q.push(QueueEntry(task_id="a", session_id="s"))
    q.push(QueueEntry(task_id="b", session_id="s"))
    drained = q.drain_pending()
    assert set(drained) == {"a", "b"}
    assert q.has_pending() is False
    assert q.drain_pending() == []      # idempotent on empty


def test_drain_pending_leaves_running_untouched():
    q = TaskQueue()
    q.push(QueueEntry(task_id="a", session_id="s"))
    q.mark_running("r")                 # running set is independent of _entries
    drained = q.drain_pending()
    assert drained == ["a"]
    assert "r" in q._running
