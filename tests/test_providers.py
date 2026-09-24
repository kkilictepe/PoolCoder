"""The agent-provider seam: lookup, call-time wrappers, and the engine wiring."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from codex_records import (
    CHILD,
    ROOT,
    WINDOW,
    rate_limits,
    session_meta,
    subagent_meta,
    task_started,
    token_count,
    token_usage,
    ts,
    turn_context,
    usage,
    write_rollout,
)
from pool_coder import providers
from pool_coder.aggregator import Aggregator
from pool_coder.codex import paths as cpaths
from pool_coder.codex.aggregator import CodexAggregator
from pool_coder.codex.discovery import CodexDiscovery
from pool_coder.codex.parser import parse_codex_line
from pool_coder.config import Config
from pool_coder.discovery import SessionDiscovery
from pool_coder.engine import Engine
from pool_coder.overview import SessionOverview
from pool_coder.parser import parse_line
from pool_coder.paths import SessionInfo
from pool_coder.providers import CLAUDE, CODEX, PROVIDERS, Provider, get_provider
from pool_coder.sources.codex_limits import CodexLimitsSource
from pool_coder.sources.jsonl_source import JsonlSource
from pool_coder.sources.plan_limits import PlanLimitsSource
from pool_coder.state import SessionState


@pytest.fixture
def codex_session(codex_home):
    """A root thread (with a reported window and rate limits) and one child."""
    write_rollout(codex_home, ROOT, [
        session_meta(ROOT, cwd="C:\\Git\\rootproj", at=ts(0)),
        turn_context("gpt-5.6-sol", cwd="C:\\Git\\rootproj", at=ts(0, 1)),
        task_started(at=ts(0, 2)),
        token_usage("r1", inp=10_000, cached=6_000, out=500, at=ts(1)),
        token_count(usage(10_000, 6_000, 0, 500), at=ts(1, 1),
                    rate_limits=rate_limits(plan_type="business",
                                            credits={"has_credits": True, "unlimited": True,
                                                     "balance": None})),
    ])
    write_rollout(codex_home, CHILD, [
        subagent_meta(CHILD, forked=False, at=ts(0, 30)),
        token_usage("c1", thread_id=CHILD, inp=2_000, out=100, at=ts(0, 40)),
    ], local_ts="2026-09-22T23-48-12")
    return cpaths.find_session(ROOT)


@pytest.fixture
def claude_info(tmp_path):
    main = tmp_path / "proj" / "11111111-2222-3333-4444-555555555555.jsonl"
    main.parent.mkdir()
    main.write_text("", encoding="utf-8")
    return SessionInfo(main, main.stem, "proj", datetime.now(timezone.utc), 0)


# -- lookup ------------------------------------------------------------------------
def test_get_provider_defaults_to_claude():
    assert get_provider(None) is CLAUDE
    assert get_provider("") is CLAUDE
    assert get_provider("claude") is CLAUDE


def test_get_provider_codex_and_normalisation():
    assert get_provider("codex") is CODEX
    assert get_provider(" Codex ") is CODEX


def test_get_provider_unknown_raises():
    with pytest.raises(ValueError, match="nope"):
        get_provider("nope")


def test_provider_identity_fields():
    assert PROVIDERS == {"claude": CLAUDE, "codex": CODEX}
    assert (CLAUDE.name, CLAUDE.label, CLAUDE.version_tag) == ("claude", "Claude Code", "cc")
    assert (CODEX.name, CODEX.label, CODEX.version_tag) == ("codex", "Codex", "codex")
    assert isinstance(CLAUDE, Provider)
    with pytest.raises(AttributeError):
        CODEX.name = "x"  # type: ignore[misc]  # frozen


def test_providers_module_imports_no_ui():
    import ast
    import inspect

    tree = ast.parse(inspect.getsource(providers))
    mods = [n.module or "" for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)]
    mods += [a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names]
    assert not [m for m in mods if "ui" in m.split(".") or m.split(".")[0] in ("textual", "rich")]


# -- wrappers look the module functions up at call time ---------------------------------
def test_claude_wrappers_are_resolved_at_call_time(monkeypatch):
    from pool_coder import overview, paths

    sentinel = object()
    monkeypatch.setattr(paths, "list_sessions", lambda *a, **k: [sentinel])
    monkeypatch.setattr(paths, "find_session", lambda sid, *a, **k: (sid, sentinel))
    monkeypatch.setattr(overview, "peek_session", lambda info, config=None, **k: (info, config))
    cfg = Config()
    assert CLAUDE.list_sessions() == [sentinel]
    assert CLAUDE.find_session("abc") == ("abc", sentinel)
    assert CLAUDE.peek_session("info", cfg) == ("info", cfg)
    assert CLAUDE.peek_session("info") == ("info", None)


def test_codex_wrappers_are_resolved_at_call_time(monkeypatch):
    from pool_coder.codex import overview as cov

    sentinel = object()
    monkeypatch.setattr(cpaths, "list_sessions", lambda *a, **k: [sentinel])
    monkeypatch.setattr(cpaths, "find_session", lambda sid, *a, **k: (sid, sentinel))
    monkeypatch.setattr(cov, "peek_codex_session", lambda info, config=None, **k: (info, config))
    cfg = Config(agent="codex")
    assert CODEX.list_sessions() == [sentinel]
    assert CODEX.find_session("abc") == ("abc", sentinel)
    assert CODEX.peek_session("info", cfg) == ("info", cfg)


# -- Codex provider against a fixture home ----------------------------------------------------
def test_codex_list_hides_children_and_find_allows_them(codex_session):
    assert [s.session_id for s in CODEX.list_sessions()] == [ROOT]
    child = CODEX.find_session(CHILD)
    assert child is not None and child.session_id == CHILD
    assert CODEX.find_session("00000000-0000-0000-0000-000000000000") is None


def test_normalize_id_matches_what_find_session_matches(codex_session):
    # Codex matches thread ids case-insensitively; Claude ids stay exact
    assert CODEX.normalize_id(f" {ROOT.upper()}\n") == ROOT
    assert CODEX.find_session(f" {ROOT.upper()}\n").session_id == ROOT
    assert CLAUDE.normalize_id(" AbC-1 ") == " AbC-1 "


def test_codex_peek_returns_overview(codex_session):
    ov = CODEX.peek_session(codex_session, Config(agent="codex"))
    assert isinstance(ov, SessionOverview)
    assert ov.model == "gpt-5.6-sol" and ov.label == "rootproj"
    assert ov.context_tokens == 10_000
    assert ov.occupancy == pytest.approx(10_000 / WINDOW)


def test_open_session_claude(claude_info):
    state = SessionState(session_id=claude_info.session_id, main_path=str(claude_info.main_path))
    fold, source = CLAUDE.open_session(state, Config())
    assert type(fold) is Aggregator and fold.state is state
    assert isinstance(source, JsonlSource) and source.agg is fold
    assert isinstance(source.discovery, SessionDiscovery)
    assert source.parse is parse_line
    assert source.main_path == claude_info.main_path
    assert state.agent == "claude"


def test_open_session_codex(codex_session):
    state = SessionState(session_id=ROOT, main_path=str(codex_session.main_path))
    fold, source = CODEX.open_session(state, Config(agent="codex"))
    assert isinstance(fold, CodexAggregator) and fold.state is state
    assert isinstance(source, JsonlSource) and source.agg is fold
    assert isinstance(source.discovery, CodexDiscovery)
    assert source.discovery.session_id == ROOT
    assert source.parse is parse_codex_line
    assert state.agent == "codex"


def test_make_plan_source():
    claude_plan = CLAUDE.make_plan_source()
    codex_plan = CODEX.make_plan_source()
    assert isinstance(claude_plan, PlanLimitsSource)
    assert isinstance(codex_plan, CodexLimitsSource)
    # built, not started: no thread and no fetch until the engine asks
    assert claude_plan._thread is None and codex_plan._thread is None
    assert claude_plan.view.error == codex_plan.view.error == "not polled yet"
    assert CODEX.make_plan_source() is not codex_plan


# -- engine wiring -----------------------------------------------------------------------------
def test_engine_codex_uses_codex_fold_and_limits(codex_session):
    engine = Engine(codex_session, Config(agent="codex"))
    assert engine.provider is CODEX
    assert isinstance(engine.agg, CodexAggregator)
    assert isinstance(engine.plan, CodexLimitsSource)
    assert isinstance(engine.jsonl.discovery, CodexDiscovery)
    assert engine.state.agent == "codex"

    snap = engine.snapshot_once()
    s = snap.session
    assert s.agent == "codex" and s.effective_window == WINDOW
    assert snap.files_watched == 2
    assert [sub.agent_id for sub in s.subagents] == [CHILD]
    assert s.subagents[0].tokens == 2_100
    assert snap.plan_limits.available and snap.plan_limits.plan_type == "business"
    assert snap.plan_limits.note == "unlimited credits"


def test_engine_codex_without_plan_limits(codex_session):
    assert Engine(codex_session, Config(agent="codex"), enable_plan_limits=False).plan is None
    assert Engine(codex_session, Config(agent="codex", plan_limits=False)).plan is None


def test_engine_claude_unchanged(claude_info):
    engine = Engine(claude_info, Config())
    assert engine.provider is CLAUDE
    assert type(engine.agg) is Aggregator
    assert isinstance(engine.plan, PlanLimitsSource)
    assert isinstance(engine.jsonl.discovery, SessionDiscovery)
    assert engine.jsonl.parse is parse_line
    assert engine.state.agent == "claude"


def test_engine_claude_snapshot_once(claude_info):
    snap = Engine(claude_info, Config(), enable_plan_limits=False).snapshot_once()
    assert snap.session.agent == "claude"
    assert snap.plan_limits is None and snap.files_watched == 1


def test_engine_unknown_agent_raises(claude_info):
    with pytest.raises(ValueError):
        Engine(claude_info, Config(agent="nope"))
