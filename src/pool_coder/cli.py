"""Command-line entry point.

    pool-coder                 interactive picker -> live dashboard (Textual)
    pool-coder --session ID    monitor a session directly
    pool-coder --list          list sessions and exit
    pool-coder --once [--json] print one snapshot and exit (headless)
    pool-coder --codex ...     the same for OpenAI Codex (= --agent codex)

Claude Code is the default agent. The agent's ``Provider`` supplies session
listing, lookup and previews, so every mode works for either agent.

The headless paths import nothing from the UI, which is the acceptance test
for the core/UI decoupling.
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import sys
from datetime import datetime

from .config import Config
from .engine import Engine
from .format import (
    as_of_text,
    fmt_age,
    fmt_clock,
    fmt_duration,
    fmt_int,
    fmt_pct,
    fmt_pct100,
    fmt_tokens,
    fmt_usd,
    limit_window,
    reset_in,
)
from .paths import SessionInfo
from .pricing import Pricing
from .providers import CLAUDE, PROVIDERS, Provider, get_provider


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="pool-coder",
        description="Realtime monitor for coding-agent sessions (Claude Code or Codex).")
    p.add_argument("--agent", choices=sorted(PROVIDERS), default="claude",
                   help="which agent's sessions to monitor (default: claude)")
    p.add_argument("--codex", action="store_const", const="codex", dest="agent",
                   default="claude", help="shorthand for --agent codex")
    p.add_argument("--list", action="store_true", help="list sessions and exit")
    p.add_argument("--session", metavar="ID", help="monitor this session id (skip picker)")
    p.add_argument("--once", action="store_true", help="print a single snapshot and exit")
    p.add_argument("--json", action="store_true", help="with --once: emit JSON")
    p.add_argument("--all", action="store_true", help="include idle sessions in list/picker")
    p.add_argument("--serve", action="store_true",
                   help="serve the dashboard over HTTP (open it on your phone)")
    p.add_argument("--port", type=int, default=8765, help="HTTP port for --serve (default 8765)")
    p.add_argument("--host", default="0.0.0.0",
                   help="bind address for --serve (default 0.0.0.0 = all interfaces)")
    p.add_argument("--no-plan-limits", action="store_true",
                   help="disable the plan-limit panel")
    p.add_argument("--window", action="append", default=[], metavar="FAM=N",
                   help="context-window override, e.g. opus=200000 (repeatable); for Codex, "
                        "gpt=N (alias codex=N) is only a fallback for rollouts that report "
                        "no window")
    p.add_argument("--pricing", metavar="PATH", help="path to a pricing.toml override")
    return p


def _jsonable(obj) -> str:
    def default(o):
        if isinstance(o, datetime):
            return o.isoformat()
        return str(o)

    return json.dumps(dataclasses.asdict(obj), default=default, indent=2)


def _command(prov: Provider) -> str:
    """How to invoke pool-coder for this agent (Claude needs no flag)."""
    return "pool-coder" if prov is CLAUDE else f"pool-coder --{prov.name}"


def print_session_list(config: Config, show_all: bool) -> None:
    prov = get_provider(config.agent)
    sessions = prov.list_sessions()
    if not show_all:
        sessions = [s for s in sessions if s.age_seconds() <= config.active_window_seconds]
    if not sessions:
        agent = "" if prov is CLAUDE else f"{prov.label} "
        print(f"No active {agent}sessions in the last 30 min. Use --all to show older ones.")
        return
    print(f"{'':3}{'AGE':>10}  {'CTX':>5}  {'MODEL':<16} {'PROJECT':<22} LAST")
    for s in sessions[:60]:
        ov = prov.peek_session(s, config)
        live = " ● " if ov.is_live else "   "
        print(f"{live}{fmt_age(s.age_seconds()):>10}  {fmt_pct(ov.occupancy):>5}  "
              f"{(ov.model or '?'):<16.16} {ov.label:<22.22} {ov.last_shown[:46]}")
    cmd = _command(prov)
    print(f"\n{len(sessions)} session(s). Run `{cmd} --session <id>` or just `{cmd}`.")


def _codex_tokens_line(s) -> str:
    """Codex: "in" is the non-cached input; reasoning is already inside "out"."""
    cum = s.cumulative
    cache_w = f"cache_w {fmt_tokens(cum.cache_creation)}  " if cum.cache_creation > 0 else ""
    return (f"  TOKENS    in {fmt_tokens(cum.input)}  cached {fmt_tokens(cum.cache_read)}  "
            f"{cache_w}out {fmt_tokens(cum.output)}  reasoning {fmt_tokens(cum.reasoning)}  "
            f"(cache hit {fmt_pct(s.cache_hit_ratio)})")


def _plan_window(label: str, pct: float | None, resets_at: datetime | None) -> str | None:
    """One Codex rate-limit window, or None when the window is absent."""
    if pct is None and resets_at is None:
        return None
    value, reset, _ = limit_window(pct, resets_at)  # a passed reset shows no stale %
    return f"{label} {value}" + (f" ({reset})" if reset else "")


def _codex_plan_line(pl) -> str:
    """Codex limits come from local records: labelled windows, plan and age."""
    parts = [
        w for w in (_plan_window(pl.five_hour_label, pl.five_hour_pct, pl.five_hour_resets_at),
                    _plan_window(pl.seven_day_label, pl.seven_day_pct, pl.seven_day_resets_at))
        if w
    ]
    plan = " · ".join(x for x in (pl.plan_type, pl.note) if x)
    if plan:
        parts.append(plan)
    if pl.as_of is not None:
        parts.append(f"as of {as_of_text(pl.as_of)}")  # HH:MM (age): it can be days old
    return f"  PLAN      {'   '.join(parts) or 'no limit data'}"


def format_snapshot_human(snap) -> str:
    s = snap.session
    codex = s.agent == "codex"
    tag = (PROVIDERS.get(s.agent) or CLAUDE).version_tag
    lines: list[str] = []
    badge = "● live" if s.is_live else f"idle {fmt_age(s.idle_s)}"
    lines.append(f"Session {s.session_id}  [{badge}]")
    lines.append(f"  project   {s.cwd or s.project_hash}  ({s.git_branch or 'no branch'})")
    lines.append(f"  model     {s.model or '?'}   {tag} v{s.version or '?'}   mode={s.mode or '-'}")
    lines.append(f"  duration  {fmt_duration(s.duration_s)}   turns={s.turns}   prompts={s.user_messages}")
    if s.last_prompt:
        lines.append(f"  prompt    » {s.last_prompt[:90]}")
    lines.append("")
    lines.append(f"  CONTEXT   {fmt_int(s.current_context)} / {fmt_int(s.effective_window)} "
                 f"({fmt_pct(s.occupancy, 1)})   to-limit {fmt_tokens(s.tokens_to_limit)}   "
                 f"auto-compact headroom {fmt_tokens(s.auto_compact_headroom)}")
    if s.compaction_count:
        lines.append(f"            compactions={s.compaction_count} (last {fmt_clock(s.last_compaction)})")
    cum = s.cumulative
    lines.append(_codex_tokens_line(s) if codex else
                 f"  TOKENS    in {fmt_tokens(cum.input)}  cache_r {fmt_tokens(cum.cache_read)}  "
                 f"cache_w {fmt_tokens(cum.cache_creation)}  out {fmt_tokens(cum.output)}  "
                 f"(cache hit {fmt_pct(s.cache_hit_ratio)})")
    lines.append(f"  COST      {fmt_usd(s.cost.total_usd)} total   {fmt_usd(s.cost.per_min_usd)}/min   "
                 f"{int(s.tokens_per_min):,} tok/min")
    if s.web_search or s.web_fetch:
        lines.append(f"  WEB       searches={s.web_search}  fetches={s.web_fetch}")
    lines.append("")
    if s.current_activity:
        a = s.current_activity
        lines.append(f"  NOW       {a.name} {a.target[:60]}  ({fmt_duration(a.elapsed_s)})")
    elif s.in_flight:
        lines.append(f"  NOW       {len(s.in_flight)} tool(s) in flight")
    else:
        lines.append("  NOW       idle")
    top = "  ".join(f"{name}×{n}" for name, n in s.top_tools[:8])
    lines.append(f"  TOOLS     {s.tool_call_total} calls   errors={s.tool_errors}   {top}")
    lines.append(f"  FILES     {s.files_count} touched"
                 + (f"   last: {s.files_touched[0]}" if s.files_touched else ""))

    if s.subagents:
        lines.append("")
        lines.append(f"  SUBAGENTS ({s.subagents_running} running / {len(s.subagents)} total)")
        for sub in s.subagents[:8]:
            mark = "▶" if sub.running else "✓"
            lines.append(f"    {mark} {sub.agent_type:<16.16} {fmt_tokens(sub.tokens):>7}  "
                         f"turns={sub.turns} inflight={sub.in_flight_tools}  {sub.description[:40]}")
    if s.workflows:
        lines.append("")
        lines.append(f"  WORKFLOWS ({s.workflows_running_agents} agents running)")
        for wf in s.workflows[:6]:
            lines.append(f"    ⊞ {wf.name:<24.24} agents {wf.completed_agents}/{wf.total_agents} done, "
                         f"{wf.running_agents} running   phases: {', '.join(wf.phases) or '-'}")

    if snap.plan_limits is not None:
        pl = snap.plan_limits
        lines.append("")
        if pl.available and codex:
            lines.append(_codex_plan_line(pl))
        elif pl.available:
            lines.append(f"  PLAN      5h {fmt_pct100(pl.five_hour_pct)} (resets {reset_in(pl.five_hour_resets_at)})   "
                         f"7d {fmt_pct100(pl.seven_day_pct)}   opus {fmt_pct100(pl.seven_day_opus_pct)}   "
                         f"sonnet {fmt_pct100(pl.seven_day_sonnet_pct)}")
        else:
            lines.append(f"  PLAN      unavailable ({pl.error})")
    return "\n".join(lines)


def _choose(args: argparse.Namespace, config: Config) -> SessionInfo | None:
    prov = get_provider(config.agent)
    if args.session:
        return prov.find_session(args.session)
    sessions = prov.list_sessions()
    if not sessions:
        return None
    if args.once:
        active = [s for s in sessions if s.age_seconds() <= config.active_window_seconds]
        return (active or sessions)[0]
    return None  # interactive: let the picker decide


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    config = Config(plan_limits=not args.no_plan_limits, agent=args.agent)
    if args.all:
        config.active_window_seconds = 10 ** 18
    try:
        for spec in args.window:
            config.apply_window_override(spec)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    pricing = Pricing.load(args.pricing)

    if args.list:
        print_session_list(config, args.all)
        return 0

    if args.serve:
        from .web import serve
        return serve(args.host, args.port, config, pricing,
                     session=args.session, enable_plan_limits=config.plan_limits)

    info = _choose(args, config)
    if info is None and (args.once or args.session):
        cmd = _command(get_provider(config.agent))
        print(f"error: no matching session (try `{cmd} --list`)", file=sys.stderr)
        return 1

    if args.once:
        engine = Engine(info, config, pricing, enable_plan_limits=config.plan_limits)
        snap = engine.snapshot_once()
        print(_jsonable(snap) if args.json else format_snapshot_human(snap))
        return 0

    try:
        from .ui.app import run_app
    except Exception as exc:  # pragma: no cover - UI import guard
        print(f"error: UI unavailable ({exc}). Use --once or --list.", file=sys.stderr)
        return 1
    return run_app(info, config, pricing)


if __name__ == "__main__":
    raise SystemExit(main())
