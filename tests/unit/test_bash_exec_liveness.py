import sys

from ctx_weft.providers.capability_filesystem import provider as fsprov
from ctx_weft.protocols.context import ProviderContext


async def _collect(events):
    return [e async for e in events]


async def test_bash_exec_streams_and_completes():
    ctx = ProviderContext(session_id="s1")
    cmd = f'{sys.executable} -c "print(\'alpha\'); print(\'beta\')"'
    events = await _collect(fsprov.shell(cmd, ctx=ctx))
    kinds = [e.kind for e in events]
    assert "stdout" in kinds
    result = next(e for e in events if e.kind == "result")
    assert "alpha" in result.payload["content"]
    assert "beta" in result.payload["content"]
    assert result.payload["metadata"]["exit_code"] == 0


async def test_bash_exec_idle_timeout_reports_error():
    ctx = ProviderContext(session_id="s1", extra={"bash_idle_timeout_sec": 1.0})
    cmd = f'{sys.executable} -c "import time; time.sleep(60)"'
    events = await _collect(fsprov.shell(cmd, ctx=ctx))
    err = next(e for e in events if e.kind == "error")
    assert err.payload["code"] == "TIMEOUT"
