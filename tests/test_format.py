"""Format helpers: short model labels and short session ids."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from pool_coder.format import as_of_text, limit_window, short_id, short_model


def test_short_model_claude_keeps_todays_rule():
    # the renderer's historical `m.split('-')[1] if '-' in m else m`
    assert short_model("claude-opus-4-8") == "opus"
    assert short_model("claude-sonnet-4-6") == "sonnet"
    assert short_model("claude-fable-5") == "fable"
    assert short_model("unknown") == "unknown"
    assert short_model("some-future-model") == "future"


def test_short_model_gpt_drops_prefix_only():
    assert short_model("gpt-5.6-sol") == "5.6-sol"
    assert short_model("gpt-5.6-terra-2026-09-01") == "5.6-terra-2026-09-01"
    assert short_model("gpt-6-astra") == "6-astra"
    # gpt-family ids without the "gpt-" prefix stay whole
    assert short_model("codex-auto-review") == "codex-auto-review"
    assert short_model("o4-mini") == "o4-mini"
    assert short_model("o3") == "o3"
    assert short_model("gpt") == "gpt"


def test_short_model_in_a_claude_session_keeps_the_old_rule():
    # OpenAI ids recorded by Claude Code (behind a gateway) label as they did
    assert short_model("o4-mini", "claude") == "mini"
    assert short_model("gpt-5.6-sol", "claude") == "5.6"
    assert short_model("openai/gpt-5", "claude") == "5"
    assert short_model("claude-opus-4-8", "claude") == "opus"
    assert short_model("gpt-5.6-sol", "codex") == "5.6-sol"


def test_short_model_missing():
    assert short_model(None) == "?"
    assert short_model("") == "?"


def test_short_id_claude_uses_head():
    sid = "048b605c-543a-48da-a927-035c9d24ad26"
    assert short_id(sid) == "048b605c"
    assert short_id(sid, "claude") == "048b605c"


def test_short_id_codex_uses_tail():
    # UUIDv7 thread ids minted within a minute share their first 8 chars
    a = "019a1b2c-3d4e-7f00-8000-0000000000aa"
    b = "019a1b2c-3d4f-7f00-8000-0000000000bb"
    assert short_id(a, "codex") == "000000aa"
    assert short_id(b, "codex") == "000000bb"
    assert short_id(a, "codex") != short_id(b, "codex")


def test_short_id_empty():
    assert short_id("") == ""
    assert short_id("", "codex") == ""


NOW = datetime(2026, 9, 24, 12, 0, tzinfo=timezone.utc)


def test_limit_window_live_expired_and_unknown_reset():
    ahead = NOW + timedelta(hours=2, minutes=1)
    value, reset, bar = limit_window(87.0, ahead, NOW)
    assert (value, bar) == ("87%", 87.0) and reset.startswith("resets 2h0")
    # the reset time has passed: the old numbers describe a finished window
    assert limit_window(87.0, NOW - timedelta(hours=1), NOW) == ("—", "reset, no newer data", None)
    assert limit_window(87.0, NOW, NOW) == ("—", "reset, no newer data", None)
    # an unknown reset time never expires
    assert limit_window(87.0, None, NOW) == ("87%", None, 87.0)
    assert limit_window(None, None, NOW) == ("—", None, None)


def test_as_of_text_shows_the_age():
    at = NOW - timedelta(days=4)
    assert as_of_text(at, NOW) == f"{at.astimezone():%H:%M} (4d ago)"
    assert as_of_text(None) is None
