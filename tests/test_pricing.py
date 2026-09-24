"""Pricing: family resolution, prefix keys, cost math, and override loading."""

from __future__ import annotations

import tomllib
from pathlib import Path

from pool_coder.models import UsageTokens
from pool_coder.pricing import _DEFAULT_TOML, Pricing, Rate


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


def _in(p, model):
    """Cost of 1M input tokens == the model's input rate."""
    return p.cost(UsageTokens(input=1_000_000), model)


def test_gpt_longest_prefix_over_family():
    p = Pricing.load()
    assert _in(p, "gpt-5.6-sol") == 4.0
    assert _in(p, "gpt-5") == 1.25
    # dated ids resolve through the prefix at a "-" boundary
    assert _in(p, "gpt-5.6-terra-2026-09-01") == 2.0
    assert p.cost(UsageTokens(output=1_000_000), "gpt-5.6-terra-2026-09-01") == 12.0
    # the longest matching prefix wins (gpt-5-mini over gpt-5)
    assert _in(p, "gpt-5-mini") == 0.25
    assert _in(p, "gpt-5.1-codex-mini") == 0.25
    assert p.rate_for("gpt-6-astra").cache_write == 12.5


def test_mini_variant_of_a_listed_key_has_its_own_price():
    # without its own key, gpt-5-codex-mini would take ["gpt-5-codex"] (5x)
    p = Pricing.load()
    assert p.rate_for("gpt-5-codex-mini") == Rate(0.25, 2.0, 0.25, 0.025)
    assert p.rate_for("gpt-5-codex-mini") == p.rate_for("gpt-5.1-codex-mini")
    assert _in(p, "gpt-5-codex") == 1.25


def test_unknown_gpt_falls_to_gpt_fallback_not_prefix():
    p = Pricing.load()
    fallback = p.rates["gpt"]
    # "gpt-5.7-sol" must not match "gpt-5" (no "-" boundary after "gpt-5")
    assert p.rate_for("gpt-5.7-sol") == fallback
    assert _in(p, "gpt-5.7-sol") == 2.0
    # the guardian reviewer has no public price -> [gpt] fallback
    assert p.rate_for("codex-auto-review") == fallback
    assert p.rate_for("gpt") == fallback


def test_o_series_rates():
    p = Pricing.load()
    assert _in(p, "o3") == 2.0
    assert p.cost(UsageTokens(output=1_000_000), "o3") == 8.0
    assert _in(p, "o4-mini") == 1.1
    assert p.cost(UsageTokens(cache_read=1_000_000), "o4-mini") == 0.275


def test_claude_lookups_unchanged_by_prefix_keys():
    p = Pricing.load()
    assert p.rate_for("claude-opus-4-8") == p.rates["opus"]
    assert p.rate_for("claude-sonnet-4-6") == p.rates["sonnet"]
    assert p.rate_for(None) == p.rates["default"]


def test_prefix_keys_never_price_claude_ids(tmp_path):
    # A custom file's non-family keys did nothing for Claude ids before Codex
    # support; a "-" boundary is no version boundary for claude-opus-4-8.
    f = tmp_path / "pricing.toml"
    f.write_text("[default]\ninput=5.0\n[opus]\ninput=5.0\noutput=25.0\n"
                 '["claude-opus-4"]\ninput=15.0\noutput=75.0\n'
                 "[claude]\ninput=0\n[anthropic]\ninput=0\n", encoding="utf-8")
    p = Pricing.load(f)
    for model in ("claude-opus-4-8", "claude-opus-4-5-20251101", "claude-opus-4"):
        for agent in (None, "claude", "codex"):
            assert p.rate_for(model, agent) == p.rates["opus"], (model, agent)
    assert _in(p, "claude-sonnet-4-6") == 5.0  # no [sonnet]: default, never [claude]
    assert p.cost(UsageTokens(input=1_000_000, output=1_000_000), "claude-opus-4-8") == 30.0


def test_claude_sessions_price_openai_ids_as_before(tmp_path):
    # Claude Code behind a gateway records e.g. "gpt-4o": priced like any
    # unknown model in that session, never with the OpenAI table
    p = Pricing.load()
    for model in ("gpt-4o", "gpt-5.6-sol", "o4-mini"):
        assert p.rate_for(model, "claude") == p.rates["default"], model
    assert p.rate_for("gpt-5.6-sol", "codex").input == 4.0
    f = tmp_path / "pricing.toml"
    f.write_text("[default]\ninput=1.0\n", encoding="utf-8")
    custom = Pricing.load(f)
    assert custom.cost(UsageTokens(input=1_000_000), "gpt-4o", "claude") == 1.0
    assert custom.cost(UsageTokens(input=1_000_000), "gpt-4o") == 2.0  # Codex: [gpt]


