"""``--agent all``: Claude Code and Codex sessions together, in every mode.

One Claude Code session (``claude_projects``) and one Codex thread with a
sub-agent child (``codex_home``). Lists merge both agents newest first, each
session is read by its own agent's provider (fold, window, plan limits), and
mixed views tag every session with its agent.
"""

from __future__ import annotations

import asyncio
import json
import os
import threading
import time
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

import codex_records as cr
from pool_coder import web
from pool_coder.cli import build_parser, main
from pool_coder.config import Config
from pool_coder.engine import Engine
from pool_coder.pricing import Pricing
from pool_coder.providers import (
    AGENT_CHOICES,
    CLAUDE,
    CODEX,
    agent_label,
    agent_prefix,
    find_session,
    get_provider,
    list_sessions,
    providers_for,
)
from pool_coder.snapshot import PlanLimitsView
from pool_coder.sources import plan_limits
from pool_coder.sources.codex_limits import CodexLimitsSource

CLAUDE_SID = "aaaaaaaa-1111-2222-3333-444444444444"
CLAUDE_CONTEXT = 10 + 100_000  # input + cache read of its one response


def _claude_session(projects: Path, model: str = "claude-opus-4-8") -> Path:
    path = projects / "C--Git-claudeproj" / f"{CLAUDE_SID}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    base = {"sessionId": CLAUDE_SID, "cwd": "C:\\Git\\claudeproj", "gitBranch": "main"}
    records = [
        {**base, "type": "user", "timestamp": "2026-10-11T09:00:00.000Z",
         "message": {"role": "user", "content": "refactor the parser"}},
        {**base, "type": "assistant", "timestamp": "2026-10-11T09:00:05.000Z", "requestId": "r1",
         "message": {"model": model, "content": [],
                     "usage": {"input_tokens": 10, "output_tokens": 20,
                               "cache_read_input_tokens": 100_000,
                               "cache_creation_input_tokens": 0}}},
    ]
    path.write_text("".join(json.dumps(r) + "\n" for r in records), encoding="utf-8")
    return path


def _codex_session(home: Path) -> Path:
    business = cr.rate_limits(plan_type="business",
                              credits={"has_credits": True, "unlimited": True, "balance": None})
    main_path = cr.write_rollout(home, cr.ROOT, [
        cr.session_meta(cr.ROOT, cwd="C:\\Git\\codexproj", at=cr.ts(0)),
        cr.turn_context("gpt-5.6-terra", cwd="C:\\Git\\codexproj", at=cr.ts(0, 1)),
        cr.task_started(at=cr.ts(0, 2)),
        cr.user_message_item("fix the flaky test", at=cr.ts(0, 3)),
        cr.token_usage("resp_1", inp=100_000, cached=80_000, out=5_000, rs=2_000, at=cr.ts(1)),
        cr.token_count(cr.usage(100_000, 80_000, 0, 5_000, 2_000), rate_limits=business,
                       at=cr.ts(1, 1)),
        cr.sub_agent_item("act_1", "started", cr.CHILD, at=cr.ts(1, 20)),
    ])
    cr.write_rollout(home, cr.CHILD, [
        cr.subagent_meta(cr.CHILD, forked=False, at=cr.ts(1, 20)),
        cr.token_usage("resp_c1", thread_id=cr.CHILD, inp=40_000, cached=30_000, out=2_000,
                       rs=500, at=cr.ts(1, 30)),
    ], local_ts="2026-09-22T23-49-12")
    return main_path


def _set_mtime(path: Path, seconds_ago: float) -> None:
    t = time.time() - seconds_ago
    os.utime(path, (t, t))


@pytest.fixture
def both(claude_projects, codex_home):
    """Both sessions, active; the Claude Code one is the more recent."""
    claude, codex = _claude_session(claude_projects), _codex_session(codex_home)
    _set_mtime(claude, 60)
    _set_mtime(codex, 120)
    return claude, codex


# -- selection ---------------------------------------------------------------------------
def test_agent_choices_and_selection():
    assert AGENT_CHOICES == ("claude", "codex", "all")
    assert providers_for("all") == providers_for(" ALL ") == (CLAUDE, CODEX)
    assert providers_for("codex") == (CODEX,)
    assert providers_for("claude") == providers_for(None) == (CLAUDE,)
    with pytest.raises(ValueError, match="nope"):
        providers_for("nope")
    with pytest.raises(ValueError, match="'all'"):  # a selection, not one agent
        get_provider("all")


