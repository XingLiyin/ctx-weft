from loomex_core.providers.llm.openai import OpenAIAdapter


def test_chat_url_default_unchanged():
    a = OpenAIAdapter(api_key="k", base_url="https://api.openai.com")
    assert a._chat_url() == "https://api.openai.com/v1/chat/completions"
