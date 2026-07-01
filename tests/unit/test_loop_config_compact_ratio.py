from ctx_weft.protocols.template import LoopConfig


def test_compact_target_ratio_defaults_to_zero():
    assert LoopConfig().compact_target_ratio == 0.0


def test_compact_target_ratio_below_trigger():
    lc = LoopConfig(compact_token_ratio=0.8, compact_target_ratio=0.6)
    assert lc.compact_target_ratio < lc.compact_token_ratio
