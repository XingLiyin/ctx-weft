"""CancelToken / PauseToken / Deadline.

Phase 6 §6.1. 用于在 step 边界检查控制信号。
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field


class CancelToken:
    """Cooperative cancellation token (hard cancel)."""

    def __init__(self) -> None:
        self._event = asyncio.Event()

    def cancel(self) -> None:
        self._event.set()

    @property
    def is_cancelled(self) -> bool:
        return self._event.is_set()

    async def wait(self) -> None:
        await self._event.wait()

    def raise_if_cancelled(self) -> None:
        if self.is_cancelled:
            raise asyncio.CancelledError("CancelToken triggered")


class PauseToken:
    """Cooperative pause token — can be paused and resumed."""

    def __init__(self) -> None:
        self._paused = asyncio.Event()
        self._resume = asyncio.Event()
        self._resume.set()  # start un-paused

    def pause(self) -> None:
        self._paused.set()
        self._resume.clear()

    def resume(self) -> None:
        self._paused.clear()
        self._resume.set()

    @property
    def is_paused(self) -> bool:
        return self._paused.is_set()

    async def wait_if_paused(self) -> None:
        """Suspend caller until resumed."""
        if self._paused.is_set():
            await self._resume.wait()

    async def wait_paused(self) -> None:
        """Resolve once paused (mirror of CancelToken.wait for the soft-stop signal)."""
        await self._paused.wait()


@dataclass
class Deadline:
    """Absolute wall-clock deadline."""

    deadline_at: float = field(default_factory=lambda: time.monotonic() + 3600)

    @property
    def remaining_sec(self) -> float:
        return max(0.0, self.deadline_at - time.monotonic())

    @property
    def is_expired(self) -> bool:
        return time.monotonic() >= self.deadline_at

    def raise_if_expired(self) -> None:
        if self.is_expired:
            raise TimeoutError("Deadline exceeded")
