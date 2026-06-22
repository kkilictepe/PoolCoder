"""Web renderer: snapshots -> HTML fragments/pages, with escaping."""

from __future__ import annotations

from datetime import datetime, timezone

from pool_coder import web
from pool_coder.aggregator import Aggregator
from pool_coder.config import Config
from pool_coder.parser import Record
from pool_coder.pricing import Pricing
from pool_coder.snapshot import PlanLimitsView, Snapshot, build_session_snapshot
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
