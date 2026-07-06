"""Per-session control-state reclamation (per-run token registry + TaskManager).

- B: when a session parks/suspends and goes idle, its per-run tokens are already gone
     (deregistered in `_SessionTaskRunner.execute`'s finally as the run itself ends),
     but the TaskManager is KEPT (still needed to cancel a paused session).
- A: cancelling an already-idle (parked) session reclaims the TaskManager too —
     cancel_all never fires the session-done callback, so it would otherwise linger.
"""
from __future__ import annotations

import asyncio

import pytest

from ctx_weft.providers.llm.mock import MockResponse
from tests.integration.test_outage_resume import (
    _FlakyLLM, _make_runtime, _new_params, _wait_for_run_finish,
)

pytestmark = pytest.mark.asyncio


async def test_idle_park_reclaims_tokens_keeps_task_manager():
    # fail_for=1: the first act call raises LLMOutageError → task SUSPENDED → session idle.
    llm = _FlakyLLM(fail_for=1, responses=[], ri_responses=[MockResponse(text="")] * 3)
    rt = _make_runtime(llm)

    handle = await rt.start_session(_new_params())
    sid = handle.session_id
    await _wait_for_run_finish(handle)
    await asyncio.sleep(0.05)  # let the SUSPENDED branch + idle callback run

    # B — tokens reclaimed on idle-park; TaskManager retained for cancellability/resume query.
    assert sid not in rt._run_tokens, "per-run tokens deregister when the run parks"
    assert sid in rt._task_managers, "task_manager must be kept while the session is only paused"

    # A — cancelling the idle (parked) session reclaims the heavy TaskManager too.
    assert await rt.cancel_session(sid) is True
    assert sid not in rt._task_managers, "cancel of an idle session must reclaim the task_manager"
    assert sid not in rt._run_tokens