def test_agent_label_and_prefix():
    assert [agent_label(a) for a in AGENT_CHOICES] == [
        "Claude Code", "Codex", "Claude Code + Codex"]
    # messages name only a lone non-default agent: "No active Codex sessions"
    assert [agent_prefix(a) for a in AGENT_CHOICES] == ["", "Codex ", ""]


def test_sessions_name_their_agent(both):
    assert [(s.session_id, s.agent) for s in CLAUDE.list_sessions()] == [(CLAUDE_SID, "claude")]
    assert [(s.session_id, s.agent) for s in CODEX.list_sessions()] == [(cr.ROOT, "codex")]
    assert CLAUDE.find_session(CLAUDE_SID).agent == "claude"
    assert CODEX.find_session(cr.CHILD).agent == "codex"


def test_list_sessions_merges_both_agents_newest_first(both):
    claude, _codex = both
    everyone = providers_for("all")
    assert [s.session_id for s in list_sessions(everyone)] == [CLAUDE_SID, cr.ROOT]
    _set_mtime(claude, 300)
    assert [s.session_id for s in list_sessions(everyone)] == [cr.ROOT, CLAUDE_SID]
    assert [s.session_id for s in list_sessions(providers_for("claude"))] == [CLAUDE_SID]


def test_find_session_asks_each_selected_provider(both):
    everyone = providers_for("all")
    assert find_session(everyone, CLAUDE_SID).agent == "claude"
    info = find_session(everyone, cr.ROOT.upper())  # Codex ids match case-insensitively
    assert (info.session_id, info.agent) == (cr.ROOT, "codex")
    assert find_session(everyone, "no-such-session") is None
    assert find_session(providers_for("claude"), cr.ROOT) is None


def test_engine_reads_each_session_with_its_own_agent(both):
    config = Config(agent="all", plan_limits=False)
    sessions = {}
    for info in list_sessions(providers_for("all")):
        engine = Engine(info, config)
        assert engine.provider is get_provider(info.agent)
        sessions[info.agent] = engine.snapshot_once().session
    claude, codex = sessions["claude"], sessions["codex"]
    assert (claude.agent, codex.agent) == ("claude", "codex")
    assert claude.current_context == CLAUDE_CONTEXT
    assert claude.effective_window == 1_000_000            # the opus default
    assert codex.effective_window == cr.WINDOW             # the window Codex reported
    assert [sub.agent_id for sub in codex.subagents] == [cr.CHILD]


def test_claude_preview_keeps_claude_windows_under_all(claude_projects):
    # An OpenAI id in a Claude Code transcript (e.g. behind a gateway) keeps the
    # 200K default window as with --agent claude, never the Codex gpt family's.
    _claude_session(claude_projects, model="gpt-5.6-sol")
    (info,) = CLAUDE.list_sessions()
    for agent in ("claude", "all"):
        ov = CLAUDE.peek_session(info, Config(agent=agent))
        assert ov.occupancy == pytest.approx(CLAUDE_CONTEXT / 200_000)


# -- CLI ---------------------------------------------------------------------------------
def test_parser_accepts_agent_all():
    p = build_parser()
    assert p.parse_args(["--agent", "all"]).agent == "all"
    args = p.parse_args(["--agent", "all", "--all"])  # --all (idle sessions too) is separate
    assert args.agent == "all" and args.all


def test_list_merges_both_agents_with_an_agent_column(both, capsys):
    assert main(["--agent", "all", "--list"]) == 0
    lines = capsys.readouterr().out.splitlines()
    assert lines[0].split() == ["AGE", "CTX", "AGENT", "MODEL", "PROJECT", "LAST"]
    claude, codex = lines[1], lines[2]  # newest first
    assert " claude claude-opus-4-8 " in claude and "claudeproj" in claude
    assert claude.endswith("refactor the parser")
    assert " codex  gpt-5.6-terra " in codex and "codexproj" in codex
    assert codex.endswith("fix the flaky test")
    assert lines[-1] == ("2 session(s). Run `pool-coder --agent all --session <id>` "
                         "or just `pool-coder --agent all`.")


def test_list_without_sessions_of_either_agent(claude_projects, codex_home, capsys):
    assert main(["--agent", "all", "--list"]) == 0
    assert capsys.readouterr().out == (
        "No active sessions in the last 15 min. Use --all to show older ones.\n")


