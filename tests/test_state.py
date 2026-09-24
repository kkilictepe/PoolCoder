"""SessionState mutators shared by the Claude and Codex folds."""

from __future__ import annotations

import copy
import dataclasses
from datetime import datetime, timezone

from pool_coder.config import AUTO_COMPACT_FRACTION, Config
from pool_coder.models import ToolUse, UsageTokens
from pool_coder.pricing import Pricing
from pool_coder.snapshot import build_session_snapshot
from pool_coder.state import (
    CONTEXT_HISTORY_MAX,
    EVENT_LOG_MAX,
    CompactionEvent,
    SessionState,
    SubagentStatus,
    ToolStatus,
    WorkflowStatus,
)

T0 = datetime(2026, 9, 24, 10, 0, tzinfo=timezone.utc)
T1 = datetime(2026, 9, 24, 10, 5, tzinfo=timezone.utc)


def test_new_fields_default_to_claude():
    s = SessionState()
    assert s.agent == "claude"
    assert s.context_window == 0
    assert s.auto_compact_fraction == AUTO_COMPACT_FRACTION


def test_record_usage_is_an_upsert():
    s = SessionState()
    s.record_usage("main", "r1", UsageTokens(input=10, output=5), "gpt-5.6-sol")
    s.record_usage("main", "r2", UsageTokens(input=3), "gpt-5.5")
    # replaying r1 with the same numbers must not double count
    s.record_usage("main", "r1", UsageTokens(input=10, output=5), "gpt-5.6-sol")
    assert s.cumulative_tokens().input == 13
    assert s.cumulative_tokens().output == 5
    assert s.keys_by_source["main"] == {"r1", "r2"}
    assert s.model_breakdown() == {
        "gpt-5.6-sol": UsageTokens(input=10, output=5),
        "gpt-5.5": UsageTokens(input=3),
    }
    # a None model is grouped as "unknown", as before
    s.record_usage("agent:a", "x", UsageTokens(input=1), None)
    assert s.model_breakdown()["unknown"].input == 1
    assert s.source_tokens("agent:a").input == 1


def test_drop_usage_removes_one_key():
    s = SessionState()
    s.record_usage("main", "tc:100", UsageTokens(input=100), "gpt")
    s.record_usage("main", "resp_1", UsageTokens(input=7), "gpt")
    s.drop_usage("main", "tc:100")
    assert s.cumulative_tokens().input == 7
    assert s.keys_by_source["main"] == {"resp_1"}
    assert ("main", "tc:100") not in s.key_model
    # dropping the last key forgets the source; unknown keys are a no-op
    s.drop_usage("main", "resp_1")
    s.drop_usage("main", "missing")
    s.drop_usage("agent:none", "missing")
    assert s.tokens_by_key == {}
    assert "main" not in s.keys_by_source


def test_drop_source_tokens_leaves_other_sources():
    s = SessionState()
    s.record_usage("main", "r1", UsageTokens(input=50), "m")
    s.record_usage("agent:a1", "ar1", UsageTokens(input=7), "m")
    s.record_usage("agent:a1", "ar2", UsageTokens(input=3), "m")
    s.drop_source_tokens("agent:a1")
    assert s.source_tokens("agent:a1") == UsageTokens()
    assert s.source_tokens("main").input == 50
    assert "agent:a1" not in s.keys_by_source
    assert set(s.key_model) == {("main", "r1")}
    s.drop_source_tokens("never-seen")  # no-op


def test_reset_main_clears_exactly_the_main_fields():
    s = SessionState(session_id="sid", model="claude-opus-4-8", cwd="C:/x", last_prompt="hi")
    s.record_usage("main", "r1", UsageTokens(input=50), "m")
    s.turns, s.user_messages, s.tool_errors = 3, 2, 1
    s.tool_counts["Read"] = 4
    s.tools["t1"] = ToolStatus(ToolUse("t1", "Read"))
    s.files_touched["a.py"] = T0
    s.update_context(UsageTokens(input=1000), T0)
    s.update_context(UsageTokens(input=100), T1)  # drop -> one compaction
    assert s.compactions and s.events
    s.reset_main()
    assert (s.turns, s.user_messages, s.tool_errors) == (0, 0, 0)
    assert s.tool_counts == {} and s.tools == {} and s.files_touched == {}
    assert (s.current_context_tokens, s.prev_context_tokens, s.max_context_tokens) == (0, 0, 0)
    assert s.context_history == [] and s.compactions == [] and len(s.events) == 0
    # not reset: identity/meta, the prompt, and tokens (drop_source_tokens does that)
    assert (s.session_id, s.model, s.cwd, s.last_prompt) == ("sid", "claude-opus-4-8", "C:/x", "hi")
    assert s.source_tokens("main").input == 50


# What Aggregator.reset_source("main") cleared before the refactor: nothing else.
MAIN_FIELDS = {"turns", "user_messages", "tool_counts", "tool_errors", "tools", "files_touched",
               "current_context_tokens", "prev_context_tokens", "max_context_tokens",
               "context_history", "compactions", "events"}


def _every_field_set() -> SessionState:
    s = SessionState(
        session_id="sid", project_hash="p", main_path="m.jsonl", cwd="C:/x", git_branch="main",
        version="2.1.280", model="claude-opus-4-8", mode="plan", title="t", last_prompt="hi",
        started_at=T0, last_record_at=T1, current_context_tokens=500, max_context_tokens=900,
        prev_context_tokens=700, latest_usage=UsageTokens(input=3), context_history=[1, 2],
        compactions=[CompactionEvent(T0, 900, 500)], turns=3, user_messages=2,
        tool_counts={"Read": 4}, tool_errors=1, tools={"t1": ToolStatus(ToolUse("t1", "Read"))},
        files_touched={"a.py": T0}, subagents={"a1": SubagentStatus("a1", "Explore")},
        workflows={"w1": WorkflowStatus("w1", "wf", started_keys={"k"})},
        agent="codex", context_window=258_400, auto_compact_fraction=0.5,
    )
    s.record_usage("main", "r1", UsageTokens(input=50), "m")
    s.push_event(T0, "tool", "→ Read a.py")
    return s


