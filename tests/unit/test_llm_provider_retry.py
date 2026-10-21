from ctx_weft.providers.llm import ModelCapabilities, ModelLimits
from ctx_weft.providers.llm.provider import LLMAccount, LLMProvider, ModelConfig


class _MemStore:
    def save(self, account): ...
    def delete(self, name): ...
    def list_all(self):
        return []


def _acct(style):
    return LLMAccount(
        name="a",
        api_key="k",
        models=[
            ModelConfig(
                name="m",
                output_reserve=100,
                limit=ModelLimits(1000, output=None),
                protocol=style.removesuffix("-multimodal"),
                capabilities=ModelCapabilities(
                    ["text", "image"] if style.endswith("-multimodal") else ["text"]
                ),
            )
        ],
        default_model="m",
        base_url="https://api.anthropic.com"
        if style.removesuffix("-multimodal") == "anthropic"
        else "https://api.openai.com",
    )


def test_provider_threads_retry_to_adapter():
    p = LLMProvider(_MemStore(), max_http_retries=9)
    p.register_account(_acct("anthropic"), persist=False)
    assert p.get_client("a")._adapter._max_http_retries == 9


def test_provider_threads_retry_to_openai_adapter():
    p = LLMProvider(_MemStore(), max_http_retries=5)
    p.register_account(_acct("openai"), persist=False)
    assert p.get_client("a")._adapter._max_http_retries == 5


def test_provider_default_retries():
    p = LLMProvider(_MemStore())
    p.register_account(_acct("anthropic"), persist=False)
    assert p.get_client("a")._adapter._max_http_retries == 3
