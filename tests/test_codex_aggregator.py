"""Codex fold: tokens, context, session details, tools, sub-agents, reset.

Every record is built with ``codex_records`` and goes through
``parse_codex_line(line(r))``, so the real parser is exercised too.
"""

from __future__ import annotations

import dataclasses
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

import codex_records as cr
from codex_records import CHILD, GRANDCHILD, GUARDIAN, ROOT, WINDOW, line, rec, ts
from pool_coder.codex.aggregator import CodexAggregator
from pool_coder.codex.parser import parse_codex_line, usage_from
from pool_coder.config import CODEX_AUTO_COMPACT_FRACTION, Config
from pool_coder.discovery import DiscoveryDelta, SubagentReg, TailerSpec
from pool_coder.models import UsageTokens
from pool_coder.pricing import Pricing
from pool_coder.snapshot import build_session_snapshot
from pool_coder.sources.base import FoldTarget
from pool_coder.sources.jsonl_source import JsonlSource
from pool_coder.state import CompactionEvent, SessionState

NOW = datetime(2026, 9, 22, 23, 0, 0, tzinfo=timezone.utc)
AGENT = f"agent:{CHILD}"
# every event text starts with one of these (SPEC section 4)
PREFIXES = ("→ ", "✓ ", "✗ ", "» ", "↳ ", "✎ ", "⟳ ", "⊕ ", "⊙ ", "⊘ ")


# -- local builders (not in codex_records) -------------------------------------
def context_compacted_event(at: str = cr.TS) -> dict:
    return rec("event_msg", {"type": "context_compacted"}, at)


def context_compaction_item(at: str = cr.TS) -> dict:
    return rec("event_msg", {"type": "item_completed", "thread_id": ROOT, "turn_id": "turn-1",
                             "item": {"type": "ContextCompaction", "id": "item-6"}}, at)


def exec_multi_js() -> str:
    """Two shell commands and a two-file patch in one code-mode script."""
    patch = ("*** Begin Patch\n*** Update File: C:\\Git\\proj\\a.py\n@@\n-x\n+y\n"
             "*** Add File: C:\\Git\\proj\\b.py\n+new\n*** End Patch\n")
    return (f"const a = await tools.exec_command({json.dumps({'cmd': 'git status'})});\n"
            f"const b = await tools.exec_command({json.dumps({'cmd': 'git diff'})});\n"
            f"const patch = {json.dumps(patch)};\n"
            "await tools.apply_patch(patch);\ntext(a.output);\n")


# -- folding helpers ---------------------------------------------------------------
def make(session_id: str = ROOT) -> CodexAggregator:
    return CodexAggregator(SessionState(session_id=session_id, main_path="x.jsonl"))


def feed(agg: CodexAggregator, records: list[dict], source: str = "main") -> CodexAggregator:
    for r in records:
        parsed = parse_codex_line(line(r))
        assert parsed is not None
        agg.apply(source, parsed)
    return agg


def texts(agg: CodexAggregator) -> list[str]:
    return [e.text for e in agg.state.events]


def snap(agg: CodexAggregator):
    return build_session_snapshot(agg.state, Config(), Pricing.load(), now=NOW)


def u(inp=0, cached=0, cw=0, out=0, rs=0) -> UsageTokens:
    return usage_from(cr.usage(inp, cached, cw, out, rs))


def summary(agg: CodexAggregator) -> dict:
    """Everything a replay must leave unchanged."""
    st = agg.state
    return {
        "cumulative": st.cumulative_tokens(),
        "main": st.source_tokens("main"),
        "child": st.source_tokens(AGENT),
        "by_model": st.model_breakdown(),
        "turns": st.turns,
        "user_messages": st.user_messages,
        "tool_counts": dict(st.tool_counts),
        "tool_errors": st.tool_errors,
        "files": dict(st.files_touched),
        "compactions": list(st.compactions),
        "context": (st.current_context_tokens, st.max_context_tokens, list(st.context_history)),
        "subs": {k: (s.turns, s.finished, s.agent_type) for k, s in st.subagents.items()},
        "last_prompt": st.last_prompt,
        "tools_done": {k: (t.done, t.is_error) for k, t in st.tools.items()},
    }


def main_session() -> list[dict]:
    """A realistic main thread: settings, prompts, tools, tokens, compaction, a sub-agent."""
    return [
        cr.session_meta(ROOT, at=ts(0)),
        cr.task_started(at=ts(0, 1)),
        cr.turn_context("gpt-5.6-terra", at=ts(0, 1)),
        cr.user_message_item("# Context from my IDE setup:\n\n## My request:\ninstall all packages\n",
                             client_id="c1", at=ts(0, 2)),
        cr.reasoning("Planning the install", at=ts(0, 3)),
        cr.assistant_message("I'll inspect the manifests first.", at=ts(0, 4)),
        cr.agent_message_item("I'll inspect the manifests first.", at=ts(0, 4)),
        cr.exec_call("call_1", cr.exec_js("npm ci"), at=ts(0, 5)),
        cr.token_usage("resp_1", inp=20_000, cached=15_000, out=300, rs=100, at=ts(0, 6)),
        cr.token_count(cr.usage(20_000, 15_000, 0, 300, 100), at=ts(0, 6)),
        cr.exec_output("call_1", at=ts(0, 30)),
        cr.command_execution("exec-1", ["pwsh.exe", "-Command", "npm ci"], at=ts(0, 30)),
        cr.function_call("spawn_agent", {"task_name": "dependency_audit", "fork_turns": 1,
                                         "message": "gAAAA-secret"}, "call_2",
                         namespace="collaboration", at=ts(1)),
        cr.sub_agent_item("call_2", "started", at=ts(1, 1)),
        cr.function_output("call_2", '{"task_name":"/root/dependency_audit"}', at=ts(1, 1)),
        cr.token_usage("resp_2", inp=240_000, cached=200_000, out=500, at=ts(1, 2)),
        cr.compacted(at=ts(2)),
        cr.token_usage("resp_3", inp=30_000, cached=1_000, out=200, at=ts(2, 5)),
        cr.exec_call("call_3", patch_js_two(), at=ts(3)),
        cr.exec_output("call_3", header="Script failed", at=ts(3, 1)),
        cr.file_change("exec-2", ["C:\\Git\\proj\\a.py"], at=ts(3, 1)),
        cr.sub_agent_item("call_2", "completed", at=ts(4)),
        cr.task_complete(at=ts(4, 1)),
    ]


def patch_js_two() -> str:
    return cr.patch_js("C:\\Git\\proj\\a.py")


def child_session() -> list[dict]:
    return [
        cr.subagent_meta(CHILD, forked=False, at=ts(1, 1)),
        cr.task_started(at=ts(1, 1)),
        cr.turn_context("gpt-5.6-sol", at=ts(1, 1)),
        cr.exec_call("c_call_1", cr.exec_js("rg --files"), at=ts(1, 2)),
        cr.token_usage("c_resp_1", thread_id=CHILD, inp=5_000, cached=4_000, out=100, at=ts(1, 3)),
        cr.exec_output("c_call_1", at=ts(1, 4)),
        cr.token_usage("c_resp_2", thread_id=CHILD, inp=6_000, cached=5_000, out=150, at=ts(1, 5)),
        cr.task_complete(at=ts(1, 6)),
    ]


# ---------------------------------------------------------------------------
# construction
# ---------------------------------------------------------------------------
def test_sets_agent_and_codex_compact_fraction():
    agg = make()
    assert agg.state.agent == "codex"
    assert agg.state.auto_compact_fraction == CODEX_AUTO_COMPACT_FRACTION
    assert isinstance(agg, FoldTarget)
    assert snap(agg).agent == "codex"


# ---------------------------------------------------------------------------
# tokens
# ---------------------------------------------------------------------------
def test_replaying_everything_twice_changes_nothing():
    agg = make()
    feed(agg, main_session())
    feed(agg, child_session(), AGENT)
    first = summary(agg)
    assert first["turns"] == 3 and first["subs"][CHILD][0] == 2
    feed(agg, main_session())
    feed(agg, child_session(), AGENT)
    assert summary(agg) == first


def test_replaying_fallback_totals_twice_changes_nothing():
    records = [
        cr.session_meta(ROOT),
        cr.token_count(cr.usage(1_000, 800, 0, 50, 10), at=ts(0, 1)),
        cr.token_count(cr.usage(1_000, 800, 0, 50, 10), at=ts(0, 2)),   # repeat
        cr.token_count(cr.usage(3_000, 2_500, 0, 90, 20), at=ts(0, 3)),
        cr.token_count(cr.usage(6_000, 5_000, 0, 150, 30), at=ts(0, 4)),
    ]
    agg = feed(make(), records)
    first = summary(agg)
    assert first["turns"] == 3
    assert first["cumulative"] == u(6_000, 5_000, 0, 150, 30)
    feed(agg, records)
    assert summary(agg) == first


def test_duplicate_token_usage_record_ignored():
    agg = feed(make(), [
        cr.token_usage("resp_1", inp=1_000, cached=600, out=40, rs=5),
        cr.token_usage("resp_1", inp=1_000, cached=600, out=40, rs=5),
    ])
    st = agg.state
    assert st.cumulative_tokens() == u(1_000, 600, 0, 40, 5)
    assert st.turns == 1
    assert st.context_history == [1_000]


