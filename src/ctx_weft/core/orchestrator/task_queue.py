"""TaskQueue：LIFO 调度 + blocked DAG 支持。

设计文档 §4（Phase 4 §4.1）。
"""

from __future__ import annotations

import logging
from collections import deque
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)


@dataclass
class QueueEntry:
    task_id: str
    session_id: str
    priority: int = 5
    blocked_by: set[str] = field(default_factory=set)


class TaskQueue:
    """In-process LIFO queue with DAG dependency support.

    Rules:
    - Tasks are scheduled LIFO (stack) when not blocked.
    - A task is blocked if any of its blocked_by task IDs are still active.
    - When a task completes, its dependents are unblocked automatically.
    - pop() returns the most recently pushed non-blocked task.
    """

    def __init__(self) -> None:
        self._entries: list[QueueEntry] = []
        self._completed: set[str] = set()
        self._running: set[str] = set()

    def push(self, entry: QueueEntry) -> None:
        # Remove completed dependencies at push time
        entry.blocked_by -= self._completed
        self._entries.append(entry)
        logger.debug("TaskQueue.push: %s (blocked_by=%s)", entry.task_id, entry.blocked_by)

    def pop(self) -> QueueEntry | None:
        """Return the topmost non-blocked, non-running task (LIFO)."""
        for i in range(len(self._entries) - 1, -1, -1):
            entry = self._entries[i]
            # Refresh: remove deps that have since completed
            entry.blocked_by -= self._completed
            if not entry.blocked_by and entry.task_id not in self._running:
                self._entries.pop(i)
                self._running.add(entry.task_id)
                return entry
        return None

    def mark_running(self, task_id: str) -> None:
        self._running.add(task_id)

    def unmark_running(self, task_id: str) -> None:
        """Remove from running set without marking as complete/failed. Used for retries."""
        self._running.discard(task_id)

    def unmark_completed(self, task_id: str) -> None:
        """Remove from completed set so a reopened task can be scheduled again."""
        self._completed.discard(task_id)

    def mark_complete(self, task_id: str) -> None:
        self._running.discard(task_id)
        self._completed.add(task_id)
        # Refresh all blocked entries
        for entry in self._entries:
            entry.blocked_by.discard(task_id)
        logger.debug("TaskQueue.mark_complete: %s, %d pending", task_id, len(self._entries))

    def mark_failed(self, task_id: str) -> None:
        self._running.discard(task_id)
        self._completed.add(task_id)  # treat failed as done for unblocking

    def cancel(self, task_id: str) -> bool:
        for i, entry in enumerate(self._entries):
            if entry.task_id == task_id:
                self._entries.pop(i)
                return True
        return False

    def drain_pending(self) -> list[str]:
        """Remove all queued (pending) entries; return their task ids.

        Does not touch ``_running`` / ``_completed`` — only clears what hasn't started.
        """
        ids = [e.task_id for e in self._entries]
        self._entries.clear()
        return ids

    def pending_count(self) -> int:
        return len(self._entries)

    def has_pending(self) -> bool:
        return bool(self._entries)

    def all_blocked(self) -> bool:
        """True if every pending task is blocked (deadlock signal)."""
        if not self._entries:
            return False
        return all(bool(e.blocked_by) for e in self._entries)

    def peek_all(self) -> list[QueueEntry]:
        return list(self._entries)
