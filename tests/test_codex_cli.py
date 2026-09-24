"""Codex through the CLI and engine: --agent/--codex, list, --once, headless.

A fixture ``$CODEX_HOME`` holds one root thread with a sub-agent child and a
guardian reviewer (both hidden from lists, rolled up under the root).
"""

from __future__ import annotations

import dataclasses
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone

import pytest

from codex_records import (
    CHILD,
    GUARDIAN,
    ROOT,
    WINDOW,
    exec_call,
    exec_js,
    exec_output,
    guardian_meta,
    rate_limits,
    session_meta,
    sub_agent_item,
    subagent_meta,
    task_complete,
    task_started,
    token_count,
    token_usage,
    ts,
    turn_context,
    usage,
    user_message_item,
    window,
    write_rollout,
)
from pool_coder import cli
from pool_coder.cli import build_parser, format_snapshot_human, main
from pool_coder.config import Config

OTHER = "01a0cb00-1111-7222-8333-444455556666"  # a second top-level thread

# Fixture tokens (usage_from: input = input_tokens - cached - cache_write):
#   main  resp_1  in 100k (80k cached)             out 5k  (2k reasoning)
#   main  resp_2  in 120k (100k cached, 5k write)  out 3k  (1k reasoning)
#   child resp_c1 in 40k  (30k cached)             out 2k  (0.5k reasoning)
# => input 45,000  cache_read 210,000  cache_write 5,000  output 10,000
# gpt-5.6-terra: $2 in / $12 out / $2.50 cache write / $0.20 cache read per 1M
TERRA_COST = (45_000 * 2 + 10_000 * 12 + 5_000 * 2.5 + 210_000 * 0.2) / 1e6  # $0.2645
GPT_FALLBACK_COST = (45_000 * 2 + 10_000 * 10 + 5_000 * 2.5 + 210_000 * 0.2) / 1e6


def _root_records(*, reported_window: bool = True) -> list[dict]:
    recs = [
        session_meta(ROOT, cwd="C:\\Git\\rootproj", cli_version="0.155.0",
                     model="gpt-5.6-terra", at=ts(0)),
        turn_context("gpt-5.6-terra", cwd="C:\\Git\\rootproj", at=ts(0, 1)),
    ]
    if reported_window:
        recs.append(task_started(at=ts(0, 2)))
    recs += [
        user_message_item("fix the flaky test", client_id="c1", at=ts(0, 3)),
        token_usage("resp_1", inp=100_000, cached=80_000, out=5_000, rs=2_000, at=ts(1)),
        exec_call("call_1", exec_js("git status"), at=ts(1, 5)),
        exec_output("call_1", at=ts(1, 10)),
        sub_agent_item("act_1", "started", CHILD, at=ts(1, 20)),
        token_usage("resp_2", inp=120_000, cached=100_000, cw=5_000, out=3_000, rs=1_000,
                    at=ts(2)),
    ]
    if reported_window:
        # Already in primary mode, so the totals are ignored; rate limits are read
        # by the Codex plan source.
        total = usage(220_000, 180_000, 5_000, 8_000, 3_000)
        recs.append(token_count(total, window=WINDOW, at=ts(2, 1), rate_limits=rate_limits(
            window(40.0, 300, resets_at=4_102_444_800),         # 2100-01-01
            window(10.0, 10080, resets_at=4_102_444_800), plan_type="plus")))
    recs.append(task_complete(at=ts(3)))
    return recs


def _child_records() -> list[dict]:
    return [
        subagent_meta(CHILD, forked=False, cwd="C:\\Git\\childproj", at=ts(1, 20)),
        turn_context("gpt-5.6-terra", cwd="C:\\Git\\childproj", at=ts(1, 21)),
        task_started(at=ts(1, 22)),
        token_usage("resp_c1", thread_id=CHILD, inp=40_000, cached=30_000, out=2_000, rs=500,
                    at=ts(1, 30)),
        task_complete(at=ts(1, 40)),
    ]


def _guardian_records() -> list[dict]:
    return [guardian_meta(GUARDIAN, cwd="C:\\Git\\guardproj", at=ts(2, 30))]


@pytest.fixture
def session(codex_home):
    """Root + child + guardian rollouts in one day folder; returns their paths."""
    return {
        "root": write_rollout(codex_home, ROOT, _root_records(), local_ts="2026-09-22T23-47-42"),
        "child": write_rollout(codex_home, CHILD, _child_records(),
                               local_ts="2026-09-22T23-49-12"),
        "guardian": write_rollout(codex_home, GUARDIAN, _guardian_records(),
                                  local_ts="2026-09-22T23-50-22"),
    }