def test_once_session_resolves_either_agent(both, capsys):
    for sid, agent in ((CLAUDE_SID, "claude"), (cr.ROOT, "codex")):
        assert main(["--agent", "all", "--once", "--json", "--no-plan-limits",
                     "--session", sid]) == 0
        session = json.loads(capsys.readouterr().out)["session"]
        assert (session["session_id"], session["agent"]) == (sid, agent)


def test_once_picks_the_most_recently_active_of_either_agent(both, capsys):
    claude, codex = both
    for newer, older, agent in ((claude, codex, "claude"), (codex, claude, "codex")):
        _set_mtime(newer, 60)
        _set_mtime(older, 300)
        assert main(["--agent", "all", "--once", "--json", "--no-plan-limits"]) == 0
        assert json.loads(capsys.readouterr().out)["session"]["agent"] == agent


def test_unknown_session_error_names_agent_all(claude_projects, codex_home, capsys):
    assert main(["--agent", "all", "--once", "--session", "no-such-session"]) == 1
    assert capsys.readouterr().err == (
        "error: no matching session (try `pool-coder --agent all --list`)\n")


# -- web ---------------------------------------------------------------------------------
def test_web_list_tags_each_session_with_its_agent(both):
    config = Config(agent="all")
    frag = web.fragment_list(config)
    assert "active Claude Code + Codex sessions — tap to monitor" in frag
    assert frag.index(f"href='/s/{CLAUDE_SID}'") < frag.index(f"href='/s/{cr.ROOT}'")
    assert cr.CHILD not in frag  # sub-agent threads stay rolled up
    assert "<span class='agent claude'>claude</span>" in frag
    assert "<span class='agent codex'>codex</span>" in frag
    assert "refactor the parser" in frag and "fix the flaky test" in frag
    assert "<title>pool-coder · Claude Code + Codex</title>" in web.page_list(config)


def test_web_list_without_sessions_of_either_agent(claude_projects, codex_home):
    assert "No active sessions in the last 15 min." in web.fragment_list(Config(agent="all"))


def test_single_agent_views_are_not_tagged(both):
    for agent in ("claude", "codex"):
        config = Config(agent=agent, plan_limits=False)
        assert "class='agent" not in web.fragment_list(config)
        (info,) = list_sessions(providers_for(agent))
        frag = web.fragment_all([Engine(info, config).snapshot_once()], config)
        assert f"all active {agent_label(agent)} sessions" in frag
        assert "class='agent" not in frag


class _FakePlan:
    """Stands in for Claude Code's OAuth poller: no network in tests."""

    def __init__(self):
        self.view = PlanLimitsView(available=False, error="claude stub")
        self.started = self.stopped = False

    def start(self):
        self.started = True

    def stop(self):
        self.stopped = True


def _loaded_active(manager: web.EngineManager, timeout: float = 10.0) -> list:
    deadline = time.monotonic() + timeout
    while True:
        snaps = manager.active_snapshots()
        if not any(snap.loading for snap in snaps) or time.monotonic() > deadline:
            return snaps
        time.sleep(0.05)


def test_engine_manager_runs_both_agents_with_their_own_plan_limits(both, monkeypatch):
    monkeypatch.setattr(plan_limits, "PlanLimitsSource", _FakePlan)
    manager = web.EngineManager(Config(agent="all"), Pricing.load())
    try:
        assert manager.providers == (CLAUDE, CODEX)
        assert list(manager.plans) == ["claude", "codex"]
        assert isinstance(manager.plans["claude"], _FakePlan) and manager.plans["claude"].started
        assert isinstance(manager.plans["codex"], CodexLimitsSource)
        manager.plans["codex"].fetch_once()

        snaps = _loaded_active(manager)
        assert [(s.session.session_id, s.session.agent) for s in snaps] == [
            (CLAUDE_SID, "claude"), (cr.ROOT, "codex")]
        claude_plan, codex_plan = (snap.plan_limits for snap in snaps)
        assert claude_plan.error == "claude stub"
        assert codex_plan.available and codex_plan.plan_type == "business"

        # a session page finds either agent's session and reuses its /all engine
        assert manager.snapshot(CLAUDE_SID).session.agent == "claude"
        assert manager.snapshot(cr.ROOT.upper()).session.agent == "codex"
        assert set(manager._engines) == {("claude", CLAUDE_SID), ("codex", cr.ROOT)}
        assert manager.snapshot("no-such-session") is None
    finally:
        manager.stop_all()
    assert manager.plans["claude"].stopped


