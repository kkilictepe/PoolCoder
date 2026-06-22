"""Quit wiring: q and Ctrl+C must both exit immediately (no confirm prompt)."""

from __future__ import annotations

from pool_coder.config import Config
from pool_coder.pricing import Pricing
from pool_coder.ui.app import PoolCoderApp


def test_quit_bindings_present_at_app_level():
    keymap = {b.key: b.action for b in PoolCoderApp.BINDINGS}
    assert keymap.get("q") == "quit"
    assert keymap.get("ctrl+c") == "quit"


def test_quit_actions_call_exit():
    app = PoolCoderApp(None, Config(), Pricing.load())
    calls: list[bool] = []
    app.exit = lambda *a, **k: calls.append(True)  # type: ignore[method-assign]
    app.action_quit()
    app.action_help_quit()  # the Ctrl+C path Textual 8.x routes through
    assert len(calls) == 2