def _run_json(capsys, argv: list[str]) -> dict:
    assert main(argv) == 0
    return json.loads(capsys.readouterr().out)


def _set_mtime(path, seconds_ago: float) -> None:
    t = time.time() - seconds_ago
    os.utime(path, (t, t))


# -- argument parsing ----------------------------------------------------------
def test_default_agent_is_claude():
    assert build_parser().parse_args([]).agent == "claude"
    assert build_parser().parse_args(["--once"]).agent == "claude"


def test_codex_flag_equals_agent_codex():
    p = build_parser()
    assert p.parse_args(["--codex"]).agent == "codex"
    assert p.parse_args(["--agent", "codex"]).agent == "codex"
    assert p.parse_args(["--agent", "claude"]).agent == "claude"
    assert p.parse_args(["--codex", "--agent", "claude"]).agent == "claude"  # last one wins


def test_unknown_agent_is_rejected(capsys):
    with pytest.raises(SystemExit):
        build_parser().parse_args(["--agent", "nope"])
    assert "invalid choice" in capsys.readouterr().err


def test_help_is_agent_neutral(capsys):
    with pytest.raises(SystemExit):
        build_parser().parse_args(["--help"])
    out = capsys.readouterr().out
    assert "Claude Code or Codex" in out
    assert "--codex" in out and "{claude,codex}" in out
    assert "OAuth" not in out
    # a rollout's reported window always wins: gpt=N is no override for Codex
    text = " ".join(out.split())
    assert "gpt=N (alias codex=N) is only a fallback for rollouts that report no window" in text
    assert "gpt=400000" not in text


def test_window_gpt_is_only_a_fallback_for_codex(session, codex_home, capsys):
    # the fixture reports 258,400; the override applies only where none is reported
    data = _run_json(capsys, ["--codex", "--once", "--json", "--no-plan-limits",
                              "--session", ROOT, "--window", "gpt=400000"])
    assert data["session"]["effective_window"] == WINDOW


# -- --once --json -------------------------------------------------------------------
def test_once_json_codex_session(session, capsys):
    data = _run_json(capsys, ["--agent", "codex", "--once", "--json", "--no-plan-limits",
                              "--session", ROOT])
    s = data["session"]
    assert s["agent"] == "codex"
    assert s["session_id"] == ROOT
    assert s["model"] == "gpt-5.6-terra"
    assert s["version"] == "0.155.0"
    assert s["effective_window"] == WINDOW == 258_400
    assert s["current_context"] == 120_000   # last prompt's input_tokens
    assert s["auto_compact_headroom"] == int(WINDOW * 0.947) - 120_000
    assert s["turns"] == 2 and s["user_messages"] == 1
    assert s["last_prompt"] == "fix the flaky test"
    assert data["plan_limits"] is None
    assert data["files_watched"] == 3        # root + child + guardian

    cum = s["cumulative"]
    assert (cum["input"], cum["cache_read"], cum["cache_creation"], cum["output"],
            cum["reasoning"]) == (45_000, 210_000, 5_000, 10_000, 3_500)

    subs = {sub["agent_id"]: sub for sub in s["subagents"]}
    assert set(subs) == {CHILD, GUARDIAN}
    child = subs[CHILD]
    assert child["tokens"] == 10_000 + 30_000 + 2_000 > 0
    assert child["agent_type"] == "dependency_audit"
    assert child["running"] is False
    assert subs[GUARDIAN]["agent_type"] == "guardian" and subs[GUARDIAN]["tokens"] == 0

    # every token priced at gpt-5.6-terra rates, not the [gpt] fallback
    assert s["cost"]["total_usd"] == pytest.approx(TERRA_COST, abs=1e-12)
    assert s["cost"]["total_usd"] == pytest.approx(0.2645, abs=1e-12)
    assert TERRA_COST != pytest.approx(GPT_FALLBACK_COST)
    assert [m for m, _ in s["cost"]["by_model"]] == ["gpt-5.6-terra"]


def test_window_override_applies_when_no_window_is_reported(codex_home, capsys):
    write_rollout(codex_home, ROOT, _root_records(reported_window=False))
    data = _run_json(capsys, ["--codex", "--once", "--json", "--no-plan-limits",
                              "--session", ROOT, "--window", "gpt=300000"])
    assert data["session"]["effective_window"] == 300_000


