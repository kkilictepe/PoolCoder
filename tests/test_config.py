"""Config: model-family resolution and context-window selection."""

from __future__ import annotations

import pytest

from pool_coder.config import OPUS_1M, STANDARD_WINDOW, Config, model_family


def test_model_family_resolution():
    assert model_family("claude-opus-4-8") == "opus"
    assert model_family("claude-sonnet-4-6") == "sonnet"
    assert model_family("claude-haiku-4-5") == "haiku"
    assert model_family("claude-fable-5") == "fable"
    assert model_family("claude-mythos-5") == "fable"
    assert model_family("some-future-model") == "default"
    assert model_family(None) == "default"


def test_window_defaults():
    c = Config()
    assert c.window_for("claude-opus-4-8") == OPUS_1M
    assert c.window_for("claude-fable-5") == OPUS_1M
    assert c.window_for("claude-mythos-5") == OPUS_1M
    assert c.window_for("claude-sonnet-4-6") == STANDARD_WINDOW
    assert c.window_for("unknown-model") == STANDARD_WINDOW


def test_window_autobump_on_observed_context():
    c = Config()
    assert c.window_for("claude-sonnet-4-6", observed_max=250_000) == OPUS_1M
    c.auto_bump = False
    assert c.window_for("claude-sonnet-4-6", observed_max=250_000) == STANDARD_WINDOW


def test_window_override():
    c = Config()
    c.apply_window_override("fable=200000")
    assert c.window_for("claude-fable-5") == 200_000
    c.apply_window_override("mythos=500000")
    assert c.window_for("claude-mythos-5") == 500_000
    with pytest.raises(ValueError):
        c.apply_window_override("gizmo=1000")


def test_gpt_family_resolution():
    for model in ("gpt-5.6-sol", "gpt-5.6-terra", "gpt-6-astra", "GPT-5.5",
                  "codex-auto-review", "codex-mini-latest", "o3", "o4-mini"):
        assert model_family(model) == "gpt", model
    # Claude families win over the gpt substring rules and stay unchanged.
    assert model_family("claude-opus-4-8") == "opus"
    assert model_family("claude-fable-5") == "fable"
    # "opus"/"other" start with "o" but not "o<digit>".
    assert model_family("other-model") == "default"


def test_gpt_window_default_and_never_bumped():
    c = Config(agent="codex")
    assert c.window_for("gpt-5.6-sol") == 272_000
    assert c.window_for("codex-auto-review") == 272_000
    # Past 200K a Claude window auto-bumps to 1M; a gpt window never does.
    assert c.window_for("gpt-5.6-sol", observed_max=900_000) == 272_000
    assert c.window_for("claude-sonnet-4-6", observed_max=250_000) == OPUS_1M
    # the session's agent can be passed per call
    assert Config().window_for("gpt-5.6-sol", 900_000, agent="codex") == 272_000


def test_openai_ids_in_a_claude_session_keep_the_default_window():
    # e.g. Claude Code behind a gateway that records the routed model id
    c = Config()
    for model in ("gpt-4o", "o3", "o4-mini", "openai/gpt-5", "gpt-5.6-sol"):
        assert model_family(model, "claude") == "default", model
        assert c.window_for(model) == STANDARD_WINDOW, model
        assert c.window_for(model, observed_max=900_000) == OPUS_1M, model
    assert model_family("claude-opus-4-8", "claude") == "opus"


def test_gpt_window_override_and_codex_alias():
    c = Config(agent="codex")
    c.apply_window_override("gpt=400000")
    assert c.window_for("gpt-5.6-sol") == 400_000
    c.apply_window_override("codex=128000")
    assert c.window_for("o4-mini") == 128_000
    # other families are untouched by the gpt override
    assert c.window_for("claude-opus-4-8") == OPUS_1M


def test_config_agent_defaults_to_claude():
    assert Config().agent == "claude"
    assert Config(agent="codex").agent == "codex"
