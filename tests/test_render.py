"""The render layer turns snapshots into Rich panels without exploding."""

from __future__ import annotations

import dataclasses
from datetime import datetime, timedelta, timezone

from rich.panel import Panel

from pool_coder.aggregator import Aggregator
from pool_coder.config import Config
from pool_coder.parser import Record
from pool_coder.pricing import Pricing
from pool_coder.snapshot import PlanLimitsView, Snapshot, build_session_snapshot
from pool_coder.state import SessionState
from pool_coder.ui import render

_ALL = (
    render.header_bar, render.panel_context, render.panel_tokens, render.panel_activity,
    render.panel_plan, render.panel_subagents, render.panel_workflows, render.panel_events,
)


def _snapshot(with_data: bool) -> Snapshot:
    state = SessionState(session_id="abc12345", project_hash="proj")
    agg = Aggregator(state, Config())
    if with_data:
        agg.apply("main", Record({
            "type": "assistant", "requestId": "r1", "timestamp": "2026-06-20T10:00:00.000Z",
            "cwd": "F:/proj", "gitBranch": "main",
            "message": {"model": "claude-opus-4-8",
                        "usage": {"input_tokens": 100, "cache_read_input_tokens": 5000, "output_tokens": 50},
                        "content": [{"type": "tool_use", "id": "t1", "name": "Read", "input": {"file_path": "a.py"}}]},
        }))
        agg.register_subagent("a1", "Explore", "map the repo", "t1")
        agg.register_workflow("wf1", "review", "adversarial", [("Review", ""), ("Verify", "")])
        agg.apply("wfjournal:wf1", Record({"type": "started", "key": "k1"}))
    session = build_session_snapshot(state, Config(), Pricing.load())
    return Snapshot(generated_at=datetime.now(timezone.utc), session=session,
                    plan_limits=PlanLimitsView(available=False, error="disabled"))


def test_panels_render_with_data():
    snap = _snapshot(True)
    for fn in _ALL:
        assert isinstance(fn(snap), Panel), fn.__name__


def test_panels_render_when_empty():
    snap = _snapshot(False)
    for fn in _ALL:
        assert isinstance(fn(snap), Panel), fn.__name__


def test_bar_clamps():
    assert len(render.bar(2.0, 10).plain) == 10
    assert len(render.bar(-1.0, 10).plain) == 10
    assert len(render.bar(None, 10).plain) == 10


# -- Codex ---------------------------------------------------------------------------
# Codex snapshots come from folding real-shaped records (tests/codex_records.py)
# through CodexAggregator, exactly as the engine does.
import io  # noqa: E402

from rich.console import Console  # noqa: E402

import codex_records as cr  # noqa: E402
from pool_coder.codex.aggregator import CodexAggregator  # noqa: E402
from pool_coder.codex.parser import parse_codex_line  # noqa: E402
from pool_coder.sources.codex_limits import parse_rate_limits  # noqa: E402

_AS_OF = datetime(2026, 9, 22, 21, 5, tzinfo=timezone.utc)
_FAR = 4_102_444_800  # 2100-01-01, epoch seconds
_BUSINESS = cr.rate_limits(plan_type="business",
                           credits={"has_credits": True, "unlimited": True, "balance": None})


def _text(renderable) -> str:
    con = Console(record=True, width=120, file=io.StringIO())
    con.print(renderable)
    return con.export_text()