def test_reported_window_wins_over_override(session, capsys):
    data = _run_json(capsys, ["--codex", "--once", "--json", "--no-plan-limits",
                              "--session", ROOT, "--window", "gpt=300000"])
    assert data["session"]["effective_window"] == WINDOW


# -- --once (human) ------------------------------------------------------------------
def test_once_human_codex(session, capsys):
    assert main(["--codex", "--once", "--no-plan-limits", "--session", ROOT]) == 0
    out = capsys.readouterr().out
    assert "codex v0.155.0" in out
    assert "cc v" not in out
    assert ("  TOKENS    in 45.0k  cached 210.0k  cache_w 5.0k  out 10.0k  reasoning 3.5k  "
            "(cache hit 81%)") in out
    assert "  CONTEXT   120,000 / 258,400 (46.4%)" in out
    assert "  COST      $0.26 total" in out
    assert "PLAN" not in out
    assert "dependency_audit" in out


def test_once_human_codex_plan_line_from_local_records(session, capsys):
    assert main(["--codex", "--once", "--session", ROOT]) == 0
    out = capsys.readouterr().out
    plan = [ln for ln in out.splitlines() if ln.startswith("  PLAN")]
    assert len(plan) == 1
    as_of = datetime(2026, 9, 22, 20, 49, 53, tzinfo=timezone.utc).astimezone()
    assert plan[0].startswith("  PLAN      5-hour 40% (resets ")
    assert "   weekly 10% (resets " in plan[0]
    # the age too: a local snapshot can be days old
    assert re.search(rf"\)   plus   as of {as_of:%H:%M} \(\d+[smhd] ago\)$", plan[0]), plan[0]


# -- --list ----------------------------------------------------------------------------
def test_list_shows_root_but_not_child_or_guardian(session, capsys):
    assert main(["--codex", "--list", "--all"]) == 0
    out = capsys.readouterr().out
    assert "rootproj" in out and "fix the flaky test" in out
    assert "childproj" not in out and "guardproj" not in out
    assert "gpt-5.6-terra" in out
    assert out.rstrip().endswith(
        "1 session(s). Run `pool-coder --codex --session <id>` or just `pool-coder --codex`.")


def test_list_marks_a_thread_name_standing_in_for_the_prompt(session, capsys, monkeypatch):
    from pool_coder.codex import overview as codex_overview

    real = codex_overview.peek_codex_session

    def peek(info, config=None, **kw):
        return dataclasses.replace(real(info, config, **kw), last_text="Auto generated title",
                                   last_is_title=True)

    monkeypatch.setattr(codex_overview, "peek_codex_session", peek)
    assert main(["--codex", "--list", "--all"]) == 0
    assert "rootproj               [Auto generated title]" in capsys.readouterr().out


def test_list_without_codex_sessions(codex_home, capsys):
    assert main(["--codex", "--list"]) == 0
    assert capsys.readouterr().out == (
        "No active Codex sessions in the last 15 min. Use --all to show older ones.\n")


# -- choosing a session -----------------------------------------------------------------
@pytest.mark.parametrize("newer", [ROOT, OTHER])
def test_once_without_session_picks_most_recently_active(session, codex_home, capsys, newer):
    # OTHER's file name sorts after ROOT's; its records are newer (ts 30+) than ROOT's.
    other = write_rollout(codex_home, OTHER, [
        session_meta(OTHER, cwd="C:\\Git\\otherproj", at=ts(30)),
        turn_context("gpt-5.6-sol", at=ts(30, 1)),
        token_usage("resp_o1", thread_id=OTHER, inp=1_000, out=10, at=ts(31)),
    ], local_ts="2026-09-22T23-59-59")
    # Windows mtimes are unreliable, so activity = max(mtime, last record); the
    # child is the most recently active thread but must never be picked.
    _set_mtime(session["child"], 30)
    _set_mtime(session["root"], 120 if newer == ROOT else 900)
    _set_mtime(other, 120 if newer == OTHER else 900)
    data = _run_json(capsys, ["--codex", "--once", "--json", "--no-plan-limits"])
    assert data["session"]["session_id"] == newer
    assert data["session"]["agent"] == "codex"


