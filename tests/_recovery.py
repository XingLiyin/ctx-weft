"""Address older agent-oriented test fixtures through the session recovery contract."""
async def recover_fixture_session(runtime, agent_id, *, session_id=None, keep_alive=False, **kwargs):
    if session_id is None:
        session_id = (await runtime.get_agent(agent_id)).data.session_id
    if keep_alive:
        return await runtime.rebuild_session(session_id, strict=True)
    if kwargs:
        # These fixtures specifically exercise the private HITL recovery path.
        async with runtime._session_guard(session_id):
            return await runtime._recover_session_locked(session_id, **kwargs)
    return await runtime.recover_session(session_id)


async def load_pending_fixture(runtime, session_id):
    await runtime.rebuild_session(session_id, strict=True)
    return len(await runtime.list_pending_hitl(session_id=session_id))


async def query_committed_fixture_tasks(runtime, session_id):
    """Check known fixture task identities even after a snapshot prunes hot tasks."""
    cursor, tasks = None, []
    while True:
        page = await runtime.list_task_ids(session_id, cursor=cursor)
        tasks.extend([await runtime.get_task(session_id, tid) for tid in page.ids])
        if page.next_cursor is None:
            return tasks
        cursor = page.next_cursor
