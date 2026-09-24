"""Web renderer: snapshots -> HTML fragments/pages, with escaping."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from pool_coder import web
from pool_coder.aggregator import Aggregator
from pool_coder.config import Config
from pool_coder.parser import Record
from pool_coder.pricing import Pricing
from pool_coder.snapshot import EventView, PlanLimitsView, Snapshot, build_session_snapshot
from pool_coder.state import SessionState


def _snapshot() -> Snapshot:
    state = SessionState(session_id="abc12345", project_hash="proj")
    agg = Aggregator(state, Config())
    agg.apply("main", Record({"type": "user", "timestamp": "2026-06-20T10:00:00.000Z",
                              "message": {"content": "cleanup a & b"}}))
    # a file path with HTML metacharacters (NOT tag-stripped) proves escaping
    agg.apply("main", Record({
        "type": "assistant", "requestId": "r1", "timestamp": "2026-06-20T10:00:01.000Z",
        "cwd": "F:/proj", "gitBranch": "main",
        "message": {"model": "claude-opus-4-8",
                    "usage": {"input_tokens": 100, "cache_read_input_tokens": 5000, "output_tokens": 50},
                    "content": [{"type": "tool_use", "id": "t1", "name": "Read",
                                 "input": {"file_path": "a<b>.py"}}]},
    }))
    session = build_session_snapshot(state, Config(), Pricing.load())
    return Snapshot(generated_at=datetime.now(timezone.utc), session=session,
                    plan_limits=PlanLimitsView(available=False, error="disabled"))


def test_fragment_has_panels():
    frag = web.fragment_dashboard(_snapshot())
    for token in ("Context window", "Tokens", "Activity", "Recent activity", "class=bar"):
        assert token in frag


def test_html_is_escaped():
    frag = web.fragment_dashboard(_snapshot())
    assert "a<b>.py" not in frag       # the raw path must not leak through
    assert "a&lt;b&gt;.py" in frag     # it is HTML-escaped
    assert "&amp;" in frag             # the "a & b" prompt is escaped too


def test_page_wraps_fragment_with_poller():
    page = web.page_dashboard("abc12345", _snapshot())
    assert "viewport" in page and "/partial/s/abc12345" in page


def test_not_found_fragment():
    assert "not found" in web.fragment_dashboard(None)


def test_snapshot_json_roundtrips():
    import json
    data = json.loads(web.snapshot_json(_snapshot()))
    assert data["session"]["session_id"] == "abc12345"
    assert json.loads(web.snapshot_json(None)) == {"error": "no snapshot"}


# -- Codex ---------------------------------------------------------------------------
# Codex snapshots come from folding real-shaped records (tests/codex_records.py)
# through CodexAggregator; sessions for EngineManager / the list live in the
# ``codex_home`` fixture (never the real ~/.codex).
import dataclasses  # noqa: E402
import json  # noqa: E402
import os  # noqa: E402
import threading  # noqa: E402
import time  # noqa: E402
import urllib.error  # noqa: E402
import urllib.request  # noqa: E402
from http.server import ThreadingHTTPServer  # noqa: E402
from pathlib import Path  # noqa: E402

import pytest  # noqa: E402

import codex_records as cr  # noqa: E402
from pool_coder import overview, paths  # noqa: E402
from pool_coder.codex import overview as codex_overview  # noqa: E402
from pool_coder.codex import paths as codex_paths  # noqa: E402
from pool_coder.codex.aggregator import CodexAggregator  # noqa: E402
from pool_coder.codex.parser import parse_codex_line  # noqa: E402
from pool_coder.overview import SessionOverview  # noqa: E402
from pool_coder.paths import SessionInfo  # noqa: E402
from pool_coder.providers import CLAUDE, CODEX  # noqa: E402
from pool_coder.sources import plan_limits  # noqa: E402
from pool_coder.sources.codex_limits import CodexLimitsSource, parse_rate_limits  # noqa: E402

_AS_OF = datetime(2026, 9, 22, 21, 5, tzinfo=timezone.utc)
_FAR = 4_102_444_800  # 2100-01-01, epoch seconds
_BUSINESS = cr.rate_limits(plan_type="business",
                           credits={"has_credits": True, "unlimited": True, "balance": None})
_WINDOWS = cr.rate_limits(cr.window(40.0, 300, resets_at=_FAR),
                          cr.window(10.0, 10080, resets_at=_FAR))


def _main_records(*, cache_write: int = 0, prompt: str = "fix the flaky test",
                  command: str = "pytest -q", with_limits: bool = False) -> list[dict]:
    recs = [
        cr.session_meta(cr.ROOT, cwd="C:\\Git\\rootproj", at=cr.ts(0)),
        cr.turn_context("gpt-5.6-terra", cwd="C:\\Git\\rootproj", at=cr.ts(0, 1)),
        cr.task_started(at=cr.ts(0, 2)),
        cr.user_message_item(prompt, at=cr.ts(0, 3)),
        cr.token_usage("resp_1", inp=100_000, cached=80_000, cw=cache_write, out=5_000,
                       rs=2_000, at=cr.ts(1)),
        cr.sub_agent_item("act_1", "started", cr.CHILD, at=cr.ts(1, 20)),
        cr.turn_context("gpt-5.6-sol", cwd="C:\\Git\\rootproj", at=cr.ts(1, 40)),
        cr.token_usage("resp_2", inp=120_000, cached=100_000, out=3_000, rs=1_000, at=cr.ts(2)),
        cr.exec_call("call_2", cr.exec_js(command), at=cr.ts(2, 5)),  # still running
    ]
    if with_limits:
        recs.append(cr.token_count(cr.usage(220_000, 180_000, 0, 8_000, 3_000),
                                   rate_limits=_BUSINESS, at=cr.ts(2, 6)))
    return recs


def _child_records() -> list[dict]:
    return [
        cr.subagent_meta(cr.CHILD, forked=False, at=cr.ts(1, 20)),
        cr.task_started(at=cr.ts(1, 21)),
        cr.token_usage("resp_c1", thread_id=cr.CHILD, inp=40_000, cached=30_000, out=2_000,
                       rs=500, at=cr.ts(1, 30)),
    ]


def _codex_snapshot(with_data: bool = True, *, plan: PlanLimitsView | None = None,
                    **kw) -> Snapshot:
    state = SessionState(session_id=cr.ROOT, project_hash="2026/09/22")
    agg = CodexAggregator(state, Config(agent="codex"))
    if with_data:
        for source_id, records in (("main", _main_records(**kw)),
                                   (f"agent:{cr.CHILD}", _child_records())):
            for r in records:
                agg.apply(source_id, parse_codex_line(cr.line(r)))
    session = build_session_snapshot(state, Config(agent="codex"), Pricing.load(),
                                     now=datetime(2026, 9, 22, 21, 0, tzinfo=timezone.utc))
    return Snapshot(generated_at=datetime.now(timezone.utc), session=session, plan_limits=plan)


def _multi_snapshot(sid: str, label: str, offset: int = 0, count: int = 60) -> Snapshot:
    base = _snapshot()
    start = datetime(2026, 6, 20, 10, 0, tzinfo=timezone.utc)
    events = tuple(
        EventView(start + timedelta(seconds=(i * 2) + offset), "text", f"{label}-{i:03d}")
        for i in range(count)
    )
    session = dataclasses.replace(
        base.session, session_id=sid, cwd=f"C:/work/{label}", title=label,
        events=events, agent="claude",
    )
    return dataclasses.replace(base, session=session)


def test_multi_fragment_has_full_columns_and_one_tabbed_activity_card():
    alpha = _multi_snapshot("alpha-session", "Alpha <x>")
    beta = _multi_snapshot("beta-session", "Beta", offset=1)
    frag = web.fragment_all([alpha, beta], Config())

    assert frag.count("class=session-column") == 2
    for title in ("Context window", "Tokens &amp; cost", "Subagents", "Workflows", "Plan limits"):
        assert frag.count(title) == 2
    assert frag.count("Recent activity") == 1
    assert frag.count("role=tab ") == 3  # All + one tab per session
    assert "data-tab-key=all" in frag and "data-tab-key='alpha-session'" in frag
    assert "Alpha <x>" not in frag and "Alpha &lt;x&gt; · alpha-se" in frag

    # Each session contributes only its newest 50 rows, once in All and once
    # in its own tab. The merged All feed keeps timestamp order.
    assert "Alpha &lt;x&gt; · alpha-se" in frag
    assert "Alpha &lt;x&gt;-009" not in frag and frag.count("Alpha &lt;x&gt;-010") == 2
    assert frag.count("Alpha &lt;x&gt;-059") == 2 and frag.count("Beta-059") == 2
    assert (frag.index("Alpha &lt;x&gt;-010") < frag.index("Beta-010")
            < frag.index("Alpha &lt;x&gt;-011"))


def test_multi_page_persists_visibility_and_poll_state_in_the_browser():
    snap = _multi_snapshot("alpha-session", "Alpha")
    page = web.page_all([snap], Config())
    assert "<title>pool-coder · all Claude Code sessions</title>" in page
    assert "/partial/all" in page and "multi-wrap" in page
    assert 'pool-coder:hidden:claude' in page and "localStorage" in page
    assert "data-action=hide" in page and "data-action=show-all" in page
    assert "scrollLeft" in page and "activeTab" in page
    assert "[hidden]{display:none!important;}" in page


def test_multi_fragment_empty_state_is_pollable_and_recoverable():
    frag = web.fragment_all([], Config(agent="codex"))
    assert "all active Codex sessions · 0 matched" in frag
    assert "No sessions in the configured active window" in frag
    assert "Recent activity" in frag and "data-tab-key=all" in frag


_CODEX_PLANS = {
    "none": None,
    "unavailable": PlanLimitsView(available=False, error="no Codex rate-limit data yet"),
    "business": parse_rate_limits(_BUSINESS, _AS_OF),
    "windows": parse_rate_limits(_WINDOWS, _AS_OF),
    "empty": PlanLimitsView(available=True),
}


def _rows(html_text: str) -> list[str]:
    """The key cells of every kv table row, in order."""
    return [part.split("</td>")[0] for part in html_text.split("<tr><td>")[1:]]


def test_codex_fragment_has_every_panel_with_and_without_data():
    for with_data in (True, False):
        for name, plan in _CODEX_PLANS.items():
            frag = web.fragment_dashboard(_codex_snapshot(with_data, plan=plan))
            for token in ("Context window", "Tokens &amp; cost", "Activity", "Subagents",
                          "Workflows", "Plan limits", "Recent activity", "class=bar"):
                assert token in frag, (token, name, with_data)


def test_codex_tokens_card_rows():
    card = web._card_tokens(_codex_snapshot())
    assert _rows(card) == ["input", "cached", "output", "reasoning", "cache hit"]
    assert "3.5k" in card                               # reasoning incl. the child's
    assert "5.6-terra:$" in card and "5.6-sol:$" in card  # per-model costs, short names
    card = web._card_tokens(_codex_snapshot(cache_write=5_000))
    assert _rows(card) == ["input", "cached", "cache write", "output", "reasoning", "cache hit"]


def test_claude_tokens_card_rows_unchanged():
    assert _rows(web._card_tokens(_snapshot())) == [
        "input", "cache read", "cache write", "output", "cache hit"]


def test_codex_plan_card_windows_note_and_as_of():
    card = web._card_plan(_codex_snapshot(plan=_CODEX_PLANS["windows"]))
    assert _rows(card) == ["5-hour", "weekly", "plan", "as of"]
    assert "40%" in card and "10%" in card and "resets" in card and "plus" in card
    assert f"{_AS_OF.astimezone().strftime('%H:%M')} (" in card and "ago)" in card
    assert "class=bar" in card and "width:40.0%" in card

    weekly_only = parse_rate_limits(cr.rate_limits(None, cr.window(55.0, 10080)), _AS_OF)
    card = web._card_plan(_codex_snapshot(plan=weekly_only))
    assert _rows(card) == ["weekly", "plan", "as of"]
    assert "width:55.0%" in card  # the bar falls back to the weekly window


def test_codex_plan_card_window_past_its_reset_shows_no_stale_percent():
    now = datetime.now(timezone.utc)
    plan = PlanLimitsView(available=True, five_hour_pct=87.0,
                          five_hour_resets_at=now - timedelta(hours=4),
                          seven_day_pct=41.0, seven_day_resets_at=now + timedelta(days=3),
                          plan_type="plus", as_of=now - timedelta(hours=6))
    card = web._card_plan(_codex_snapshot(plan=plan))
    assert _rows(card) == ["5-hour", "weekly", "plan", "as of"]
    assert "— · reset, no newer data" in card and "resets now" not in card
    assert "87%" not in card and "width:87" not in card
    assert "width:41.0%" in card  # the bar skips the finished window
    done = PlanLimitsView(available=True, five_hour_pct=87.0,
                          five_hour_resets_at=now - timedelta(seconds=1), as_of=now)
    card = web._card_plan(_codex_snapshot(plan=done))
    assert "class=bar" not in card and "87%" not in card


def test_codex_plan_card_business_note_without_windows():
    card = web._card_plan(_codex_snapshot(plan=_CODEX_PLANS["business"]))
    assert _rows(card) == ["plan", "as of"]
    assert "business · unlimited credits" in card
    assert "class=bar" not in card
    assert "no limit data" in web._card_plan(_codex_snapshot(plan=_CODEX_PLANS["empty"]))
    assert "no Codex rate-limit data yet" in web._card_plan(
        _codex_snapshot(plan=_CODEX_PLANS["unavailable"]))


def test_codex_fragment_is_escaped():
    plan = parse_rate_limits(cr.rate_limits(plan_type="<i>pro</i>", reached="<script>x"), _AS_OF)
    # (prompts are tag-stripped by clean_prompt, so "&" proves their escaping)
    frag = web.fragment_dashboard(_codex_snapshot(
        plan=plan, prompt="cleanup a & b", command="echo <b>hi</b>"))
    for raw in ("<script>x", "<i>pro</i>", "cleanup a & b", "echo <b>hi</b>"):
        assert raw not in frag
    assert "limit reached: &lt;script&gt;x" in frag
    assert "&lt;i&gt;pro&lt;/i&gt;" in frag
    assert "» cleanup a &amp; b" in frag
    assert "echo &lt;b&gt;hi&lt;/b&gt;" in frag


def test_codex_page_title_uses_short_id():
    page = web.page_dashboard(cr.ROOT, _codex_snapshot())
    assert f"<title>pool-coder · {cr.ROOT[-8:]}</title>" in page
    assert f"/partial/s/{cr.ROOT}" in page
    # no snapshot yet: the agent is passed explicitly (the handler does)
    assert f"<title>pool-coder · {cr.ROOT[-8:]}</title>" in web.page_dashboard(cr.ROOT, None, "codex")
    assert "<title>pool-coder · abc12345</title>" in web.page_dashboard("abc12345xyz", None)


def _write_session(home: Path) -> Path:
    main = cr.write_rollout(home, cr.ROOT, _main_records(with_limits=True))
    cr.write_rollout(home, cr.CHILD, _child_records(), local_ts="2026-09-22T23-49-12")
    return main


def test_codex_fragment_list(codex_home):
    _write_session(codex_home)
    cfg = Config(agent="codex")
    frag = web.fragment_list(cfg)
    assert "active Codex sessions — tap to monitor" in frag
    assert "href='/all'>Monitor all</a>" in frag
    assert f"href='/s/{cr.ROOT}'" in frag
    assert cr.CHILD not in frag          # sub-agent threads are rolled up, not listed
    assert "rootproj" in frag and "fix the flaky test" in frag
    assert "<title>pool-coder · Codex</title>" in web.page_list(cfg)


def test_codex_list_marks_a_thread_name_standing_in_for_the_prompt(monkeypatch):
    info = SessionInfo(Path("x.jsonl"), cr.ROOT, "2026/09/22", datetime.now(timezone.utc), 1)
    ov = SessionOverview(info=info, cwd="C:/p", model="gpt-5.6-sol", git_branch=None,
                         context_tokens=1, occupancy=0.5, last_text="Plan <the> editor",
                         is_live=True, last_is_title=True)
    monkeypatch.setattr(codex_paths, "list_sessions", lambda *a, **k: [info])
    monkeypatch.setattr(codex_overview, "peek_codex_session", lambda i, c=None, **k: ov)
    frag = web.fragment_list(Config(agent="codex"))
    assert "<span class=last>[Plan &lt;the&gt; editor]</span>" in frag


def test_codex_fragment_list_empty(codex_home):
    frag = web.fragment_list(Config(agent="codex"))
    assert "No active Codex sessions in the last 30 min." in frag
    assert "href='/all'>Monitor all</a>" in frag


def test_claude_fragment_list_text_unchanged(monkeypatch):
    info = SessionInfo(Path("x.jsonl"), "sess-1", "proj", datetime.now(timezone.utc), 1)
    monkeypatch.setattr(paths, "list_sessions", lambda *a, **k: [info])
    monkeypatch.setattr(overview, "peek_session", lambda i, c=None, **k: SessionOverview(
        info=i, cwd="C:/p", model="claude-opus-4-8", git_branch=None, context_tokens=1,
        occupancy=0.5, last_text="hi", is_live=True))
    frag = web.fragment_list(Config())
    assert "active Claude Code sessions — tap to monitor" in frag
    assert "href='/s/sess-1'" in frag
    assert "<title>pool-coder</title>" in web.page_list(Config())
    monkeypatch.setattr(paths, "list_sessions", lambda *a, **k: [])
    assert "No active sessions in the last 30 min." in web.fragment_list(Config())


def _loaded(manager: web.EngineManager, sid: str, timeout: float = 10.0) -> Snapshot | None:
    deadline = time.monotonic() + timeout
    while True:
        snap = manager.snapshot(sid)
        if snap is None or not snap.loading or time.monotonic() > deadline:
            return snap
        time.sleep(0.05)


def test_engine_manager_codex_finds_and_folds_a_session(codex_home):
    _write_session(codex_home)
    manager = web.EngineManager(Config(agent="codex"), Pricing.load(), enable_plan_limits=False)
    try:
        assert manager.provider is CODEX and manager.plan is None
        snap = _loaded(manager, cr.ROOT)
        assert snap is not None and not snap.loading
        s = snap.session
        assert s.agent == "codex" and s.session_id == cr.ROOT
        assert s.effective_window == cr.WINDOW
        assert [sub.agent_id for sub in s.subagents] == [cr.CHILD]
        assert "reasoning" in web.fragment_dashboard(snap)
        assert manager.snapshot("no-such-thread") is None
    finally:
        manager.stop_all()


def test_engine_manager_reuses_an_engine_before_looking_up(codex_home):
    _write_session(codex_home)
    manager = web.EngineManager(Config(agent="codex"), Pricing.load(), enable_plan_limits=False)
    lookups: list[str] = []

    def find(sid):
        lookups.append(sid)
        return CODEX.find_session(sid)

    manager.provider = dataclasses.replace(CODEX, find_session=find)
    try:
        for _ in range(3):
            assert manager.snapshot(cr.ROOT) is not None
        assert lookups == [cr.ROOT]
        assert len(manager._engines) == 1
    finally:
        manager.stop_all()


def test_engine_manager_active_batch_has_no_cap_reuses_and_evicts(monkeypatch):
    now = datetime.now(timezone.utc)
    active = [
        SessionInfo(Path(f"s{i}.jsonl"), f"session-{i}", f"project-{i}",
                    now - timedelta(seconds=i), i)
        for i in range(10)
    ]
    stale = SessionInfo(Path("stale.jsonl"), "stale", "old", now - timedelta(hours=2), 1)
    created: list[FakeEngine] = []

    class FakeEngine:
        def __init__(self, info, config, pricing, enable_plan_limits=False):
            self.state = type("FakeState", (), {"main_path": str(info.main_path)})()
            base = _snapshot()
            session = dataclasses.replace(base.session, session_id=info.session_id,
                                          project_hash=info.project_hash)
            self.snap = dataclasses.replace(base, session=session)
            self.started = False
            self.stopped = False
            created.append(self)

        def start(self):
            self.started = True

        def stop(self):
            self.stopped = True

        def get_snapshot(self):
            return self.snap

    class FakePlan:
        view = PlanLimitsView(available=False, error="shared")
        stopped = False

        def stop(self):
            self.stopped = True

    monkeypatch.setattr(web, "Engine", FakeEngine)
    monkeypatch.setattr(web, "_transcript_gone", lambda engine: False)
    manager = web.EngineManager(Config(), Pricing.load(), enable_plan_limits=False,
                                max_engines=2, idle_ttl=0)
    manager.plan = FakePlan()
    manager.provider = dataclasses.replace(
        CLAUDE, list_sessions=lambda: [stale, *reversed(active)])
    try:
        snaps = manager.active_snapshots()
        assert [snap.session.session_id for snap in snaps] == [f"session-{i}" for i in range(10)]
        assert len(manager._engines) == 10 and len(created) == 10
        assert all(engine.started and not engine.stopped for engine in created)
        assert all(snap.plan_limits.error == "shared" for snap in snaps)

        # Later activity changes do not reshuffle existing columns. A newly
        # discovered session appends even when it is the most recently active.
        reranked = [dataclasses.replace(info, mtime=now + timedelta(seconds=i))
                    for i, info in enumerate(active)]
        newest = SessionInfo(Path("new.jsonl"), "session-new", "new-project",
                             now + timedelta(minutes=1), 1)
        manager.provider = dataclasses.replace(
            manager.provider, list_sessions=lambda: [newest, *reversed(reranked)])
        snaps = manager.active_snapshots()
        assert [snap.session.session_id for snap in snaps] == [
            *(f"session-{i}" for i in range(10)), "session-new"]
        assert len(created) == 11

        # A third batch reuses every engine despite the ordinary cap of two.
        manager.active_snapshots()
        assert len(created) == 11

        # Once the batch no longer protects those ids, normal idle eviction applies.
        manager.provider = dataclasses.replace(manager.provider, list_sessions=lambda: [])
        for sid in manager._last:
            manager._last[sid] -= 1
        assert manager.active_snapshots() == []
        assert manager._engines == {} and all(engine.stopped for engine in created)
    finally:
        manager.stop_all()


def test_engine_manager_one_engine_per_thread_however_its_id_is_spelled(codex_home):
    _write_session(codex_home)
    manager = web.EngineManager(Config(agent="codex"), Pricing.load(), enable_plan_limits=False)
    lookups: list[str] = []

    def find(sid):
        lookups.append(sid)
        return CODEX.find_session(sid)

    manager.provider = dataclasses.replace(CODEX, find_session=find)
    try:
        for sid in (cr.ROOT, cr.ROOT.upper(), f" {cr.ROOT}\t"):
            snap = manager.snapshot(sid)
            assert snap is not None and snap.session.session_id == cr.ROOT
        assert lookups == [cr.ROOT] and list(manager._engines) == [cr.ROOT]
    finally:
        manager.stop_all()


CLAUDE_SID = "aaaaaaaa-1111-2222-3333-444444444444"


def _claude_transcript(projects: Path, project: str = "c--proj") -> Path:
    path = projects / project / f"{CLAUDE_SID}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    record = {"type": "assistant", "timestamp": "2026-09-24T10:00:00.000Z", "requestId": "r1",
              "sessionId": CLAUDE_SID,
              "message": {"model": "claude-opus-4-8", "content": [],
                          "usage": {"input_tokens": 10, "output_tokens": 20,
                                    "cache_read_input_tokens": 100,
                                    "cache_creation_input_tokens": 0}}}
    path.write_text(json.dumps(record) + "\n", encoding="utf-8")
    return path


def test_engine_manager_drops_an_engine_whose_transcript_was_deleted(tmp_path, monkeypatch):
    projects = tmp_path / "projects"
    path = _claude_transcript(projects)
    lookups: list[str] = []
    real_find = paths.find_session

    def find(sid, root=None):
        lookups.append(sid)
        return real_find(sid, root=projects)

    monkeypatch.setattr(paths, "find_session", find)
    manager = web.EngineManager(Config(), Pricing.load(), enable_plan_limits=False)
    try:
        assert _loaded(manager, CLAUDE_SID).session.cumulative.output == 20
        assert manager.snapshot(CLAUDE_SID) is not None and lookups == [CLAUDE_SID]
        path.unlink()
        # as before the engine reuse: a transcript that is gone is a 404
        assert manager.snapshot(CLAUDE_SID) is None
        assert manager._engines == {} and len(lookups) == 2
        assert manager.snapshot(CLAUDE_SID) is None
        # moved to another project folder: found again and served
        _claude_transcript(projects, "c--other")
        snap = _loaded(manager, CLAUDE_SID)
        assert snap is not None and snap.session.project_hash == "c--other"
        assert len(manager._engines) == 1
    finally:
        manager.stop_all()


def test_engine_manager_plan_source_comes_from_the_provider(codex_home, monkeypatch):
    _write_session(codex_home)
    manager = web.EngineManager(Config(agent="codex"), Pricing.load())
    try:
        assert isinstance(manager.plan, CodexLimitsSource)
        manager.plan.fetch_once()
        snap = _loaded(manager, cr.ROOT)
        assert snap.plan_limits is not None and snap.plan_limits.available
        assert snap.plan_limits.plan_type == "business"
        assert "business · unlimited credits" in web.fragment_dashboard(snap)
    finally:
        manager.stop_all()

    # Claude still gets its OAuth poller (stubbed here: no network in tests)
    class FakePlan:
        view = PlanLimitsView(available=False, error="stub")
        started = stopped = False

        def start(self):
            self.started = True

        def stop(self):
            self.stopped = True

    monkeypatch.setattr(plan_limits, "PlanLimitsSource", FakePlan)
    manager = web.EngineManager(Config(), Pricing.load())
    assert manager.provider is CLAUDE
    assert isinstance(manager.plan, FakePlan) and manager.plan.started
    manager.stop_all()
    assert manager.plan.stopped


@pytest.fixture
def codex_server(codex_home):
    _write_session(codex_home)
    manager = web.EngineManager(Config(agent="codex"), Pricing.load(), enable_plan_limits=False)
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), web._make_handler(manager, None))
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{httpd.server_address[1]}", manager
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


def test_codex_http_endpoints(codex_server):
    base, manager = codex_server
    code, body = _get(f"{base}/")
    assert code == 200 and "active Codex sessions" in body
    code, body = _get(f"{base}/partial/list")
    assert code == 200 and f"/s/{cr.ROOT}" in body and "href='/all'" in body
    code, body = _get(f"{base}/all")
    assert code == 200 and "all Codex sessions</title>" in body
    assert "/partial/all" in body and f"data-session-id='{cr.ROOT}'" in body
    code, body = _get(f"{base}/partial/all")
    assert code == 200 and "all active Codex sessions" in body
    assert body.count("class=session-column") == 1
    code, body = _get(f"{base}/s/{cr.ROOT}")
    assert code == 200 and f"<title>pool-coder · {cr.ROOT[-8:]}</title>" in body
    _loaded(manager, cr.ROOT)
    code, body = _get(f"{base}/partial/s/{cr.ROOT}")
    assert code == 200 and "reasoning" in body and "dependency_audit" in body
    code, body = _get(f"{base}/api/s/{cr.ROOT}")
    data = json.loads(body)
    assert code == 200 and data["session"]["agent"] == "codex"
    assert data["session"]["cumulative"]["reasoning"] == 3_500
    code, _ = _get(f"{base}/partial/s/no-such-thread")
    assert code == 404


def test_all_endpoint_discovers_and_drops_sessions(codex_server, codex_home):
    base, _manager = codex_server
    other = "02b1dbef-e837-4df3-bc04-29ddddedccfd"
    path = cr.write_rollout(
        codex_home, other,
        [cr.session_meta(other, cwd="C:\\Git\\new-project"),
         cr.turn_context("gpt-5.6-terra", cwd="C:\\Git\\new-project")],
        local_ts="2026-09-22T23-59-59",
    )
    code, body = _get(f"{base}/partial/all")
    assert code == 200 and f"data-session-id='{other}'" in body
    assert body.count("class=session-column") == 2

    old = time.time() - 7200
    os.utime(path, (old, old))
    code, body = _get(f"{base}/partial/all")
    assert code == 200 and f"data-session-id='{other}'" not in body
    assert body.count("class=session-column") == 1