def test_usage_mapping_counts_cache_inside_input():
    agg = feed(make(), [cr.token_usage("r", inp=10_000, cached=7_000, cw=1_000, out=300, rs=120)])
    cum = agg.state.cumulative_tokens()
    assert (cum.input, cum.cache_read, cum.cache_creation, cum.output, cum.reasoning) == \
        (2_000, 7_000, 1_000, 300, 120)
    assert agg.state.current_context_tokens == 10_000


def test_repeated_identical_token_count_totals_ignored():
    total = cr.usage(2_000, 1_500, 0, 80, 20)
    agg = feed(make(), [cr.token_count(total), cr.token_count(total), cr.token_count(total)])
    assert agg.state.cumulative_tokens() == u(2_000, 1_500, 0, 80, 20)
    assert agg.state.turns == 1
    assert agg.state.context_history == [2_000]  # last_token_usage drives context in fallback


def test_token_count_going_backwards_rebaselines():
    agg = feed(make(), [
        cr.token_count(cr.usage(1_000, 0, 0, 100)),
        cr.token_count(cr.usage(500, 0, 0, 50)),      # counter reset: baseline only
        cr.token_count(cr.usage(800, 0, 0, 70)),      # +300 in, +20 out
    ])
    assert agg.state.cumulative_tokens() == u(1_300, 0, 0, 120)


def test_fallback_then_primary_switch_drops_fallback_keys():
    agg = feed(make(), [
        cr.token_count(cr.usage(1_000, 500, 0, 40), at=ts(0, 1)),
        cr.token_count(cr.usage(3_000, 2_000, 0, 90), at=ts(0, 2)),
    ])
    st = agg.state
    assert st.turns == 2
    assert st.cumulative_tokens() == u(3_000, 2_000, 0, 90)
    feed(agg, [
        cr.token_usage("resp_9", inp=4_000, cached=3_500, out=60, at=ts(0, 3)),
        cr.token_count(cr.usage(7_000, 5_500, 0, 150), at=ts(0, 3)),   # ignored from now on
    ])
    assert st.keys_by_source["main"] == {"resp_9"}
    assert st.cumulative_tokens() == u(4_000, 3_500, 0, 60)
    assert st.turns == 1
    assert st.current_context_tokens == 4_000


def test_token_usage_record_of_another_thread_ignored():
    agg = feed(make(), [cr.token_usage("resp_x", thread_id=CHILD, inp=9_999, out=9)])
    assert agg.state.cumulative_tokens() == UsageTokens()
    assert agg.state.turns == 0


def test_per_model_breakdown_when_model_changes():
    agg = feed(make(), [
        cr.session_meta(ROOT),
        cr.turn_context("gpt-5.6-terra"),
        cr.token_usage("r1", inp=1_000, cached=500, out=10),
        cr.token_usage("r2", inp=2_000, cached=1_500, out=20),
        cr.turn_context("gpt-5.6-sol"),
        cr.token_usage("r3", inp=3_000, cached=2_500, out=30),
    ])
    by_model = agg.state.model_breakdown()
    assert set(by_model) == {"gpt-5.6-terra", "gpt-5.6-sol"}
    assert by_model["gpt-5.6-terra"] == u(3_000, 2_000, 0, 30)
    assert by_model["gpt-5.6-sol"] == u(3_000, 2_500, 0, 30)
    assert agg.state.model == "gpt-5.6-sol"
    costs = dict(snap(agg).cost.by_model)
    assert set(costs) == {"gpt-5.6-terra", "gpt-5.6-sol"} and all(c > 0 for c in costs.values())


def test_model_defaults_to_meta_model_then_gpt():
    agg = feed(make(), [cr.token_usage("r0", inp=10)])
    assert set(agg.state.model_breakdown()) == {"gpt"}
    agg = feed(make(), [cr.session_meta(ROOT, model="gpt-5.5"), cr.token_usage("r0", inp=10)])
    assert agg.state.model == "gpt-5.5"
    assert set(agg.state.model_breakdown()) == {"gpt-5.5"}


def test_forked_child_ignores_parent_copies_and_baselines_its_totals():
    agg = feed(make(), [
        cr.subagent_meta(CHILD, forked=True, cwd="C:\\child", branch="child-branch",
                         cli_version="9.9.9", at=ts(0)),
        cr.session_meta(ROOT, cwd="C:\\parent", at=ts(0)),                  # parent's copied meta
        cr.token_usage("parent_resp", thread_id=ROOT, inp=50_000, out=10),  # parent's copied usage
        # the parent's token_count history, copied in one burst: the first total
        # is a baseline, and so is every later one (they are the parent's usage)
        cr.token_count(cr.usage(60_000, 50_000, 0, 3_000, 500), at=ts(0)),
        cr.token_count(cr.usage(100_000, 90_000, 0, 5_000, 1_000), at=ts(0)),
    ], AGENT)
    st = agg.state
    assert st.source_tokens(AGENT) == UsageTokens()
    assert st.subagents[CHILD].turns == 0
    assert st.source_tokens("main") == UsageTokens()
    # the child's meta (or the parent's copy) never touches the main thread's details
    assert (st.cwd, st.git_branch, st.version) == (None, None, None)
    sub = st.subagents[CHILD]
    assert sub.agent_type == "dependency_audit"
    assert sub.description == "Pauli · /root/dependency_audit"
    # only the child's own per-response records count
    feed(agg, [
        cr.token_usage("child_resp", thread_id=CHILD, inp=4_100, cached=3_000, out=210,
                       at=ts(0, 31)),
        cr.token_count(cr.usage(104_100, 93_000, 0, 5_210, 1_000), at=ts(0, 31)),
    ], AGENT)
    assert st.keys_by_source[AGENT] == {"child_resp"}
    assert st.source_tokens(AGENT) == u(4_100, 3_000, 0, 210)
    assert sub.turns == 1


def test_forked_child_totals_never_count_even_without_primary_records():
    agg = feed(make(), [
        cr.subagent_meta(CHILD, forked=True),
        cr.token_count(cr.usage(100_000, 90_000, 0, 5_000), at=ts(0)),
        cr.token_count(cr.usage(104_000, 93_000, 0, 5_200), at=ts(0, 30)),
    ], AGENT)
    assert agg.state.source_tokens(AGENT) == UsageTokens()


def test_forked_main_thread_uses_only_its_own_usage_records():
    agg = feed(make(), [
        cr.session_meta(ROOT, forked_from=CHILD),
        cr.token_count(cr.usage(100_000, 90_000, 0, 5_000), window=WINDOW),
        cr.token_usage("r1", inp=2_000, cached=1_000, out=20),
    ])
    assert agg.state.cumulative_tokens() == u(2_000, 1_000, 0, 20)
    assert agg.state.context_window == WINDOW


def test_non_forked_child_counts_its_first_total():
    agg = feed(make(), [
        cr.subagent_meta(CHILD, forked=False),
        cr.token_count(cr.usage(2_000, 1_000, 0, 30)),
    ], AGENT)
    assert agg.state.source_tokens(AGENT) == u(2_000, 1_000, 0, 30)


def test_child_model_from_its_own_settings():
    agg = make()
    feed(agg, [cr.turn_context("gpt-5.6-terra"), cr.token_usage("r1", inp=10)])
    feed(agg, [
        cr.subagent_meta(CHILD, forked=False, model="gpt-5.5"),
        cr.token_usage("c1", thread_id=CHILD, inp=10),
        cr.thread_settings("gpt-5.6-luna", tid=CHILD),
        cr.token_usage("c2", thread_id=CHILD, inp=10),
        cr.thread_settings("gpt-6-astra", tid=ROOT),   # parent's copied settings: ignored
        cr.token_usage("c3", thread_id=CHILD, inp=10),
    ], AGENT)
    st = agg.state
    assert st.key_model[(AGENT, "c1")] == "gpt-5.5"
    assert st.key_model[(AGENT, "c2")] == "gpt-5.6-luna"
    assert st.key_model[(AGENT, "c3")] == "gpt-5.6-luna"
    assert st.model == "gpt-5.6-terra"  # a child never sets the main model


# ---------------------------------------------------------------------------
# context
# ---------------------------------------------------------------------------
def test_reported_window_wins_without_auto_bump():
    assert int(WINDOW * CODEX_AUTO_COMPACT_FRACTION) == 244_704
    agg = feed(make(), [
        cr.token_count(cr.usage(10, 0, 0, 1), window=WINDOW),
        cr.token_usage("r1", inp=230_000, cached=220_000, out=100),   # > 200K: no 1M bump
    ])
    s = snap(agg)
    assert agg.state.context_window == WINDOW
    assert s.effective_window == 258_400
    assert s.current_context == 230_000
    assert s.auto_compact_headroom == 244_704 - 230_000
    assert s.tokens_to_limit == 258_400 - 230_000


def test_window_from_task_started():
    agg = feed(make(), [cr.task_started(window=200_000)])
    assert snap(agg).effective_window == 200_000


# Real rollouts stamp the other two compaction markers a few ms apart from
# `compacted` (e.g. .382 vs .409), so the timestamp dedupe cannot hide them.
@pytest.mark.parametrize("marker_at", ["2026-09-22T20:48:52.027Z",   # just after
                                       "2026-09-22T20:48:51.973Z"])  # just before