@pytest.fixture
def all_server(both):
    manager = web.EngineManager(Config(agent="all"), Pricing.load(), enable_plan_limits=False)
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), web._make_handler(manager, None))
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{httpd.server_address[1]}"
    finally:
        httpd.shutdown()
        httpd.server_close()
        manager.stop_all()
        thread.join(timeout=5)


def _get(url: str) -> tuple[int, str]:
    try:
        with urllib.request.urlopen(url, timeout=10) as resp:
            return resp.status, resp.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8")


def _columns(body: str) -> dict[str, str]:
    """Each /all session column's HTML, by session id, in page order."""
    out = {}
    for part in body.split("<article class=session-column data-session-id='")[1:]:
        sid, _, rest = part.partition("'")
        out[sid] = rest.split("</article>")[0]
    return out


def test_http_all_shows_both_agents_sessions(all_server):
    code, body = _get(f"{all_server}/all")
    assert code == 200 and "<title>pool-coder · all Claude Code + Codex sessions</title>" in body
    assert "pool-coder:hidden:all" in body  # hidden sessions are kept apart from one-agent views
    code, body = _get(f"{all_server}/partial/all")
    assert code == 200 and "all active Claude Code + Codex sessions · 2 matched" in body
    columns = _columns(body)
    assert list(columns) == [CLAUDE_SID, cr.ROOT]  # newest first
    claude, codex = columns[CLAUDE_SID], columns[cr.ROOT]
    assert "<span class='agent claude'>claude</span>" in claude
    assert "<span class='agent codex'>codex</span>" in codex
    # each column keeps its own agent's cards
    assert "<tr><td>cache read</td>" in claude and "<tr><td>reasoning</td>" not in claude
    assert "<tr><td>reasoning</td>" in codex and "<tr><td>cache read</td>" not in codex
    # one activity tab per session, whichever its agent
    assert f"data-tab-key='{CLAUDE_SID}'" in body and f"data-tab-key='{cr.ROOT}'" in body


def test_http_session_pages_serve_either_agent(all_server):
    code, body = _get(f"{all_server}/")
    assert code == 200 and "active Claude Code + Codex sessions" in body
    assert f"/s/{CLAUDE_SID}" in body and f"/s/{cr.ROOT}" in body
    code, body = _get(f"{all_server}/s/{CLAUDE_SID}")
    assert code == 200 and f"<title>pool-coder · {CLAUDE_SID[:8]}</title>" in body
    code, body = _get(f"{all_server}/s/{cr.ROOT}")
    assert code == 200 and f"<title>pool-coder · {cr.ROOT[-8:]}</title>" in body
    for sid, agent in ((CLAUDE_SID, "claude"), (cr.ROOT, "codex")):
        code, body = _get(f"{all_server}/api/s/{sid}")
        assert code == 200 and json.loads(body)["session"]["agent"] == agent
    code, _ = _get(f"{all_server}/partial/s/no-such-session")
    assert code == 404


# -- terminal picker ---------------------------------------------------------------------
def test_picker_lists_both_agents_and_opens_either(both):
    from textual.widgets import OptionList, Static

    from pool_coder.ui.app import DashboardScreen, PickerScreen, PoolCoderApp

    async def run():
        app = PoolCoderApp(None, Config(agent="all", plan_limits=False), Pricing.load())
        async with app.run_test(size=(140, 40)) as pilot:
            await pilot.pause()
            screen = app.screen
            assert isinstance(screen, PickerScreen)
            title = str(screen.query_one("#picker-title", Static).content)
            options = screen.query_one(OptionList)
            rows = [(opt.id, str(opt.prompt))
                    for opt in (options.get_option_at_index(i)
                                for i in range(options.option_count))]
            screen._open(cr.ROOT)
            await pilot.pause()
            assert isinstance(app.screen, DashboardScreen)
            return title, rows, app.sub_title, app.screen.engine.provider

    title, rows, sub_title, opened = asyncio.run(run())
    assert sub_title == "Claude Code + Codex"
    assert title.startswith("Select a session to monitor — 2 active (15m).")
    assert [sid for sid, _ in rows] == [CLAUDE_SID, cr.ROOT]
    assert " claude claude-opus-4-8 " in rows[0][1] and "refactor the parser" in rows[0][1]
    assert " codex  gpt-5.6-terra " in rows[1][1] and "fix the flaky test" in rows[1][1]
    assert opened is CODEX
