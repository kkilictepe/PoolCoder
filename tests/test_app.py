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


# -- Codex ---------------------------------------------------------------------------
# Headless runs use Textual's run_test() pilot (driven with asyncio.run, so no
# pytest plugin is needed) against sessions in the ``codex_home`` fixture.
import asyncio  # noqa: E402
import io  # noqa: E402
import time  # noqa: E402

from rich.console import Console  # noqa: E402
from textual.widgets import OptionList, Static  # noqa: E402

import codex_records as cr  # noqa: E402
from pool_coder import paths  # noqa: E402
from pool_coder.codex import paths as cpaths  # noqa: E402
from pool_coder.providers import CLAUDE, CODEX  # noqa: E402
from pool_coder.ui.app import DashboardScreen, PickerScreen  # noqa: E402


def _write_session(home) -> None:
    cr.write_rollout(home, cr.ROOT, [
        cr.session_meta(cr.ROOT, cwd="C:\\Git\\rootproj", at=cr.ts(0)),
        cr.turn_context("gpt-5.6-terra", cwd="C:\\Git\\rootproj", at=cr.ts(0, 1)),
        cr.task_started(at=cr.ts(0, 2)),
        cr.user_message_item("fix the flaky test", at=cr.ts(0, 3)),
        cr.token_usage("resp_1", inp=100_000, cached=80_000, out=5_000, rs=2_000, at=cr.ts(1)),
        cr.sub_agent_item("act_1", "started", cr.CHILD, at=cr.ts(1, 20)),
    ])
    cr.write_rollout(home, cr.CHILD, [
        cr.subagent_meta(cr.CHILD, forked=False, at=cr.ts(1, 20)),
        cr.task_started(at=cr.ts(1, 21)),
        cr.token_usage("resp_c1", thread_id=cr.CHILD, inp=40_000, cached=30_000, out=2_000,
                       rs=500, at=cr.ts(1, 30)),
    ], local_ts="2026-09-22T23-49-12")


def _plain(renderable) -> str:
    con = Console(record=True, width=120, file=io.StringIO())
    con.print(renderable)
    return con.export_text()


def _picker_state(config: Config) -> tuple[str, list[tuple[str, str]], str]:
    """Run the picker headlessly: (title text, [(option id, row text)], app sub-title)."""
    async def run():
        app = PoolCoderApp(None, config, Pricing.load())
        async with app.run_test(size=(140, 40)) as pilot:
            await pilot.pause()
            screen = app.screen
            assert isinstance(screen, PickerScreen)
            title = str(screen.query_one("#picker-title", Static).content)
            options = screen.query_one(OptionList)
            rows = [(opt.id, str(opt.prompt))
                    for opt in (options.get_option_at_index(i)
                                for i in range(options.option_count))]
            return title, rows, app.sub_title
    return asyncio.run(run())


def test_app_constructs_with_codex_config():
    app = PoolCoderApp(None, Config(agent="codex"), Pricing.load())
    assert app.config.agent == "codex"
    assert PickerScreen(Config(agent="codex")).provider is CODEX
    assert PickerScreen(Config()).provider is CLAUDE


def test_codex_picker_lists_codex_sessions(codex_home):
    _write_session(codex_home)
    title, rows, sub_title = _picker_state(Config(agent="codex"))
    assert title.startswith("Select a Codex session to monitor — 1 active (15m).")
    assert [sid for sid, _ in rows] == [cr.ROOT]  # the sub-agent thread is rolled up
    text = rows[0][1]                             # previewed by the Codex overview
    assert "gpt-5.6-terra" in text and "rootproj" in text and "fix the flaky test" in text
    assert sub_title == "Codex"


def test_codex_picker_marks_a_thread_name_standing_in_for_the_prompt(monkeypatch):
    from datetime import datetime, timezone
    from pathlib import Path

    from pool_coder.codex import overview as codex_overview
    from pool_coder.overview import SessionOverview

    info = paths.SessionInfo(Path("x.jsonl"), cr.ROOT, "2026/09/22", datetime.now(timezone.utc), 1)
    ov = SessionOverview(info=info, cwd="C:/p", model="gpt-5.6-sol", git_branch=None,
                         context_tokens=1, occupancy=0.5, last_text="Auto [b]title",
                         is_live=True, last_is_title=True)
    monkeypatch.setattr(codex_overview, "peek_codex_session", lambda i, c=None, **k: ov)
    row = PickerScreen(Config(agent="codex"))._format(info)
    assert row.plain.endswith(" [Auto [b]title]")


def test_claude_picker_title_unchanged(monkeypatch):
    monkeypatch.setattr(paths, "list_sessions", lambda *a, **k: [])
    title, rows, sub_title = _picker_state(Config())
    assert title.startswith("Select a session to monitor — 0 active (15m).  ↑/↓ move")
    assert rows == [] and sub_title == ""


def test_codex_dashboard_renders_a_session(codex_home):
    _write_session(codex_home)
    info = cpaths.find_session(cr.ROOT)
    assert info is not None

    async def run():
        app = PoolCoderApp(info, Config(agent="codex", plan_limits=False), Pricing.load())
        async with app.run_test(size=(160, 60)) as pilot:
            await pilot.pause()
            screen = app.screen
            assert isinstance(screen, DashboardScreen)
            deadline = time.monotonic() + 10
            while True:  # wait for the reader thread's first full snapshot
                snap = screen.engine.get_snapshot()
                if (snap is not None and not snap.loading) or time.monotonic() > deadline:
                    break
                await pilot.pause(0.05)
            screen._tick()
            await pilot.pause()
            return snap, {wid: _plain(screen.query_one(f"#{wid}", Static).content)
                          for wid in ("hdr", "ctx", "tokens", "activity", "plan",
                                      "subagents", "workflows", "events")}

    snap, panels = asyncio.run(run())
    assert snap is not None and not snap.loading and snap.session.agent == "codex"
    assert f"pool-coder · {cr.ROOT[-8:]}" in panels["hdr"]
    assert "reasoning" in panels["tokens"] and "cached" in panels["tokens"]
    assert f"/ {cr.WINDOW:,} tokens" in panels["ctx"]
    assert "dependency_audit" in panels["subagents"]
    assert "disabled" in panels["plan"]
    assert "fix the flaky test" in panels["events"]