def test_one_compacted_record_gives_one_compaction_event(marker_at):
    agg = feed(make(), [
        cr.token_usage("r1", inp=240_000, cached=230_000, out=100, at=ts(0)),
        cr.compacted(at=ts(1)),
        context_compacted_event(at=marker_at),
        context_compaction_item(at=marker_at),
        cr.token_usage("r_zero", inp=0, out=5, at=ts(1, 5)),      # no prompt: not a context sample
        cr.token_usage("r2", inp=31_000, cached=20_000, out=100, at=ts(2)),
    ])
    st = agg.state
    assert st.compactions == [CompactionEvent(at=datetime(2026, 9, 22, 20, 48, 52, tzinfo=timezone.utc),
                                              before=240_000, after=31_000)]
    assert [t for t in texts(agg) if t.startswith("⟳")] == ["⟳ context compacted (was 240,000)"]
    assert st.current_context_tokens == 31_000
    assert snap(agg).compaction_count == 1


def compaction_estimate(total: int, at: str) -> dict:
    """The token_count Codex writes right after ``compacted``: an empty last
    usage whose ``total_tokens`` estimates the compacted context."""
    return cr.token_count(cr.usage(900_000, 800_000, 0, 9_000),
                          {**cr.usage(), "total_tokens": total}, at=at)


def manual_compact() -> list[dict]:
    """``/compact`` at a turn boundary: the summary request (its input is the
    old context), the marker, Codex's estimate, then the thread goes idle."""
    return [
        cr.task_started(at=ts(0)),
        cr.token_usage("r1", inp=204_238, cached=200_000, out=3_000, at=ts(0, 5)),
        cr.compacted(at=ts(1)),
        compaction_estimate(6_820, ts(1)),
        context_compacted_event(at=ts(1)),
        cr.task_complete(at=ts(1, 1)),
    ]


def test_manual_compaction_resets_the_gauge_until_the_next_response():
    agg = feed(make(), manual_compact())
    st = agg.state
    assert st.compactions == [CompactionEvent(at=datetime(2026, 9, 22, 20, 48, 52, tzinfo=timezone.utc),
                                              before=204_238, after=0)]
    assert st.current_context_tokens == 6_820  # Codex's estimate, not the old 204,238
    s = snap(agg)
    assert s.occupancy == pytest.approx(6_820 / WINDOW)
    assert s.auto_compact_headroom == int(WINDOW * CODEX_AUTO_COMPACT_FRACTION) - 6_820
    assert s.max_context == 204_238  # the peak is kept
    # the next response reports the real size and completes the event
    feed(agg, [cr.task_started(at=ts(5)), cr.token_usage("r2", inp=31_000, out=10, at=ts(5, 1)),
               compaction_estimate(1, ts(5, 2))])  # no compaction pending: ignored
    assert st.current_context_tokens == 31_000 and st.compactions[-1].after == 31_000
    # a re-read of the old marker is deduped and never zeroes the gauge again
    feed(agg, [cr.compacted(at=ts(1)), compaction_estimate(6_820, ts(1))])
    assert st.current_context_tokens == 31_000 and len(st.compactions) == 1


def test_compaction_without_an_estimate_empties_the_context():
    agg = feed(make(), [cr.token_usage("r1", inp=204_238, at=ts(0)),
                        compaction_estimate(6_820, ts(0, 1)),  # no marker yet: ignored
                        cr.compacted(at=ts(1))])
    assert agg.state.current_context_tokens == 0
    assert agg.state.compactions[-1].before == 204_238
    assert snap(agg).occupancy == 0.0


def test_reset_and_replay_after_a_manual_compaction():
    agg = feed(make(), manual_compact())
    before = dataclasses.asdict(snap(agg))
    agg.reset_source("main")
    feed(agg, manual_compact())
    assert dataclasses.asdict(snap(agg)) == before


def test_context_drop_without_marker_is_not_a_compaction():
    agg = feed(make(), [
        cr.token_usage("r1", inp=200_000, out=1, at=ts(0)),
        cr.token_usage("r2", inp=20_000, out=1, at=ts(1)),
    ])
    assert agg.state.compactions == []


# ---------------------------------------------------------------------------
# session details
# ---------------------------------------------------------------------------
def test_prompts_deduped_by_client_id_across_formats():
    # the two copies of one prompt need not share a timestamp: client_id decides
    agg = feed(make(), [
        cr.user_message_item("fix the bug", client_id="c1", at=ts(0)),
        cr.user_message_event("fix the bug", client_id="c1", at=ts(0, 1)),
        cr.user_message_event("and add a test", client_id="c2", at=ts(1)),
        cr.user_message_item("and add a test", client_id="c2", at=ts(1, 1)),
    ])
    assert agg.state.user_messages == 2
    assert agg.state.last_prompt == "and add a test"
    assert [t for t in texts(agg) if t.startswith("»")] == ["» fix the bug", "» and add a test"]


def test_prompts_without_client_id_are_keyed_by_item_id():
    """Guardian review threads send UserMessage items without a client_id."""
    agg = feed(make(), [
        cr.user_message_item("review this", client_id=None, item_id="item-1", at=ts(0)),
        cr.user_message_item("review this", client_id=None, item_id="item-3", at=ts(1)),
        cr.user_message_item("review this", client_id=None, item_id="item-1", at=ts(2)),  # replayed
    ])
    assert agg.state.user_messages == 2
    assert [t for t in texts(agg) if t.startswith("»")] == ["» review this", "» review this"]


def test_guardian_thread_opened_directly_counts_every_review_prompt():
    agg = feed(make(GUARDIAN), [
        cr.guardian_meta(GUARDIAN, at=ts(0)),
        cr.user_message_item("first review", client_id=None, tid=GUARDIAN, item_id="g-1", at=ts(0)),
        cr.user_message_item("second review", client_id=None, tid=GUARDIAN, item_id="g-2", at=ts(1)),
    ])
    assert agg.state.user_messages == 2 and agg.state.last_prompt == "second review"


def test_ide_preamble_cleaned():
    text = ("# Context from my IDE setup:\n\n## Active file: a.md\n\n## Open tabs:\n- a.md: a.md\n\n"
            "## My request:\ninstall all packages\n")
    agg = feed(make(), [cr.user_message_item(text)])
    assert agg.state.last_prompt == "install all packages"
    assert texts(agg) == ["» install all packages"]


def test_prompt_without_text_counts_but_keeps_last_prompt():
    agg = feed(make(), [
        cr.user_message_event("first", client_id="c1"),
        cr.user_message_event("# Context from my IDE setup:\n\n## My request:\n", client_id="c2"),
    ])
    assert agg.state.user_messages == 2
    assert agg.state.last_prompt == "first"
    assert texts(agg) == ["» first"]


def test_mode_from_turn_context_and_thread_settings():
    agg = feed(make(), [cr.turn_context(mode="plan", effort="ultra", sandbox="danger-full-access")])
    assert agg.state.mode == "plan · ultra · full-access"
    feed(agg, [cr.thread_settings("gpt-5.6-sol", effort="ultra", profile=":workspace")])
    assert agg.state.mode == "ultra · workspace"
    assert agg.state.model == "gpt-5.6-sol"
    feed(agg, [cr.turn_context(mode="default", effort="high", sandbox="read-only",
                               cwd="C:\\Git\\other")])
    assert agg.state.mode == "high · read-only"
    assert agg.state.cwd == "C:\\Git\\other"


def test_mode_from_disabled_permission_profile():
    r = rec("event_msg", {"type": "thread_settings_applied", "thread_id": ROOT,
                          "thread_settings": {"model": "gpt-5.6-terra", "reasoning_effort": "low",
                                              "permission_profile": {"type": "disabled"},
                                              "collaboration_mode": {"mode": "plan"}}})
    agg = feed(make(), [r])
    assert agg.state.mode == "plan · low · full-access"


def test_cwd_branch_version_only_from_own_session_meta():
    agg = feed(make(), [
        cr.session_meta(ROOT, cwd="C:\\Git\\proj", branch="main", cli_version="0.155.0"),
        cr.session_meta("01a0ffff-0000-7000-8000-000000000000", cwd="C:\\elsewhere",
                        branch="other", cli_version="0.1.0", model="gpt-6-astra"),
    ])
    st = agg.state
    assert (st.cwd, st.git_branch, st.version, st.model) == \
        ("C:\\Git\\proj", "main", "0.155.0", "gpt-5.6-terra")
    feed(agg, [cr.subagent_meta(CHILD, cwd="C:\\child", branch="child", cli_version="1.0")], AGENT)
    assert (st.cwd, st.git_branch, st.version) == ("C:\\Git\\proj", "main", "0.155.0")


def test_main_thread_id_learned_from_first_meta_when_unknown():
    agg = CodexAggregator(SessionState())
    feed(agg, [cr.session_meta(ROOT), cr.token_usage("r1", inp=100, out=1)])
    assert agg.state.session_id == ROOT
    assert agg.state.turns == 1


def test_assistant_text_and_reasoning_summary_events():
    agg = feed(make(), [
        cr.assistant_message("Working  on\nit."),
        cr.agent_message_item("Working on it."),
        cr.agent_message_event("Working on it."),
        cr.reasoning("Checking the manifests"),
        cr.reasoning(None),  # encrypted only
    ])
    assert texts(agg) == ["↳ Working on it.", "✎ Checking the manifests"]


def test_user_role_response_message_is_not_a_prompt():
    r = rec("response_item", {"type": "message", "role": "user",
                              "content": [{"type": "input_text", "text": "<environment_context/>"}]})
    agg = feed(make(), [r])
    assert agg.state.user_messages == 0 and texts(agg) == []


