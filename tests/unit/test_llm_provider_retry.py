from ctx_weft.providers.llm.provider import LLMProvider, LLMAccount, ModelConfig


class _MemStore:
    def save(self, account): ...
    def delete(self, name): ...
    def list_all(self): return []


def _acct(style):
    return LLMAccount(
        name="a",
        style=style,
        api_key="k",
        models=[ModelConfig(name="m", context_limit=1000, output_reserve=100)],
        default_model="m",
    )


def test_provider_threads_retry_to_adapter():
    p = LLMProvider(_MemStore(), max_http_retries=9)
    p.register_account(_acct("anthropic"), persist=False)
    assert p._adapters["a"]._max_http_retries == 9


def test_provider_threads_retry_to_openai_adapter():
    p = LLMProvider(_MemStore(), max_http_retries=5)
    p.register_account(_acct("openai"), persist=False)
    assert p._adapters["a"]._max_http_retries == 5


def test_provider_default_retries():
    p = LLMProvider(_MemStore())
    p.register_account(_acct("anthropic"), persist=False)
    assert p._adapters["a"]._max_http_retries == 3
