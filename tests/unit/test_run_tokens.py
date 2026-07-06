"""RunTokens：一次 run（单次任务派发）的控制信号对。"""

from ctx_weft.core.control import RunTokens
from ctx_weft.core.control.tokens import CancelToken, PauseToken


def test_run_tokens_pairs_are_independent():
    a = RunTokens(cancel=CancelToken(), pause=PauseToken())
    b = RunTokens(cancel=CancelToken(), pause=PauseToken())
    a.pause.pause()
    a.cancel.cancel()
    assert a.pause.is_paused and a.cancel.is_cancelled
    assert not b.pause.is_paused and not b.cancel.is_cancelled