# ---------------------------------------------------------------------------
# tools
# ---------------------------------------------------------------------------
def test_in_flight_exec_shows_its_command():
    agg = feed(make(), [cr.exec_call("call_1", cr.exec_js("git status --short"), at=ts(0))])
    s = snap(agg)
    assert s.current_activity is not None
    assert s.current_activity.name == "shell"
    assert s.current_activity.target == "git status --short"
    assert [v.name for v in s.in_flight] == ["shell"]
    assert agg.state.tool_counts == {"shell": 1}
    assert texts(agg) == ["→ shell git status --short"]


def test_exec_inner_calls_counted_separately_and_all_patch_files_touched():
    agg = feed(make(), [cr.exec_call("call_1", exec_multi_js())])
    st = agg.state
    assert st.tool_counts == {"shell": 2, "apply_patch": 1}
    tool = st.tools["call_1"]
    assert tool.tool.name == "shell"
    assert tool.tool.target == "git status (+2)"
    assert set(st.files_touched) == {"C:\\Git\\proj\\a.py", "C:\\Git\\proj\\b.py"}
    assert texts(agg) == ["→ shell git status (+2)"]


def test_exec_patch_label_is_its_file():
    agg = feed(make(), [cr.exec_call("call_1", cr.patch_js("C:\\Git\\proj\\x.py", "Add"))])
    tool = agg.state.tools["call_1"].tool
    assert (tool.name, tool.target) == ("apply_patch", "C:\\Git\\proj\\x.py")
    assert list(agg.state.files_touched) == ["C:\\Git\\proj\\x.py"]


def test_exec_without_inner_calls_counts_exec():
    agg = feed(make(), [cr.exec_call("call_1", "\n  text(1 + 1);\n")])
    tool = agg.state.tools["call_1"].tool
    assert (tool.name, tool.target) == ("exec", "text(1 + 1);")
    assert agg.state.tool_counts == {"exec": 1}


def test_script_failed_is_an_error_counted_once():
    agg = feed(make(), [
        cr.exec_call("call_1", cr.exec_js("npm test")),
        cr.exec_output("call_1", header="Script failed", body="Error: boom"),
        cr.exec_output("call_1", header="Script failed", body="Error: boom"),   # duplicate
    ])
    st = agg.state
    tool = st.tools["call_1"]
    assert tool.done and tool.is_error is True
    assert st.tool_errors == 1
    assert "Script failed" in tool.result_preview
    assert texts(agg) == ["→ shell npm test", "✗ shell"]
    assert snap(agg).in_flight == ()


def test_completed_and_background_scripts_are_not_errors():
    agg = feed(make(), [
        cr.exec_call("call_1", cr.exec_js("ls")),
        cr.exec_output("call_1", body="Exit code: 1 in the command's own output"),
        cr.exec_call("call_2", cr.exec_js("npm run dev")),
        cr.exec_output("call_2", header="Script running with cell ID 3"),
    ])
    st = agg.state
    assert st.tool_errors == 0
    assert st.tools["call_1"].is_error is False and st.tools["call_2"].is_error is False
    assert [t for t in texts(agg) if t.startswith("✓")] == ["✓ shell", "✓ shell"]


def test_output_for_unknown_call_is_ignored():
    agg = feed(make(), [cr.exec_output("nope", header="Script failed")])
    assert agg.state.tool_errors == 0 and texts(agg) == []


def test_command_execution_failure_and_read_paths():
    cmd = ["C:\\Program Files\\PowerShell\\7\\pwsh.exe", "-Command", "Get-Content a.py; git status"]
    parsed = [{"type": "read", "cmd": "Get-Content a.py", "name": "a.py", "path": "src\\a.py"},
              {"type": "read", "cmd": "cat b.py", "name": "b.py"},
              {"type": "unknown", "cmd": "git status"}]
    agg = feed(make(), [
        cr.command_execution("exec-1", cmd, exit_code=1, parsed_cmd=parsed),
        cr.command_execution("exec-1", cmd, exit_code=1, parsed_cmd=parsed),   # duplicate
        cr.command_execution("exec-2", ["bash", "-lc", "true"], exit_code=0),
        cr.command_execution("exec-3", ["bash", "-lc", "make"], exit_code=0, status="failed"),
    ])
    st = agg.state
    assert st.tool_errors == 2
    assert texts(agg) == ["✗ shell Get-Content a.py; git status", "✗ shell make"]
    # a read path is relative to the command's cwd; a bare name stays as written
    assert set(st.files_touched) == {"C:\\Git\\proj\\src\\a.py", "b.py"}
    assert st.tool_counts == {}  # the exec call already counted the command


def test_turn_aborted_closes_every_in_flight_tool():
    agg = feed(make(), [
        cr.exec_call("call_1", cr.exec_js("sleep 100"), at=ts(0)),
        cr.function_call("wait_agent", {"timeout_ms": 1000}, "call_2", at=ts(0, 1)),
        cr.turn_aborted("interrupted", at=ts(1)),
    ])
    st = agg.state
    assert all(t.done and t.is_error is None for t in st.tools.values())
    assert st.tools["call_1"].ended_at == datetime(2026, 9, 22, 20, 48, 52, tzinfo=timezone.utc)
    assert texts(agg)[-1] == "⊘ turn aborted (interrupted)"
    assert snap(agg).in_flight == () and snap(agg).current_activity is None
    assert st.tool_errors == 0
    # a late output for a closed tool changes nothing
    feed(agg, [cr.exec_output("call_1", header="Script failed")])
    assert st.tool_errors == 0


def test_task_complete_closes_open_tools():
    agg = feed(make(), [
        cr.exec_call("call_1", cr.exec_js("npm run dev"), at=ts(0)),
        cr.task_complete(at=ts(1)),
    ])
    tool = agg.state.tools["call_1"]
    assert tool.done and tool.is_error is None
    assert not any(t.startswith("⊘") for t in texts(agg))


def test_legacy_function_calls():
    patch = ("*** Begin Patch\n*** Update File: src/app.py\n@@\n-a\n+b\n"
             "*** Delete File: old.py\n*** End Patch\n")
    plan = {"plan": [{"step": "Read code", "status": "completed"},
                     {"step": "Write the fix", "status": "in_progress"}]}
    agg = feed(make(), [
        cr.function_call("shell", {"command": ["bash", "-lc", "ls -la"]}, "c1"),
        cr.function_output("c1", json.dumps({"output": "x", "metadata": {"exit_code": 2}})),
        cr.function_call("update_plan", plan, "c2"),
        cr.function_output("c2", "Plan updated"),
        cr.function_call("apply_patch", {"input": patch}, "c3"),
        cr.local_shell_call("c4", ["bash", "-lc", "pwd"]),
        rec("response_item", {"type": "custom_tool_call", "call_id": "c5", "name": "apply_patch",
                              "input": "*** Begin Patch\n*** Add File: new.txt\n+x\n*** End Patch\n"}),
    ])
    st = agg.state
    assert (st.tools["c1"].tool.name, st.tools["c1"].tool.target) == ("shell", "ls -la")
    assert st.tools["c1"].is_error is True and st.tool_errors == 1
    assert (st.tools["c2"].tool.name, st.tools["c2"].tool.target) == ("update_plan", "Write the fix")
    assert (st.tools["c3"].tool.name, st.tools["c3"].tool.target) == ("apply_patch", "src/app.py")
    assert (st.tools["c4"].tool.name, st.tools["c4"].tool.target) == ("shell", "pwd")
    assert st.tool_counts == {"shell": 2, "update_plan": 1, "apply_patch": 2}
    assert set(st.files_touched) == {"src/app.py", "old.py", "new.txt"}
    assert "→ update_plan Write the fix" in texts(agg)


def test_spawn_agent_label_is_task_name_and_message_hidden():
    agg = feed(make(), [cr.function_call("spawn_agent", {"task_name": "dependency_audit",
                                                         "fork_turns": 2,
                                                         "message": "gAAAA-encrypted"},
                                         "call_1", namespace="collaboration")])
    tool = agg.state.tools["call_1"].tool
    assert (tool.name, tool.target) == ("spawn_agent", "dependency_audit")
    assert texts(agg) == ["→ spawn_agent dependency_audit"]
    assert not any("gAAAA" in t for t in texts(agg))


def test_collab_failure_output_is_an_error():
    agg = feed(make(), [
        cr.function_call("spawn_agent", {"task_name": "x"}, "call_1"),
        cr.function_output("call_1", "collab spawn failed: too many agents"),
    ])
    assert agg.state.tool_errors == 1
    assert texts(agg)[-1] == "✗ spawn_agent"


def test_file_change_and_patch_apply_end_paths_touched():
    agg = feed(make(), [
        cr.file_change("exec-1", ["C:\\Git\\proj\\a.py", "C:\\Git\\proj\\b.py"], at=ts(0)),
        cr.patch_apply_end("call_9", ["C:\\Git\\proj\\c.md"], at=ts(1)),
    ])
    st = agg.state
    assert set(st.files_touched) == {"C:\\Git\\proj\\a.py", "C:\\Git\\proj\\b.py", "C:\\Git\\proj\\c.md"}
    assert snap(agg).files_touched[0] == "C:\\Git\\proj\\c.md"  # newest first
    assert snap(agg).files_count == 3


