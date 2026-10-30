from ctx_weft.providers.llm.adapters.openai_compatible import OpenAICompatibleAdapter


def test_chat_url_default_unchanged():
    a = OpenAICompatibleAdapter(api_key="k", base_url="https://api.openai.com")
    assert a._chat_url() == "https://api.openai.com/v1/chat/completions"