def _codex_snapshot(with_data: bool = True, *, cache_write: int = 0,
                    plan: PlanLimitsView | None = None, description: str | None = None) -> Snapshot:
    state = SessionState(session_id=cr.ROOT, project_hash="2026/09/22")
    agg = CodexAggregator(state, Config(agent="codex"))

    def feed(source_id: str, records: list[dict]) -> None:
        for r in records:
            agg.apply(source_id, parse_codex_line(cr.line(r)))

    if with_data:
        feed("main", [
            cr.session_meta(cr.ROOT, at=cr.ts(0)),
            cr.turn_context("gpt-5.6-terra", at=cr.ts(0, 1)),
            cr.task_started(at=cr.ts(0, 2)),
            cr.user_message_item("fix the flaky test", at=cr.ts(0, 3)),
            cr.token_usage("resp_1", inp=100_000, cached=80_000, cw=cache_write, out=5_000,
                           rs=2_000, at=cr.ts(1)),
            cr.exec_call("call_1", cr.exec_js("git status"), at=cr.ts(1, 5)),
            cr.exec_output("call_1", at=cr.ts(1, 10)),
            cr.sub_agent_item("act_1", "started", cr.CHILD, at=cr.ts(1, 20)),
            cr.compacted(at=cr.ts(1, 30)),
            cr.turn_context("gpt-5.6-sol", at=cr.ts(1, 40)),
            cr.token_usage("resp_2", inp=120_000, cached=100_000, out=3_000, rs=1_000,
                           at=cr.ts(2)),
            cr.exec_call("call_2", cr.exec_js("pytest -q"), at=cr.ts(2, 5)),  # still running
        ])
        feed(f"agent:{cr.CHILD}", [
            cr.subagent_meta(cr.CHILD, forked=False, at=cr.ts(1, 20)),
            cr.task_started(at=cr.ts(1, 21)),
            cr.token_usage("resp_c1", thread_id=cr.CHILD, inp=40_000, cached=30_000, out=2_000,
                           rs=500, at=cr.ts(1, 30)),
        ])
        if description is not None:
            agg.register_subagent(cr.CHILD, "dependency_audit", description, "")
    session = build_session_snapshot(state, Config(agent="codex"), Pricing.load(),
                                     now=datetime(2026, 9, 22, 21, 0, tzinfo=timezone.utc))
    return Snapshot(generated_at=datetime.now(timezone.utc), session=session, plan_limits=plan)


_CODEX_PLANS = {
    "none": None,
    "unavailable": PlanLimitsView(available=False, error="no Codex rate-limit data yet"),
    "business": parse_rate_limits(_BUSINESS, _AS_OF),
    "windows": parse_rate_limits(cr.rate_limits(cr.window(40.0, 300, resets_at=_FAR),
                                                cr.window(10.0, 10080, resets_at=_FAR)), _AS_OF),
    "empty": PlanLimitsView(available=True),
}


def test_codex_panels_render_with_and_without_data():
    for with_data in (True, False):
        for name, plan in _CODEX_PLANS.items():
            snap = _codex_snapshot(with_data, plan=plan)
            assert snap.session.agent == "codex"
            for fn in _ALL:
                panel = fn(snap)
                assert isinstance(panel, Panel), (fn.__name__, name, with_data)
                assert _text(panel).strip(), (fn.__name__, name, with_data)


def test_codex_tokens_panel_shows_cached_and_reasoning():
    text = _text(render.panel_tokens(_codex_snapshot()))
    for row in ("input", "cached", "output", "reasoning", "cache hit"):
        assert row in text
    assert "cache read" not in text          # Claude's label
    assert "cache write" not in text         # hidden while 0
    assert "3.5k" in text                    # reasoning: main 2,000 + 1,000, child 500


def test_codex_tokens_panel_shows_cache_write_when_positive():
    text = _text(render.panel_tokens(_codex_snapshot(cache_write=5_000)))
    assert "cache write" in text and "5.0k" in text


def test_codex_per_model_costs_use_short_model():
    snap = _codex_snapshot()
    assert {m for m, _ in snap.session.cost.by_model} == {"gpt-5.6-terra", "gpt-5.6-sol"}
    text = _text(render.panel_tokens(snap))
    assert "5.6-terra:$" in text and "5.6-sol:$" in text
    assert "gpt-5.6-sol:" not in text


def test_codex_header_uses_short_id():
    text = _text(render.header_bar(_codex_snapshot()))
    assert f"pool-coder · {cr.ROOT[-8:]}" in text
    assert cr.ROOT[:8] not in text  # UUIDv7 prefixes collide; Codex shows the tail


def test_claude_header_and_tokens_keep_their_labels():
    snap = _snapshot(True)
    assert "pool-coder · abc12345" in _text(render.header_bar(snap))
    text = _text(render.panel_tokens(snap))
    assert "cache read" in text and "cache write" in text
    assert "reasoning" not in text and "cached" not in text


def test_claude_per_model_costs_keep_their_labels_and_prices():
    # Claude Code behind a gateway can record an OpenAI id: labelled and priced
    # as before Codex support ("mini", the default rate)
    state = SessionState(session_id="abc12345-0000-0000-0000-000000000000")
    agg = Aggregator(state)
    for i, model in enumerate(("claude-opus-4-8", "o4-mini")):
        agg.apply("main", Record({"type": "assistant", "requestId": f"r{i}",
                                  "timestamp": "2026-06-20T10:00:00.000Z",
                                  "message": {"model": model, "content": [],
                                              "usage": {"input_tokens": 1_000_000}}}))
    session = build_session_snapshot(state, Config(), Pricing.load())
    assert dict(session.cost.by_model) == {"claude-opus-4-8": 5.0, "o4-mini": 5.0}
    text = _text(render.panel_tokens(Snapshot(generated_at=datetime.now(timezone.utc),
                                              session=session)))
    assert "mini:$5.00" in text and "o4-mini:" not in text