def test_once_without_session_falls_back_to_newest_idle(session, codex_home, capsys):
    # Nothing active in the last 15 min: the newest by record time is used.
    # Fixed mtimes before every record (never relative to the clock), OTHER's
    # the older one and its file name sorting last: only its later records
    # can make it win, whatever the date or the directory order.
    other = write_rollout(codex_home, OTHER, [
        session_meta(OTHER, cwd="C:\\Git\\otherproj", at=ts(30)),
    ], local_ts="2026-09-22T23-59-59")
    for path, day in ((*((p, 20) for p in session.values()), (other, 19))):
        stamp = datetime(2026, 9, day, tzinfo=timezone.utc).timestamp()
        os.utime(path, (stamp, stamp))
    data = _run_json(capsys, ["--codex", "--once", "--json", "--no-plan-limits"])
    assert data["session"]["session_id"] == OTHER


def test_unknown_session_error_names_the_agent(codex_home, capsys):
    assert main(["--codex", "--once", "--session", "no-such-thread"]) == 1
    assert capsys.readouterr().err == (
        "error: no matching session (try `pool-coder --codex --list`)\n")


def test_codex_session_can_be_a_subagent_thread(session, capsys):
    data = _run_json(capsys, ["--codex", "--once", "--json", "--no-plan-limits",
                              "--session", CHILD])
    assert data["session"]["session_id"] == CHILD
    assert data["session"]["cumulative"]["output"] == 2_000


# -- Claude strings unchanged -------------------------------------------------------------
def test_claude_list_and_error_strings_unchanged(monkeypatch, capsys):
    from pool_coder import paths

    monkeypatch.setattr(paths, "list_sessions", lambda *a, **k: [])
    monkeypatch.setattr(paths, "find_session", lambda *a, **k: None)
    assert main(["--list"]) == 0
    assert capsys.readouterr().out == (
        "No active sessions in the last 15 min. Use --all to show older ones.\n")
    assert main(["--once", "--session", "abc"]) == 1
    assert capsys.readouterr().err == "error: no matching session (try `pool-coder --list`)\n"


def test_claude_list_footer_unchanged(monkeypatch, capsys, tmp_path):
    from pool_coder import overview, paths
    from pool_coder.overview import SessionOverview
    from pool_coder.paths import SessionInfo

    info = SessionInfo(tmp_path / "s.jsonl", "abcdef12-0000", "C--Git-x",
                       datetime.now(timezone.utc), 0)
    monkeypatch.setattr(paths, "list_sessions", lambda *a, **k: [info])
    monkeypatch.setattr(overview, "peek_session", lambda i, c=None, **k: SessionOverview(
        i, "C:\\Git\\x", "claude-opus-4-8", "main", 1000, 0.5, "hello", True))
    assert main(["--list"]) == 0
    out = capsys.readouterr().out
    assert " ● " in out and "claude-opus-4-8" in out and "hello" in out
    assert out.endswith("\n1 session(s). Run `pool-coder --session <id>` or just `pool-coder`.\n")


# -- format_snapshot_human --------------------------------------------------------------
def _snapshot(agent: str, plan=None, **usage_kw):
    from pool_coder.models import UsageTokens
    from pool_coder.pricing import Pricing
    from pool_coder.snapshot import Snapshot, build_session_snapshot
    from pool_coder.state import SessionState

    state = SessionState(session_id="s-1", project_hash="p", agent=agent)
    if usage_kw:
        state.record_usage("main", "r1", UsageTokens(**usage_kw), "gpt-5.6-sol")
    session = build_session_snapshot(state, Config(agent=agent), Pricing.load())
    return Snapshot(generated_at=datetime.now(timezone.utc), session=session, plan_limits=plan)


def test_format_claude_unchanged_shape():
    from pool_coder.snapshot import PlanLimitsView

    out = format_snapshot_human(_snapshot("claude", PlanLimitsView(
        available=True, five_hour_pct=40.0, seven_day_pct=10.0)))
    assert "   cc v?   mode=-" in out
    assert "  TOKENS    in 0  cache_r 0  cache_w 0  out 0  (cache hit 0%)" in out
    assert "reasoning" not in out
    assert "  PLAN      5h 40% (resets —)   7d 10%   opus —   sonnet —" in out


def test_format_codex_tokens_line_hides_zero_cache_write():
    out = format_snapshot_human(_snapshot("codex", input=1_500, cache_read=8_500,
                                          output=700, reasoning=300))
    assert "   codex v?   " in out
    assert "  TOKENS    in 1.5k  cached 8.5k  out 700  reasoning 300  (cache hit 85%)" in out
    out = format_snapshot_human(_snapshot("codex", input=1_500, cache_read=8_500,
                                          cache_creation=2_000, output=700))
    assert "  TOKENS    in 1.5k  cached 8.5k  cache_w 2.0k  out 700  reasoning 0  " in out


