"""Wait for a parked run in tests that intentionally do not finish their task."""

import asyncio


async def close_runtime(runtime, *, timeout=10.0):
    """Stop test sessions through the public API and join their real producers."""
    for session_id, manager in list(runtime._task_managers.items()):
        await runtime.cancel_session(session_id)
        async with asyncio.timeout(timeout):
            while manager._dispatch_tasks or manager._background_asyncio_tasks:
                await asyncio.sleep(0.01)


async def wait_for_park(handle, *, timeout=5.0):
    async with asyncio.timeout(timeout):
        while True:
            state = handle._state
            recap = (handle._pending_observe(handle.session_id, handle.task_id)
                     if handle._pending_observe else None)
            if state is not None and state.task.status in {
                    "AWAITING_HUMAN", "SUSPENDED", "INTERRUPTED"} and recap is None:
                return state
            await asyncio.sleep(0.01)