def test_codex_plan_panel_shows_labelled_windows_note_and_as_of():
    text = _text(render.panel_plan(_codex_snapshot(plan=_CODEX_PLANS["windows"])))
    assert "5-hour" in text and "40%" in text
    assert "weekly" in text and "10%" in text
    assert "resets" in text
    assert "plus" in text                                            # plan_type
    assert f"as of {_AS_OF.astimezone().strftime('%H:%M')} (" in text and "ago)" in text
    assert "wk opus" not in text and "wk sonnet" not in text         # Claude-only rows
    assert "█" in text                                               # 5-hour bar


def test_codex_plan_panel_shows_only_non_null_windows():
    weekly_only = parse_rate_limits(cr.rate_limits(None, cr.window(55.0, 10080)), _AS_OF)
    text = _text(render.panel_plan(_codex_snapshot(plan=weekly_only)))
    assert "weekly" in text and "55%" in text
    assert "5-hour" not in text
    assert "█" in text  # the bar falls back to the weekly window

    hourly = parse_rate_limits(cr.rate_limits(cr.window(7.0, 60), None), _AS_OF)
    text = _text(render.panel_plan(_codex_snapshot(plan=hourly)))
    assert "1h" in text and "7%" in text and "weekly" not in text and "5-hour" not in text


def test_codex_plan_panel_window_past_its_reset_shows_no_stale_percent():
    now = datetime.now(timezone.utc)
    plan = PlanLimitsView(available=True, five_hour_pct=87.0,
                          five_hour_resets_at=now - timedelta(hours=4),
                          seven_day_pct=41.0, seven_day_resets_at=now + timedelta(days=3),
                          plan_type="plus", as_of=now - timedelta(hours=6))
    text = _text(render.panel_plan(_codex_snapshot(plan=plan)))
    assert "reset, no newer data" in text and "resets now" not in text
    assert "87%" not in text and "41%" in text
    assert "(6h ago)" in text
    assert text.count("█") == round(0.41 * 34)  # the bar skips the finished window
    # only finished windows: no bar at all
    done = dataclasses.replace(plan, seven_day_resets_at=now - timedelta(minutes=1))
    text = _text(render.panel_plan(_codex_snapshot(plan=done)))
    assert "█" not in text and "░" not in text and "41%" not in text
    # an unknown reset time never expires
    unknown = dataclasses.replace(plan, five_hour_resets_at=None)
    assert "87%" in _text(render.panel_plan(_codex_snapshot(plan=unknown)))


def test_codex_plan_panel_with_both_windows_null_shows_the_note():
    text = _text(render.panel_plan(_codex_snapshot(plan=_CODEX_PLANS["business"])))
    assert "business · unlimited credits" in text
    assert "as of" in text
    assert "5-hour" not in text and "weekly" not in text
    assert "█" not in text and "░" not in text  # no usage bar without a window


def test_codex_plan_panel_without_any_data():
    text = _text(render.panel_plan(_codex_snapshot(plan=_CODEX_PLANS["empty"])))
    assert "no limit data" in text
    text = _text(render.panel_plan(_codex_snapshot(plan=_CODEX_PLANS["unavailable"])))
    assert "no Codex rate-limit data yet" in text


def test_codex_record_text_is_never_parsed_as_markup():
    # Rich table cells parse markup: a stray "[/x]" would raise MarkupError.
    plan = parse_rate_limits(cr.rate_limits(plan_type="[b]pro", reached="[/x] weekly"), _AS_OF)
    snap = _codex_snapshot(plan=plan, description="Pauli · /root/[/odd]")
    assert "limit reached: [/x] weekly" in _text(render.panel_plan(snap))
    assert "[b]pro" in _text(render.panel_plan(snap))
    assert "/root/[/odd]" in _text(render.panel_subagents(snap))


def test_codex_subagents_activity_and_context_panels():
    snap = _codex_snapshot()
    subs = _text(render.panel_subagents(snap))
    assert "dependency_audit" in subs and "1 running" in subs
    act = _text(render.panel_activity(snap))
    assert "shell" in act and "pytest -q" in act   # the in-flight exec call
    ctx = _text(render.panel_context(snap))
    assert f"/ {cr.WINDOW:,} tokens" in ctx and "compactions" in ctx