VIEWS = "C:\\Git\\proj\\backend\\app\\views.py"


def test_one_file_in_every_spelling_counts_once():
    """Codex names one file relative to the workdir in patch headers and
    command reads, with forward slashes, and absolute in FileChange keys."""
    read = cr.command_execution("exec-1", ["pwsh.exe", "-Command", "Get-Content app/views.py"],
                                parsed_cmd=[{"type": "read", "cmd": "Get-Content app/views.py",
                                             "name": "views.py", "path": "app/views.py"}],
                                at=ts(4))
    read["payload"]["item"]["cwd"] = "file:///C:/Git/proj/backend"  # the command's own cwd
    records = [
        cr.session_meta(ROOT, cwd="c:\\Git\\proj", at=ts(0)),  # the meta's drive is lower-case
        cr.turn_context(cwd="C:\\Git\\proj", at=ts(0)),
        cr.exec_call("call_1", cr.patch_js("backend/app/views.py"), at=ts(1)),
        cr.file_change("fc-1", [VIEWS], at=ts(1, 1)),
        cr.exec_call("call_2", cr.patch_js("C:/Git/proj/backend/app/views.py"), at=ts(2)),
        cr.patch_apply_end("call_2", ["c:\\git\\PROJ\\backend\\app\\views.py"], at=ts(2, 1)),
        cr.function_call("apply_patch", {"input": "*** Begin Patch\n*** Update File: "
                                                  "backend\\app\\..\\app\\views.py\n@@\n-a\n+b\n"
                                                  "*** End Patch\n"}, "call_3", at=ts(3)),
        read,
    ]
    agg = feed(make(), records)
    st = agg.state
    assert list(st.files_touched) == [VIEWS]
    assert st.files_touched[VIEWS] == datetime(2026, 9, 22, 20, 51, 52, tzinfo=timezone.utc)
    s = snap(agg)
    assert (s.files_count, s.files_touched) == (1, (VIEWS,))
    agg.reset_source("main")
    feed(agg, records)
    assert list(st.files_touched) == [VIEWS]


def test_relative_paths_resolve_against_a_posix_cwd():
    agg = feed(make(), [
        cr.session_meta(ROOT, cwd="/home/me/proj"),
        cr.exec_call("call_1", cr.patch_js("src/a.py")),
        cr.patch_apply_end("call_1", ["/home/me/proj/src/a.py"]),
        cr.file_change("fc-1", ["/home/me/proj/src/A.py"]),  # case matters here
    ])
    assert set(agg.state.files_touched) == {"/home/me/proj/src/a.py", "/home/me/proj/src/A.py"}


def test_repeated_call_record_keeps_its_state():
    call = cr.exec_call("call_1", cr.exec_js("ls"))
    agg = feed(make(), [call, cr.exec_output("call_1"), call])
    st = agg.state
    assert st.tool_counts == {"shell": 1}
    assert st.tools["call_1"].done
    assert texts(agg) == ["→ shell ls", "✓ shell"]


# ---------------------------------------------------------------------------
# sub-agents
# ---------------------------------------------------------------------------
def test_sub_agent_activity_item_and_event_register_once():
    agg = feed(make(), [
        cr.sub_agent_item("call_1", "started", at=ts(0)),
        cr.sub_agent_event("call_1", "started", at=ts(0)),
        cr.sub_agent_event("call_2", "interacted", at=ts(0, 5)),
    ])
    st = agg.state
    assert list(st.subagents) == [CHILD]
    sub = st.subagents[CHILD]
    assert (sub.agent_type, sub.description, sub.finished) == \
        ("dependency_audit", "/root/dependency_audit", False)
    assert texts(agg) == ["⊕ subagent dependency_audit: /root/dependency_audit"]
    feed(agg, [
        cr.sub_agent_item("call_1", "completed", at=ts(1)),
        cr.sub_agent_event("call_1", "completed", at=ts(1)),
    ])
    assert sub.finished
    assert texts(agg)[1:] == ["⊙ subagent done: dependency_audit"]
    assert snap(agg).subagents_running == 0


def test_each_completion_of_a_retasked_sub_agent_is_logged():
    """Dedupe is per (call id, kind, thread): one activity reported in both
    formats counts once, a later completion (new id) is a new event."""
    agg = feed(make(), [
        cr.sub_agent_item("call_1", "started", at=ts(0)),
        cr.sub_agent_event("call_1", "started", at=ts(0)),
        cr.sub_agent_item("done_A", "completed", at=ts(1)),
        cr.sub_agent_event("done_A", "completed", at=ts(1)),
        cr.sub_agent_item("done_B", "completed", at=ts(2)),
        cr.sub_agent_event("done_B", "completed", at=ts(2)),
        cr.sub_agent_event("int_A", "interrupted", at=ts(3)),
        cr.sub_agent_item("int_A", "interrupted", at=ts(3)),
        cr.sub_agent_event("int_B", "interrupted", at=ts(4)),
    ])
    assert texts(agg) == ["⊕ subagent dependency_audit: /root/dependency_audit",
                          "⊙ subagent done: dependency_audit",
                          "⊙ subagent done: dependency_audit",
                          "⊘ subagent interrupted: dependency_audit",
                          "⊘ subagent interrupted: dependency_audit"]
    assert agg.state.subagents[CHILD].finished


def test_sub_agent_interrupted():
    agg = feed(make(), [
        cr.sub_agent_event("call_1", "started", agent_tid=GRANDCHILD, agent_path="/root/a/b"),
        cr.sub_agent_event("call_1", "interrupted", agent_tid=GRANDCHILD, agent_path="/root/a/b"),
    ])
    assert agg.state.subagents[GRANDCHILD].finished
    assert texts(agg)[-1] == "⊘ subagent interrupted: b"


# A child re-tasked with a follow-up (followup_task / send_message): the main
# thread logs its first task's `completed`, then only `interacted`.
def retask_main() -> list[dict]:
    return [
        cr.sub_agent_item("call_1", "started", at=ts(1)),
        cr.sub_agent_item("done_1", "completed", at=ts(2)),
        cr.sub_agent_event("i_1", "interacted", at=ts(3)),
    ]


def retask_child() -> list[dict]:
    return [
        cr.subagent_meta(CHILD, forked=False, at=ts(1)),
        cr.task_started(at=ts(1, 1)),
        cr.task_complete(at=ts(2)),     # the same instant as the main's `completed`
        cr.task_started(at=ts(3, 1)),   # the follow-up task: running again
        cr.exec_call("c1", cr.exec_js("rg foo"), at=ts(3, 2)),
    ]


@pytest.mark.parametrize("child_first", [False, True])
def test_retasked_child_runs_whatever_the_file_order(child_first):
    agg = make()
    if child_first:  # e.g. a big main file drained after a small child file
        feed(agg, retask_child(), AGENT)
        feed(agg, retask_main())
    else:
        feed(agg, retask_main())
        feed(agg, retask_child(), AGENT)
    sub = agg.state.subagents[CHILD]
    assert sub.finished is False and sub.open_tools == {"c1"}
    assert snap(agg).subagents_running == 1
    assert texts(agg)[-1] == "⊙ subagent done: dependency_audit"  # still logged


def test_main_reset_and_replay_keeps_a_retasked_child_running():
    agg = make()
    feed(agg, retask_main())
    feed(agg, retask_child(), AGENT)
    before = dataclasses.asdict(snap(agg))
    assert before["subagents_running"] == 1
    agg.reset_source("main")
    feed(agg, retask_main())
    assert agg.state.subagents[CHILD].finished is False
    assert dataclasses.asdict(snap(agg)) == before


def test_child_reset_and_replay_keeps_a_retasked_child_running():
    agg = make()
    feed(agg, retask_main())
    feed(agg, retask_child(), AGENT)
    agg.reset_source(AGENT)
    feed(agg, retask_child(), AGENT)
    assert agg.state.subagents[CHILD].finished is False


@pytest.mark.parametrize("child_first", [False, True])
def test_main_interrupt_newer_than_the_childs_start_ends_it(child_first):
    # killed by the main thread: the child itself never writes turn_aborted
    main = [cr.sub_agent_item("call_1", "started", at=ts(1)),
            cr.sub_agent_event("int_1", "interrupted", at=ts(5))]
    child = [cr.subagent_meta(CHILD, forked=False, at=ts(1)),
             cr.task_started(at=ts(1, 1)), cr.task_complete(at=ts(2)),
             cr.task_started(at=ts(3, 1)), cr.exec_call("c1", cr.exec_js("sleep 99"), at=ts(3, 2))]
    agg = make()
    for records, source in ([(child, AGENT), (main, "main")] if child_first
                            else [(main, "main"), (child, AGENT)]):
        feed(agg, records, source)
    assert agg.state.subagents[CHILD].finished is True
    assert snap(agg).subagents_running == 0


@pytest.mark.parametrize("child_first", [False, True])
def test_same_instant_done_and_start_do_not_depend_on_order(child_first):
    main = [cr.sub_agent_item("call_1", "started", at=ts(1)),
            cr.sub_agent_item("done_1", "completed", at=ts(3))]
    child = [cr.subagent_meta(CHILD, forked=False, at=ts(1)),
             cr.task_started(at=ts(1, 1)), cr.task_complete(at=ts(2, 59)),
             cr.task_started(at=ts(3))]  # a start wins a tie with the done
    agg = make()
    for records, source in ([(child, AGENT), (main, "main")] if child_first
                            else [(main, "main"), (child, AGENT)]):
        feed(agg, records, source)
    assert agg.state.subagents[CHILD].finished is False


