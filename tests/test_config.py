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
