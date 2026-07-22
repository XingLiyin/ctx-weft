"""Runtime wires a PauseToken into LoopContext and tracks task managers."""

from ctx_weft.core import CtxWeftRuntime
from ctx_weft.core.control.tokens import CancelToken, PauseToken
from ctx_weft.providers.llm.mock import MockLLMAdapter
from tests.integration.test_minimal_loop import InlineAgentTemplateProvider, make_runtime


def _rt():
    return make_runtime(llm=MockLLMAdapter(responses=[]),
                           agent_provider=InlineAgentTemplateProvider())


def test_runtime_has_pause_and_task_manager_maps():
    rt = _rt()
    assert rt._run_tokens == {}
    assert rt._task_managers == {}


def test_build_loop_ctx_wires_pause_token():
    rt = _rt()
    pause = PauseToken()
    ctx = rt._build_loop_ctx(
        assembler=None, llm=rt._resolve_llm(None, None), memory=None,
        provider_ctx=None, gateway=None, skill_index={},
        cancel_token=CancelToken(), task_manager=None, pause_token=pause,
    )
    assert ctx.pause_token is pause
