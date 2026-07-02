"""Pricing: family resolution, cost math, and override loading."""

from __future__ import annotations

from pool_coder.models import UsageTokens
from pool_coder.pricing import Pricing


def test_default_rates_and_cost_math():
    p = Pricing.load()
    # 1M input tokens on opus == $5.00 exactly
    assert p.cost(UsageTokens(input=1_000_000), "claude-opus-4-8") == 5.0
    # 1M output on sonnet == $15.00
    assert p.cost(UsageTokens(output=1_000_000), "claude-sonnet-4-6") == 15.0
    # cache read is cheap (opus 0.5/MTok)
    assert p.cost(UsageTokens(cache_read=1_000_000), "claude-opus-4-8") == 0.5


def test_fable_family_rates():
    p = Pricing.load()
    # 1M input on fable == $10.00; mythos shares the fable family
    assert p.cost(UsageTokens(input=1_000_000), "claude-fable-5") == 10.0
    assert p.cost(UsageTokens(output=1_000_000), "claude-mythos-5") == 50.0


def test_unknown_model_falls_back_to_default():
    p = Pricing.load()
    assert p.cost(UsageTokens(input=1_000_000), "some-future-model") == 5.0


def test_override_file(tmp_path):
    f = tmp_path / "pricing.toml"
    f.write_text("[opus]\ninput=99.0\noutput=0\ncache_write=0\ncache_read=0\n", encoding="utf-8")
    p = Pricing.load(f)
    assert p.cost(UsageTokens(input=1_000_000), "claude-opus-4-8") == 99.0
    # missing families still resolve via default
    assert p.cost(UsageTokens(input=1_000_000), "claude-haiku-4-5") == 5.0