def test_big_main_file_drained_after_its_child_keeps_the_child_running(tmp_path):
    """End to end: the tailer reads a big main file in 4 MB chunks, so a
    small child file is folded before the main's older `completed`."""
    from pool_coder.tailer import DEFAULT_MAX_READ
    main = cr.write_rollout(tmp_path, ROOT,
                            [cr.session_meta(ROOT, pad=DEFAULT_MAX_READ, at=ts(0))] + retask_main())
    child = cr.write_rollout(tmp_path, CHILD, retask_child(), local_ts="2026-09-22T23-48-53")
    assert main.stat().st_size > DEFAULT_MAX_READ > child.stat().st_size
    agg = make()
    source = JsonlSource(main, agg, discovery=_StubDiscovery(main, child), parse=parse_codex_line)
    source.initial_catchup()
    assert "⊙ subagent done: dependency_audit" in texts(agg)
    sub = agg.state.subagents[CHILD]
    assert sub.finished is False and sub.open_tools == {"c1"}
    # later polls do not change that (the main thread writes nothing new)
    cr.append_records(child, [cr.exec_output("c1", at=ts(4))])
    source.poll(now=0.0)
    assert sub.finished is False and sub.open_tools == set()


def test_discovery_registration_is_kept_and_announced_once():
    agg = make()
    agg.register_subagent(CHILD, "dependency_audit", "Pauli · /root/dependency_audit", "")
    feed(agg, [cr.sub_agent_item("call_1", "started")])
    sub = agg.state.subagents[CHILD]
    assert sub.description == "Pauli · /root/dependency_audit"
    assert [t for t in texts(agg) if t.startswith("⊕")] == \
        ["⊕ subagent dependency_audit: Pauli · /root/dependency_audit"]
    agg.register_subagent(GUARDIAN, "guardian", "", "")
    agg.register_subagent(GUARDIAN, "", "", "")
    assert [t for t in texts(agg) if t.startswith("⊕")][-1] == "⊕ subagent guardian: "
    assert agg.state.subagents[GUARDIAN].agent_type == "guardian"


def test_child_tokens_kept_separate_and_shown_in_snapshot():
    agg = make()
    feed(agg, main_session())
    feed(agg, child_session(), AGENT)
    st = agg.state
    child = st.source_tokens(AGENT)
    assert child == u(5_000, 4_000, 0, 100) + u(6_000, 5_000, 0, 150)
    assert st.cumulative_tokens() == st.source_tokens("main") + child
    view = next(v for v in snap(agg).subagents if v.agent_id == CHILD)
    assert view.tokens == child.billable_total
    assert view.turns == 2
    assert st.key_model[(AGENT, "c_resp_1")] == "gpt-5.6-sol"
    # a child's context never moves the main gauge
    assert st.current_context_tokens == 30_000


def test_child_task_events_and_tools():
    agg = make()
    feed(agg, [
        cr.subagent_meta(CHILD, forked=False, at=ts(0)),
        cr.task_started(at=ts(0)),
        cr.exec_call("c1", cr.exec_js("rg foo"), at=ts(0, 5)),
        cr.function_call("wait", {"cell_id": "3"}, "c2", at=ts(0, 6)),
    ], AGENT)
    st = agg.state
    sub = st.subagents[CHILD]
    assert sub.open_tools == {"c1", "c2"} and sub.last_tool == "wait"
    assert not sub.finished
    assert st.tools == {} and st.tool_counts == {}  # child tools are not main tools
    feed(agg, [cr.exec_output("c1", at=ts(1))], AGENT)
    assert sub.open_tools == {"c2"}
    feed(agg, [cr.task_complete(at=ts(2))], AGENT)
    assert sub.finished and sub.open_tools == set()
    feed(agg, [cr.task_started(at=ts(3))], AGENT)
    assert not sub.finished
    feed(agg, [cr.turn_aborted(at=ts(4))], AGENT)
    assert sub.finished
    assert sub.started_at == datetime(2026, 9, 22, 20, 47, 52, tzinfo=timezone.utc)
    assert sub.last_activity == datetime(2026, 9, 22, 20, 51, 52, tzinfo=timezone.utc)
    assert texts(agg) == []  # child records push no main events


def test_guardian_child_type_from_meta():
    agg = feed(make(), [cr.guardian_meta(GUARDIAN)], f"agent:{GUARDIAN}")
    assert agg.state.subagents[GUARDIAN].agent_type == "guardian"


def test_child_that_never_starts_a_task_is_not_running():
    """Real guardian threads are often just meta + compaction + settings."""
    agg = make()
    agg.register_subagent(GUARDIAN, "guardian", "", "")
    assert snap(agg).subagents_running == 1  # registered, nothing read yet
    feed(agg, [
        cr.guardian_meta(GUARDIAN),
        rec("response_item", {"type": "compaction", "encrypted_content": "gAAAA"}),
        cr.thread_settings("codex-auto-review", tid=GUARDIAN),
    ], f"agent:{GUARDIAN}")
    assert agg.state.subagents[GUARDIAN].finished
    assert snap(agg).subagents_running == 0
    feed(agg, [cr.task_started()], f"agent:{GUARDIAN}")
    assert snap(agg).subagents_running == 1
    # a replay of its file ends in the same state
    agg.reset_source(f"agent:{GUARDIAN}")
    feed(agg, [cr.guardian_meta(GUARDIAN), cr.task_started()], f"agent:{GUARDIAN}")
    assert not agg.state.subagents[GUARDIAN].finished


def test_reset_main_keeps_child_tokens():
    agg = make()
    feed(agg, main_session())
    feed(agg, child_session(), AGENT)
    child = agg.state.source_tokens(AGENT)
    agg.reset_source("main")
    st = agg.state
    assert st.source_tokens("main") == UsageTokens()
    assert st.source_tokens(AGENT) == child
    assert st.subagents[CHILD].turns == 2
    assert (st.turns, st.user_messages, st.tool_errors, st.last_prompt) == (0, 0, 0, None)
    assert st.compactions == [] and st.tools == {} and list(st.events) == []


def test_reset_and_replay_of_main_reproduces_the_snapshot():
    agg = make()
    feed(agg, main_session())
    feed(agg, child_session(), AGENT)
    before = dataclasses.asdict(snap(agg))
    assert before["compaction_count"] == 1 and before["user_messages"] == 1
    agg.reset_source("main")
    feed(agg, main_session())
    assert dataclasses.asdict(snap(agg)) == before


def test_reset_and_replay_of_a_child_reproduces_its_tokens():
    agg = make()
    feed(agg, main_session())
    feed(agg, child_session(), AGENT)
    before = dataclasses.asdict(snap(agg))
    agg.reset_source(AGENT)
    assert agg.state.source_tokens(AGENT) == UsageTokens()
    feed(agg, child_session(), AGENT)
    assert dataclasses.asdict(snap(agg)) == before


def test_reset_forgets_fallback_mode():
    records = [cr.token_count(cr.usage(1_000, 0, 0, 10)), cr.token_count(cr.usage(3_000, 0, 0, 30))]
    agg = feed(make(), records + [cr.token_usage("r1", inp=500, out=5)])
    assert agg.state.keys_by_source["main"] == {"r1"}
    agg.reset_source("main")
    feed(agg, records)  # a rewritten file without per-response records
    assert agg.state.cumulative_tokens() == u(3_000, 0, 0, 30)
    assert agg.state.turns == 2


# ---------------------------------------------------------------------------
# events & robustness
# ---------------------------------------------------------------------------
def test_every_event_uses_a_spec_prefix():
    agg = make()
    feed(agg, main_session() + [
        cr.sub_agent_event("call_7", "started", agent_tid=GRANDCHILD, agent_path="/root/x"),
        cr.sub_agent_event("call_7", "interrupted", agent_tid=GRANDCHILD, agent_path="/root/x"),
        cr.command_execution("exec-9", ["bash", "-lc", "false"], exit_code=1),
        cr.exec_call("call_9", cr.exec_js("sleep 1")),
        cr.turn_aborted("interrupted"),
    ])
    feed(agg, child_session(), AGENT)
    events = texts(agg)
    kinds = {e.kind for e in agg.state.events}
    assert kinds == {"tool", "result", "prompt", "text", "thinking", "compaction", "agent"}
    assert events and all(t.startswith(PREFIXES) for t in events), events