def test_custom_file_without_gpt_prices_gpt_at_gpt_fallback(tmp_path):
    f = tmp_path / "pricing.toml"
    f.write_text("[opus]\ninput=99.0\noutput=0\ncache_write=0\ncache_read=0\n", encoding="utf-8")
    p = Pricing.load(f)
    # not default ($5) and not opus ($99): the embedded gpt fallback
    assert _in(p, "gpt-5.6-sol") == 2.0
    assert p.cost(UsageTokens(output=1_000_000), "gpt-5.6-sol") == 10.0


def test_custom_file_prefix_keys_are_quoted(tmp_path):
    f = tmp_path / "pricing.toml"
    f.write_text('["gpt-5.6-sol"]\ninput=7.0\noutput=0\ncache_write=0\ncache_read=0\n',
                 encoding="utf-8")
    p = Pricing.load(f)
    assert _in(p, "gpt-5.6-sol-2026-09-01") == 7.0
    assert _in(p, "gpt-5.6-terra") == 2.0  # unlisted -> gpt fallback


def test_repo_pricing_toml_matches_embedded_defaults():
    repo_file = Path(__file__).resolve().parents[1] / "pricing.toml"
    text = repo_file.read_text(encoding="utf-8")
    # parse directly: Pricing.load() would hide a broken file behind the defaults
    file_tables = tomllib.loads(text)
    embedded_tables = tomllib.loads(_DEFAULT_TOML)
    assert list(file_tables) == list(embedded_tables)  # same keys, same order
    from_file = Pricing._from_toml_text(text).rates
    embedded = Pricing._from_toml_text(_DEFAULT_TOML).rates
    for key, rate in embedded.items():
        assert from_file[key] == rate, key


def test_unquoted_dotted_key_equals_quoted_key(tmp_path):
    body = "input=7.0\noutput=21.0\ncache_write=8.0\ncache_read=0.7\n"
    quoted = tmp_path / "quoted.toml"
    quoted.write_text('["gpt-5.6-sol"]\n' + body, encoding="utf-8")
    unquoted = tmp_path / "unquoted.toml"
    # an unquoted dot is a nested TOML table: {"gpt-5": {"6-sol": {...}}}
    unquoted.write_text("[gpt-5.6-sol]\n" + body, encoding="utf-8")
    assert tomllib.loads(unquoted.read_text(encoding="utf-8")) == {
        "gpt-5": {"6-sol": {"input": 7.0, "output": 21.0, "cache_write": 8.0, "cache_read": 0.7}}
    }
    q, u = Pricing.load(quoted), Pricing.load(unquoted)
    assert u.rates == q.rates
    assert u.rates["gpt-5.6-sol"] == Rate(7.0, 21.0, 8.0, 0.7)
    # no zero-rate "gpt-5" entry that would swallow gpt-5 / gpt-5-mini
    assert "gpt-5" not in u.rates
    assert _in(u, "gpt-5.6-sol-2026-09-01") == 7.0
    assert _in(u, "gpt-5-mini") == 2.0  # unlisted -> gpt fallback, not $0


def test_unquoted_dotted_keys_deep_and_mixed_with_a_rate_table(tmp_path):
    f = tmp_path / "pricing.toml"
    f.write_text(
        "[gpt-5]\ninput=1.25\noutput=10.0\ncache_write=1.25\ncache_read=0.125\n"
        # a sub-table of a rate table, and a two-dot key (three nesting levels)
        "[gpt-5.1]\ninput=1.5\noutput=11.0\ncache_write=1.5\ncache_read=0.15\n"
        "[gpt-5.1.9-codex]\ninput=3.0\noutput=0\ncache_write=0\ncache_read=0\n"
        '["o3"]\ninput=2.0\noutput=8.0\ncache_write=2.0\ncache_read=0.5\n',
        encoding="utf-8",
    )
    p = Pricing.load(f)
    assert set(p.rates) == {"gpt-5", "gpt-5.1", "gpt-5.1.9-codex", "o3", "default", "gpt"}
    assert _in(p, "gpt-5") == 1.25
    assert _in(p, "gpt-5-codex") == 1.25
    assert _in(p, "gpt-5.1") == 1.5
    assert p.cost(UsageTokens(output=1_000_000), "gpt-5.1-codex") == 11.0
    assert _in(p, "gpt-5.1.9-codex") == 3.0
    assert _in(p, "o3") == 2.0


def test_embedded_defaults_unchanged_by_flattening():
    # the embedded table has no nested tables: flattening is a no-op for it
    data = tomllib.loads(_DEFAULT_TOML)
    rates = Pricing._from_toml_text(_DEFAULT_TOML).rates
    assert list(rates) == list(data)
    assert rates["gpt-5.6-sol"] == Rate(4.0, 20.0, 5.0, 0.4)


def test_bad_value_type_falls_back_to_defaults(tmp_path):
    f = tmp_path / "pricing.toml"
    f.write_text("[opus]\ninput=[1, 2]\n", encoding="utf-8")
    p = Pricing.load(f)  # does not raise
    assert p.rates == Pricing.load().rates