def _value(s: SessionState, name: str):
    value = getattr(s, name)
    return list(value) if name == "events" else value


def test_reset_main_changes_exactly_the_main_fields():
    s = _every_field_set()
    default = SessionState()
    names = [f.name for f in dataclasses.fields(SessionState)]
    unset = [n for n in names if _value(s, n) == _value(default, n)]
    assert unset == []  # every field holds a non-default value (new fields too)
    before = copy.deepcopy(s)
    s.reset_main()
    changed = {n for n in names if _value(s, n) != _value(before, n)}
    assert changed == MAIN_FIELDS
    assert all(_value(s, n) == _value(default, n) for n in MAIN_FIELDS)
    assert s.events.maxlen == EVENT_LOG_MAX  # cleared in place, still bounded


def test_update_context_detects_drop_by_default():
    s = SessionState()
    s.update_context(UsageTokens(input=800_000), T0)
    s.update_context(UsageTokens(input=120_000, cache_read=5), T1)
    assert s.current_context_tokens == 120_005
    assert s.prev_context_tokens == 800_000
    assert s.max_context_tokens == 800_000
    assert s.context_history == [800_000, 120_005]
    assert s.latest_usage == UsageTokens(input=120_000, cache_read=5)
    assert len(s.compactions) == 1
    ev = s.compactions[0]
    assert (ev.at, ev.before, ev.after) == (T1, 800_000, 120_005)
    assert [e.kind for e in s.events] == ["compaction"]


def test_update_context_without_drop_detection():
    s = SessionState()
    s.update_context(UsageTokens(input=800_000), T0, detect_drop=False)
    s.update_context(UsageTokens(input=120_000), T1, detect_drop=False)
    assert s.compactions == []
    assert len(s.events) == 0
    # everything else is tracked exactly as with detection on
    assert s.current_context_tokens == 120_000
    assert s.prev_context_tokens == 800_000
    assert s.max_context_tokens == 800_000
    assert s.context_history == [800_000, 120_000]


def test_update_context_history_is_capped():
    s = SessionState()
    for i in range(CONTEXT_HISTORY_MAX + 25):
        s.update_context(UsageTokens(input=i + 1), None)
    assert len(s.context_history) == CONTEXT_HISTORY_MAX == 600
    assert s.context_history[-1] == CONTEXT_HISTORY_MAX + 25


def test_snapshot_prefers_reported_window_and_state_fraction():
    s = SessionState(model="gpt-5.6-sol", agent="codex", context_window=258_400,
                     auto_compact_fraction=0.947)
    s.update_context(UsageTokens(input=100_000), T0, detect_drop=False)
    snap = build_session_snapshot(s, Config(), Pricing.load(), now=T1)
    assert snap.agent == "codex"
    assert snap.effective_window == 258_400  # reported, not the 272K default
    assert snap.auto_compact_headroom == int(258_400 * 0.947) - 100_000 == 144_704
    # without a reported window: the configured gpt window, never bumped (even
    # with a Claude-mode config: the session's agent decides)
    s2 = SessionState(model="gpt-5.6-sol", agent="codex")
    s2.update_context(UsageTokens(input=300_000), T0)
    snap2 = build_session_snapshot(s2, Config(), Pricing.load(), now=T1)
    assert snap2.effective_window == 272_000
    # the same id in a Claude Code session keeps the default window and its bump
    s3 = SessionState(model="gpt-5.6-sol")
    s3.update_context(UsageTokens(input=300_000), T0)
    snap3 = build_session_snapshot(s3, Config(), Pricing.load(), now=T1)
    assert (snap3.effective_window, snap3.agent) == (1_000_000, "claude")


def test_codex_snapshot_without_model_or_reported_window_uses_gpt_window():
    # a Codex thread with no model and no reported window yet: the gpt window
    # (as the picker preview uses), never the 200K default or its 1M auto-bump
    s = SessionState(agent="codex")
    s.update_context(UsageTokens(input=210_000), T0, detect_drop=False)
    snap = build_session_snapshot(s, Config(gpt_window=300_000), Pricing.load(), now=T1)
    assert snap.effective_window == 300_000
    assert snap.model is None
    # Claude without a model keeps today's default window and its auto-bump
    c = SessionState()
    c.update_context(UsageTokens(input=210_000), T0)
    assert build_session_snapshot(c, Config(), Pricing.load(), now=T1).effective_window == 1_000_000
    c2 = SessionState()
    c2.update_context(UsageTokens(input=10_000), T0)
    assert build_session_snapshot(c2, Config(), Pricing.load(), now=T1).effective_window == 200_000


def test_source_billable_totals_match_source_tokens():
    s = SessionState()
    s.record_usage("main", "r1", UsageTokens(input=10, cache_read=5, output=2, reasoning=1), "m")
    s.record_usage("agent:a", "x", UsageTokens(input=3, cache_creation=4, output=1), "m")
    s.record_usage("agent:a", "y", UsageTokens(cache_read=7, output=6), None)
    s.record_usage("wfagent:w", "z", UsageTokens(output=9), "m")
    totals = s.source_billable_totals()
    assert totals == {src: s.source_tokens(src).billable_total for src in s.keys_by_source}
    assert totals == {"main": 17, "agent:a": 21, "wfagent:w": 9}
    s.drop_source_tokens("agent:a")
    assert "agent:a" not in s.source_billable_totals()