def test_format_codex_plan_business_has_no_windows():
    from pool_coder.snapshot import PlanLimitsView

    as_of = datetime.now(timezone.utc) - timedelta(hours=6)
    out = format_snapshot_human(_snapshot("codex", PlanLimitsView(
        available=True, plan_type="business", note="unlimited credits", as_of=as_of)))
    assert out.endswith(
        f"  PLAN      business · unlimited credits   as of {as_of.astimezone():%H:%M} (6h ago)")


def test_format_codex_plan_window_past_its_reset_shows_no_stale_percent():
    from pool_coder.snapshot import PlanLimitsView

    now = datetime.now(timezone.utc)
    out = format_snapshot_human(_snapshot("codex", PlanLimitsView(
        available=True, five_hour_pct=87.0, five_hour_resets_at=now - timedelta(hours=4),
        seven_day_pct=41.0, seven_day_resets_at=now + timedelta(days=3, minutes=1),
        plan_type="plus", as_of=now - timedelta(hours=6))))
    plan = out.splitlines()[-1]
    assert plan.startswith("  PLAN      5-hour — (reset, no newer data)   weekly 41% (resets 72h")
    assert "87%" not in plan and "resets now" not in plan
    assert plan.endswith(" (6h ago)")


def test_format_codex_plan_windows_use_their_labels():
    from pool_coder.snapshot import PlanLimitsView

    out = format_snapshot_human(_snapshot("codex", PlanLimitsView(
        available=True, seven_day_pct=55.0, seven_day_label="30d",
        five_hour_pct=None, note="limit reached: primary")))
    assert out.endswith("  PLAN      30d 55%   limit reached: primary")
    assert "5-hour" not in out


def test_format_codex_plan_unavailable_and_empty():
    from pool_coder.snapshot import PlanLimitsView

    out = format_snapshot_human(_snapshot("codex", PlanLimitsView(
        available=False, error="no Codex rate-limit data yet")))
    assert out.endswith("  PLAN      unavailable (no Codex rate-limit data yet)")
    out = format_snapshot_human(_snapshot("codex", PlanLimitsView(available=True)))
    assert out.endswith("  PLAN      no limit data")


# -- the headless path never imports the UI -------------------------------------------------
_HEADLESS = """
import sys
from pool_coder.cli import main
for argv in ARGVS:
    rc = main(argv)
    assert rc == 0, (argv, rc)
    print("=====", flush=True)
bad = sorted(m for m in sys.modules
             if m.split(".")[0] in ("textual", "rich") or m.startswith("pool_coder.ui"))
assert not bad, bad
"""


def test_headless_codex_path_does_not_import_ui(session, codex_home, tmp_path):
    argvs = [
        ["--codex", "--once", "--session", ROOT],                  # plan limits on (local)
        ["--codex", "--once", "--json", "--no-plan-limits", "--session", ROOT],
        ["--codex", "--list", "--all"],
        ["--list"],                                                # Claude, empty home
    ]
    empty_home = tmp_path / "home"
    empty_home.mkdir()
    env = dict(os.environ, CODEX_HOME=str(codex_home), PYTHONIOENCODING="utf-8",
               USERPROFILE=str(empty_home), HOME=str(empty_home))
    code = f"ARGVS = {argvs!r}\n{_HEADLESS}"
    proc = subprocess.run([sys.executable, "-c", code], env=env, capture_output=True,
                          text=True, encoding="utf-8", timeout=120)
    assert proc.returncode == 0, proc.stderr
    parts = proc.stdout.split("=====\n")
    assert "codex v0.155.0" in parts[0] and "PLAN      5-hour 40%" in parts[0]
    assert json.loads(parts[1])["session"]["agent"] == "codex"
    assert "rootproj" in parts[2] and "childproj" not in parts[2]
    assert parts[3].startswith("No active sessions in the last 15 min.")


def test_cli_module_has_no_ui_imports_at_top_level():
    import ast
    import inspect

    tree = ast.parse(inspect.getsource(cli))
    top = [n for n in tree.body if isinstance(n, (ast.Import, ast.ImportFrom))]
    names = [a.name for n in top for a in n.names] + [
        n.module or "" for n in top if isinstance(n, ast.ImportFrom)]
    assert not [n for n in names if "ui" in n.split(".") or n in ("textual", "rich")]
