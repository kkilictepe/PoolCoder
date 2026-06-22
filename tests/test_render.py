"""The render layer turns snapshots into Rich panels without exploding."""

from __future__ import annotations

from datetime import datetime, timezone

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