@pytest.mark.parametrize("raw", [
    {"type": "event_msg", "payload": {"type": "item_completed", "item": "x"}},
    {"type": "event_msg", "payload": {"type": "item_completed", "item": {"type": "UserMessage",
                                                                         "content": "nope"}}},
    {"type": "event_msg", "payload": {"type": "item_completed",
                                      "item": {"type": "CommandExecution", "parsed_cmd": "x",
                                               "exit_code": True}}},
    {"type": "event_msg", "payload": {"type": "item_completed",
                                      "item": {"type": "FileChange", "changes": ["a"]}}},
    {"type": "event_msg", "payload": {"type": "item_completed",
                                      "item": {"type": "SubAgentActivity", "kind": 3}}},
    {"type": "event_msg", "payload": {"type": "token_count", "info": {"total_token_usage": "x"}}},
    {"type": "event_msg", "payload": {"type": "token_count",
                                      "info": {"total_token_usage": {"input_tokens": "9",
                                                                     "output_tokens": None},
                                               "model_context_window": "big"}}},
    {"type": "event_msg", "payload": {"type": "task_started", "model_context_window": -5}},
    {"type": "event_msg", "payload": {"type": "thread_settings_applied", "thread_settings": []}},
    {"type": "event_msg", "payload": {"type": "user_message", "message": 42}},
    {"type": "event_msg", "payload": {"type": "sub_agent_activity", "agent_thread_id": ["x"]}},
    {"type": "event_msg", "payload": {"type": "patch_apply_end", "changes": None}},
    {"type": "event_msg", "payload": {"type": "turn_aborted", "reason": None}},
    {"type": "event_msg", "payload": "not a dict"},
    {"type": "response_item", "payload": {"type": "function_call"}},
    {"type": "response_item", "payload": {"type": "function_call", "name": 5, "arguments": "{bad"}},
    {"type": "response_item", "payload": {"type": "custom_tool_call", "name": "exec", "input": None}},
    {"type": "response_item", "payload": {"type": "local_shell_call", "action": "x"}},
    {"type": "response_item", "payload": {"type": "custom_tool_call_output", "call_id": None}},
    {"type": "response_item", "payload": {"type": "message", "role": "assistant", "content": "x"}},
    {"type": "response_item", "payload": {"type": "reasoning", "summary": [None, 1]}},
    {"type": "token_usage_record", "payload": {"thread_id": ROOT, "usage": "x"}},
    {"type": "token_usage_record", "payload": {"thread_id": ROOT, "usage": {"input_tokens": 1e400}}},
    {"type": "turn_context", "payload": {"sandbox_policy": 7, "collaboration_mode": "x"}},
    {"type": "session_meta", "payload": {"id": 7}},
    {"type": "session_meta", "payload": {"id": ROOT, "git": "x", "base_instructions": 3}},
    {"type": "compacted"},
    {"type": 5, "payload": []},
    {},
])
def test_malformed_records_never_raise(raw):
    for source in ("main", AGENT, "wfagent:x", "agent:"):
        agg = make()
        parsed = parse_codex_line(json.dumps(raw))
        assert parsed is not None
        agg.apply(source, parsed)
        snap(agg)


def test_register_workflow_is_harmless():
    agg = make()
    agg.register_workflow("run1", "wf", "desc", [("phase", "detail")])
    assert agg.state.workflows["run1"].name == "wf"
    assert snap(agg).workflows[0].phases == ("phase",)


# ---------------------------------------------------------------------------
# end to end through JsonlSource (real tailers, real RESET)
# ---------------------------------------------------------------------------
class _StubDiscovery:
    """Hands the source a main rollout, one child and its registration."""

    def __init__(self, main: Path, child: Path, extra: tuple[SubagentReg, ...] = ()):
        self.main, self.child, self.extra = main, child, extra

    def initial(self) -> DiscoveryDelta:
        return DiscoveryDelta(
            new_tailers=[TailerSpec("main", self.main), TailerSpec(AGENT, self.child)],
            subagents=[SubagentReg(CHILD, "dependency_audit", "Pauli · /root/dependency_audit", ""),
                       *self.extra],
        )

    def scan(self) -> DiscoveryDelta:
        return DiscoveryDelta()


def test_jsonl_source_catchup_and_rewrite_reset(tmp_path):
    main_records, child_records = main_session(), child_session()
    main = cr.write_rollout(tmp_path, ROOT, main_records)
    child = cr.write_rollout(tmp_path, CHILD, child_records, local_ts="2026-09-22T23-48-53")
    agg = make()
    source = JsonlSource(main, agg, discovery=_StubDiscovery(main, child), parse=parse_codex_line)
    source.initial_catchup()
    first = summary(agg)
    assert first["turns"] == 3 and first["child"].billable_total > 0
    assert agg.state.subagents[CHILD].description == "Pauli · /root/dependency_audit"
    assert [t for t in texts(agg) if t.startswith("⊕")] == \
        ["⊕ subagent dependency_audit: Pauli · /root/dependency_audit"]

    # Codex rewrites the file shorter (e.g. a migration): the tailer RESETs...
    cr.write_rollout(tmp_path, ROOT, main_records[:8])
    source.poll(now=0.0)
    assert agg.state.turns == 0 and agg.state.user_messages == 1
    assert agg.state.source_tokens(AGENT) == first["child"]
    # ...and once the rest is back, the fold matches the first catch-up
    cr.append_records(main, main_records[8:])
    source.poll(now=0.0)
    assert summary(agg) == first
    assert [t for t in texts(agg) if t.startswith("⊕")] == \
        ["⊕ subagent dependency_audit: Pauli · /root/dependency_audit"]


def test_main_rewrite_keeps_the_announcements_of_discovered_subagents(tmp_path):
    """Guardians and grandchildren have no `started` activity in the main file:
    only discovery announces them, once, so a main RESET must not lose them."""
    main_records, child_records = main_session(), child_session()
    main = cr.write_rollout(tmp_path, ROOT, main_records)
    child = cr.write_rollout(tmp_path, CHILD, child_records, local_ts="2026-09-22T23-48-53")
    extra = (SubagentReg(GUARDIAN, "guardian", "", ""),
             SubagentReg(GRANDCHILD, "pool_setup", "Leibniz · /root/dependency_audit/pool_setup", ""))
    agg = make()
    source = JsonlSource(main, agg, discovery=_StubDiscovery(main, child, extra),
                         parse=parse_codex_line)
    source.initial_catchup()
    first = [(e.at, e.kind, e.text) for e in agg.state.events]
    assert [t for _, _, t in first if t.startswith("⊕")] == [
        "⊕ subagent dependency_audit: Pauli · /root/dependency_audit",
        "⊕ subagent guardian: ",
        "⊕ subagent pool_setup: Leibniz · /root/dependency_audit/pool_setup",
    ]
    cr.write_rollout(tmp_path, ROOT, main_records[:8])  # rewritten shorter: RESET + replay
    source.poll(now=0.0)
    cr.append_records(main, main_records[8:])
    source.poll(now=0.0)
    assert [(e.at, e.kind, e.text) for e in agg.state.events] == first


def test_main_reset_keeps_a_live_spawned_subagents_announcement_in_place():
    """A child spawned while watching is announced by the main's `started`
    activity (tailed every 0.25 s) before discovery registers it (every 1.5 s)
    with a richer description. A main RESET + replay must put that timestamped
    "⊕" back where it was, with the same text."""
    main = [cr.session_meta(ROOT, at=ts(0)), cr.user_message_event("go", at=ts(0, 5)),
            cr.sub_agent_item("call_1", "started", at=ts(1)),
            cr.assistant_message("working", at=ts(2))]
    agg = feed(make(), main[:3])
    agg.register_subagent(CHILD, "dependency_audit", "Pauli · /root/dependency_audit", "")
    feed(agg, main[3:])
    before = [(e.at, e.kind, e.text) for e in agg.state.events]
    assert ("agent", "⊕ subagent dependency_audit: /root/dependency_audit") in \
        [(k, t) for at, k, t in before if at is not None]
    agg.reset_source("main")
    feed(agg, main)
    assert [(e.at, e.kind, e.text) for e in agg.state.events] == before


# ---------------------------------------------------------------------------
# forked threads: the parent's copied history is skipped
# ---------------------------------------------------------------------------
# UUIDv7 turn ids: the parent's turn predates CHILD's creation, the child's own
# turn follows it (ids sort like their millisecond timestamps).
PARENT_TURN = "01a0cadf-e000-7000-8000-000000000001"
OWN_TURN = "01a0cadf-ff00-7000-8000-000000000002"
assert ROOT < PARENT_TURN < CHILD < OWN_TURN


def stamped(record: dict, turn_id: str | None = None, thread_id: str | None = None) -> dict:
    """A copy of ``record`` with its payload's turn / thread id replaced."""
    out = json.loads(json.dumps(record))
    if turn_id is not None:
        out["payload"]["turn_id"] = turn_id
    if thread_id is not None:
        out["payload"]["thread_id"] = thread_id
    return out


