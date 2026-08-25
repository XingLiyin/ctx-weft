from ctx_weft.providers.llm.provider import ModelConfig, LLMProvider, LLMAccount, _FixedModelClient


def test_model_config_defaults_to_no_vision():
    """严格默认：未显式声明即无视觉能力。"""
    assert ModelConfig(name="m", context_limit=1000).supports_vision is False


def test_model_config_accepts_explicit_vision():
    assert ModelConfig(name="m", context_limit=1000, supports_vision=True).supports_vision is True


def test_getattr_convention_on_object_without_property():
    """core 的读取约定：未声明该属性的 client 一律视为无视觉能力。"""
    class _LegacyClient:
        pass

    assert getattr(_LegacyClient(), "supports_vision", False) is False


def _client(*, vision: bool):
    return _FixedModelClient(
        adapter=None, model="m", context_limit=1000,
        output_reserve=100, output_ceiling=None, account="acct",
        supports_vision=vision,
    )


def test_fixed_model_client_exposes_vision_true():
    """_FixedModelClient 是 core 实际拿到的 client——属性必须透传。"""
    assert _client(vision=True).supports_vision is True


def test_fixed_model_client_defaults_to_no_vision():
    """构造时不传该参数 → 严格默认。"""
    c = _FixedModelClient(adapter=None, model="m", context_limit=1000, output_reserve=100)
    assert getattr(c, "supports_vision", False) is False


# ── (c) 覆盖证明：LLMProvider.get_client 必须把 ModelConfig.supports_vision 真正传给 client ──
# 上面四条测试全部绕过 get_client（直接构造 ModelConfig / _FixedModelClient）。若只做了
# (a)(b) 而漏了 get_client 里的透传（provider.py 245 行附近），上面四条测试依旧全绿，但
# 门控在真实路径上永远拿到 False。这条测试专门堵住这个"测试全绿但功能失效"的缺口。

class _StoreStub:
    def save(self, a): ...
    def delete(self, n): return True
    def list_all(self): return []


def test_get_client_propagates_supports_vision_from_model_config():
    """经真实 LLMProvider.get_client 拿到的 client，其 supports_vision 必须来自 ModelConfig。"""
    p = LLMProvider(_StoreStub())
    p.register_account(LLMAccount(
        name="a", style="openai", api_key="k", base_url="https://x/v1",
        models=[ModelConfig(name="m", context_limit=200_000, supports_vision=True)],
        default_model="m",
    ), persist=False)
    client = p.get_client("a", "m")
    assert client.supports_vision is True


def test_get_client_defaults_supports_vision_false_when_unset():
    p = LLMProvider(_StoreStub())
    p.register_account(LLMAccount(
        name="a", style="openai", api_key="k", base_url="https://x/v1",
        models=[ModelConfig(name="m", context_limit=200_000)],  # supports_vision 未声明
        default_model="m",
    ), persist=False)
    client = p.get_client("a", "m")
    assert client.supports_vision is False