def forked_thread_records() -> list[dict]:
    """A forked rollout as Codex writes it: own meta, a re-stamped copy of the
    parent's history, the thread's own turn, then copies interleaved later."""
    return [
        cr.subagent_meta(CHILD, forked=True, at=ts(1)),
        # -- the parent's history, copied (no or old turn ids) -----------------
        cr.session_meta(ROOT, at=ts(1)),
        cr.user_message_event("parent prompt", client_id="pc1", at=ts(1)),
        cr.assistant_message("parent says hi", at=ts(1)),
        cr.compacted(at=ts(1)),
        stamped(cr.patch_apply_end("pcall", ["C:\\parent\\a.py"], at=ts(1)), PARENT_TURN),
        cr.sub_agent_event("psa", "started", agent_tid=GRANDCHILD, at=ts(1)),
        stamped(cr.turn_context("gpt-5.5", at=ts(1)), PARENT_TURN),
        stamped(cr.task_started(at=ts(1)), PARENT_TURN),
        cr.exec_call("parent-call", cr.exec_js("git log"), at=ts(1)),
        stamped(cr.task_complete(at=ts(1)), PARENT_TURN),
        cr.thread_settings("gpt-5.5", tid=CHILD, at=ts(1)),
        cr.token_count(cr.usage(90_000, 80_000, 0, 900), window=WINDOW, at=ts(1)),
        # -- the thread's own first turn ------------------------------------------
        stamped(cr.task_started(at=ts(1, 1)), OWN_TURN),
        stamped(cr.turn_context("gpt-5.6-sol", at=ts(1, 2)), OWN_TURN),
        stamped(cr.user_message_item("own prompt", client_id="oc1", tid=CHILD, at=ts(1, 2)),
                OWN_TURN),
        cr.exec_call("own-call", cr.exec_js("git status"), at=ts(1, 3)),
        cr.token_usage("own_r1", thread_id=CHILD, inp=1_000, cached=500, out=10, at=ts(1, 4)),
        cr.exec_output("own-call", at=ts(1, 5)),
        # copies Codex keeps interleaving: the child's thread id, the parent's turn
        stamped(cr.file_change("pfc", ["C:\\parent\\b.py"], at=ts(1, 6)), PARENT_TURN, CHILD),
        stamped(cr.sub_agent_item("psa2", "started", agent_tid=GUARDIAN, at=ts(1, 6)),
                PARENT_TURN, CHILD),
        stamped(cr.task_complete(at=ts(1, 6)), PARENT_TURN),
        # own records after that
        stamped(cr.file_change("ofc", ["C:\\child\\c.py"], at=ts(1, 7)), OWN_TURN, CHILD),
        cr.compacted(at=ts(1, 8)),
        cr.token_usage("own_r2", thread_id=CHILD, inp=400, out=5, at=ts(1, 9)),
    ]


def test_forked_thread_opened_directly_skips_the_copied_parent_history():
    agg = feed(make(CHILD), forked_thread_records())
    st = agg.state
    assert st.user_messages == 1 and st.last_prompt == "own prompt"
    assert len(st.compactions) == 1 and st.compactions[0].before == 1_000
    assert st.compactions[0].after == 400
    assert set(st.files_touched) == {"C:\\child\\c.py"}
    assert st.subagents == {}
    assert st.tool_counts == {"shell": 1} and set(st.tools) == {"own-call"}
    assert st.model == "gpt-5.6-sol" and st.context_window == WINDOW
    assert st.keys_by_source["main"] == {"own_r1", "own_r2"}
    assert set(st.model_breakdown()) == {"gpt-5.6-sol"}
    assert not [t for t in texts(agg) if "parent" in t or "git log" in t]
    assert all(t.startswith(PREFIXES) for t in texts(agg))


def test_forked_child_source_ignores_copied_turns_and_tools():
    records = forked_thread_records()
    own_start = next(i for i, r in enumerate(records)
                     if r["payload"].get("turn_id") == OWN_TURN)
    agg = feed(make(), records[:own_start], AGENT)
    sub = agg.state.subagents[CHILD]
    # only the copied history so far: idle, no tools, no tokens
    assert sub.finished is True and not sub.last_tool and not sub.open_tools
    assert agg.state.source_tokens(AGENT) == UsageTokens()
    feed(agg, records[own_start:own_start + 5], AGENT)  # own turn, call, response
    assert sub.finished is False and sub.last_tool == "shell"
    # the parent's copied task_complete (old turn) must not finish the child
    feed(agg, records[own_start + 5:], AGENT)
    assert sub.finished is False and sub.turns == 2
    assert agg.state.keys_by_source[AGENT] == {"own_r1", "own_r2"}
    assert set(agg.state.subagents) == {CHILD}
    feed(agg, [stamped(cr.task_complete(at=ts(2)), OWN_TURN)], AGENT)
    assert sub.finished is True


def test_forked_thread_without_turn_ids_starts_at_its_first_own_response():
    agg = feed(make(CHILD), [
        cr.subagent_meta(CHILD, forked=True),
        cr.user_message_event("parent prompt", client_id="pc1"),
        cr.compacted(at=ts(0, 1)),
        cr.token_usage("own_r1", thread_id=CHILD, inp=100, out=1, at=ts(0, 2)),
        cr.user_message_event("own prompt", client_id="oc1", at=ts(0, 3)),
        cr.compacted(at=ts(0, 4)),
    ])
    st = agg.state
    assert st.user_messages == 1 and st.last_prompt == "own prompt"
    assert len(st.compactions) == 1


def passthrough(record: dict, turn_id: str) -> dict:
    """A response_item carrying its turn id where Codex puts it for those."""
    out = json.loads(json.dumps(record))
    out["payload"]["internal_chat_message_metadata_passthrough"] = {"turn_id": turn_id}
    return out


def rollout_turn_fork() -> list[dict]:
    """A real fork's head: copies re-tagged ``rollout-N`` (not UUIDs), a copied
    sub-agent start whose completion keeps the parent's old turn id."""
    return [
        cr.subagent_meta(CHILD, forked=True, at=ts(1)),
        stamped(cr.task_started(at=ts(1)), "rollout-5"),
        stamped(cr.user_message_item("parent prompt", client_id="pc1", tid=CHILD, at=ts(1)),
                "rollout-2"),
        stamped(cr.sub_agent_item("psa", "started", agent_tid=GRANDCHILD, at=ts(1)),
                "rollout-5", CHILD),
        stamped(cr.sub_agent_item("psa_done", "completed", agent_tid=GRANDCHILD, at=ts(1)),
                PARENT_TURN, CHILD),
        cr.exec_call("parent-call", cr.exec_js("git log"), at=ts(1)),  # no turn id at all
        stamped(cr.task_complete(at=ts(1)), "rollout-5"),
        # -- the thread's own first turn ----------------------------------------------
        stamped(cr.task_started(at=ts(1, 1)), OWN_TURN),
        stamped(cr.user_message_item("own prompt", client_id="oc1", tid=CHILD, at=ts(1, 2)),
                OWN_TURN),
        cr.token_usage("own_r1", thread_id=CHILD, inp=1_000, out=10, at=ts(1, 3)),
        # a copy interleaved later, named by its response metadata only
        passthrough(cr.exec_call("late-copy", cr.exec_js("git log -p"), at=ts(1, 4)), PARENT_TURN),
        passthrough(cr.exec_call("own-call", cr.exec_js("git status"), at=ts(1, 5)), OWN_TURN),
    ]


def test_rollout_turn_ids_do_not_end_the_copied_history():
    agg = feed(make(CHILD), rollout_turn_fork())
    st = agg.state
    assert st.subagents == {}  # no phantom sub-agent stuck "running"
    assert st.user_messages == 1 and st.last_prompt == "own prompt"
    assert set(st.tools) == {"own-call"} and st.tool_counts == {"shell": 1}
    assert st.keys_by_source["main"] == {"own_r1"}


def test_rollout_turn_ids_of_a_forked_child_source():
    records = rollout_turn_fork()
    own_start = next(i for i, r in enumerate(records) if r["payload"].get("turn_id") == OWN_TURN)
    agg = feed(make(), records[:own_start], AGENT)
    sub = agg.state.subagents[CHILD]
    assert sub.finished is True and not sub.open_tools  # only copies so far
    feed(agg, records[own_start:], AGENT)
    assert sub.finished is False and sub.open_tools == {"own-call"}
    assert set(agg.state.subagents) == {CHILD}


def test_unknown_turn_id_format_hides_only_until_the_first_own_response():
    # fail-safe: tokens always count, and an own response ends the copying
    agg = feed(make(), [
        cr.subagent_meta(CHILD, forked=True),
        cr.exec_call("copy", cr.exec_js("git log")),
        cr.task_started(),                     # "turn-1": cannot be ordered
        cr.exec_call("early", cr.exec_js("git diff")),
        cr.token_usage("r1", thread_id=CHILD, inp=10, out=1),
        cr.exec_call("own", cr.exec_js("git status")),
    ], AGENT)
    sub = agg.state.subagents[CHILD]
    assert sub.open_tools == {"own"} and sub.turns == 1


def test_non_forked_threads_keep_every_turn():
    old = "01a0cadf-0000-7000-8000-000000000003"  # older than ROOT itself
    assert old < ROOT
    agg = feed(make(), [
        cr.session_meta(ROOT),
        stamped(cr.task_started(), old),
        stamped(cr.user_message_item("hello", client_id="c9"), old),
        stamped(cr.file_change("f1", ["C:\\Git\\proj\\a.py"]), old),
    ])
    st = agg.state
    assert st.user_messages == 1 and set(st.files_touched) == {"C:\\Git\\proj\\a.py"}


def test_reset_and_replay_of_a_forked_thread_reproduces_the_snapshot():
    agg = feed(make(CHILD), forked_thread_records())
    before = dataclasses.asdict(snap(agg))
    agg.reset_source("main")
    feed(agg, forked_thread_records())
    assert dataclasses.asdict(snap(agg)) == before


@pytest.mark.parametrize("value, expected", [
    (CHILD, "01a0cadffe06"),
    (CHILD.upper(), "01a0cadffe06"),
    ("01a0cadf-fe06-41b3-ab69-1a4802df0b58", None),   # UUIDv4: no time order
    ("turn-1", None),
    ("01a0cadf-fe06-71b3-ab69", None),
    ("0xa0cadf-fe06-71b3-ab69-1a4802df0b58", None),
    (None, None),
    (42, None),
])
def test_uuid7_ms(value, expected):
    from pool_coder.codex.aggregator import _uuid7_ms
    assert _uuid7_ms(value) == expected
