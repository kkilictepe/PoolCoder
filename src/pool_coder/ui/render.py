"""Pure snapshot -> Rich renderable functions (no Textual, easily testable)."""

from __future__ import annotations

from rich.console import Group
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from ..format import (
    fmt_age,
    fmt_clock,
    fmt_duration,
    fmt_int,
    fmt_pct,
    fmt_pct100,
    fmt_tokens,
    fmt_usd,
    reset_in,
)
from ..snapshot import Snapshot

_KIND_STYLE = {
    "tool": "cyan",
    "result": "green",
    "prompt": "bold white",
    "text": "white",
    "thinking": "italic grey54",
    "compaction": "magenta",
    "agent": "yellow",
    "workflow": "blue",
}


def bar(fraction: float | None, width: int = 30) -> Text:
    frac = max(0.0, min(fraction or 0.0, 1.0))
    filled = int(round(frac * width))
    color = "green" if frac < 0.6 else "yellow" if frac < 0.85 else "red"
    out = Text()
    out.append("█" * filled, style=color)
    out.append("░" * (width - filled), style="grey37")
    return out


def _kv() -> Table:
    t = Table.grid(padding=(0, 1))
    t.add_column(justify="right", style="grey62", no_wrap=True)
    t.add_column(justify="left")
    return t


def header_bar(snap: Snapshot) -> Panel:
    s = snap.session
    t = Text()
    if snap.loading:
        t.append("⏳ loading ", style="yellow")
    t.append("● live" if s.is_live else f"idle {fmt_age(s.idle_s)}",
             style="bold green" if s.is_live else "grey62")
    t.append("   ")
    t.append(s.cwd or s.project_hash, style="bold")
    t.append(f"  ({s.git_branch or 'no branch'})", style="grey62")
    t.append(f"   {s.model or '?'}", style="cyan")
    if s.mode and s.mode != "normal":
        t.append(f"  [{s.mode}]", style="magenta")
    t.append(f"   {fmt_duration(s.duration_s)} · {s.turns} turns", style="grey62")
    sub = Text(f"» {s.last_prompt}" if s.last_prompt else "(no prompt yet)",
               style="italic grey74", overflow="ellipsis", no_wrap=True)
    return Panel(Group(t, sub), title=f"pool-coder · {s.session_id[:8]}",
                 border_style="blue", padding=(0, 1))


def panel_context(snap: Snapshot) -> Panel:
    s = snap.session
    head = Text()
    head.append(fmt_int(s.current_context), style="bold")
    head.append(f" / {fmt_int(s.effective_window)} tokens   ")
    head.append(fmt_pct(s.occupancy, 1),
                style="bold " + ("green" if s.occupancy < 0.6 else "yellow" if s.occupancy < 0.85 else "red"))
    kv = _kv()
    kv.add_row("to limit", fmt_tokens(s.tokens_to_limit))
    kv.add_row("compact headroom", fmt_tokens(s.auto_compact_headroom))
    comp = f"{s.compaction_count}"
    if s.compaction_count:
        comp += f"  (last {fmt_clock(s.last_compaction)})"
    kv.add_row("compactions", comp)
    kv.add_row("peak", fmt_tokens(s.max_context))
    return Panel(Group(bar(s.occupancy, 34), head, kv),
                 title="Context window", border_style="blue", padding=(0, 1))


def panel_tokens(snap: Snapshot) -> Panel:
    s = snap.session
    c = s.cumulative
    kv = _kv()
    kv.add_row("input", fmt_tokens(c.input))
    kv.add_row("cache read", fmt_tokens(c.cache_read))
    kv.add_row("cache write", fmt_tokens(c.cache_creation))
    kv.add_row("output", fmt_tokens(c.output))
    kv.add_row("cache hit", fmt_pct(s.cache_hit_ratio))
    cost = Text()
    cost.append(fmt_usd(s.cost.total_usd), style="bold green")
    cost.append(f"  ·  {fmt_usd(s.cost.per_min_usd)}/min  ·  {fmt_tokens(s.tokens_per_min)}/min",
                style="grey74")
    body = [kv, cost]
    if s.cost.by_model and len(s.cost.by_model) > 1:
        models = Text(overflow="ellipsis", no_wrap=True)
        for m, c2 in s.cost.by_model[:3]:
            models.append(f"{m.split('-')[1] if '-' in m else m}:{fmt_usd(c2)} ", style="grey62")
        body.append(models)
    if s.web_search or s.web_fetch:
        body.append(Text(f"web: {s.web_search} search · {s.web_fetch} fetch", style="grey62"))
    return Panel(Group(*body), title="Tokens & cost", border_style="blue", padding=(0, 1))


def panel_activity(snap: Snapshot) -> Panel:
    s = snap.session
    rows = []
    now_line = Text()
    if s.current_activity:
        a = s.current_activity
        now_line.append("▶ ", style="bold yellow")
        now_line.append(a.name, style="bold")
        now_line.append(f" {a.target}", style="grey74", )
        now_line.append(f"  ({fmt_duration(a.elapsed_s)})", style="grey50")
    elif s.in_flight:
        now_line.append(f"▶ {len(s.in_flight)} tools in flight", style="yellow")
    else:
        now_line.append("· idle", style="grey50")
    now_line.no_wrap = True
    now_line.overflow = "ellipsis"
    rows.append(now_line)

    kv = _kv()
    kv.add_row("tool calls", f"{s.tool_call_total}   (errors {s.tool_errors})")
    kv.add_row("files", str(s.files_count))
    rows.append(kv)
    if s.top_tools:
        tools = Text(overflow="ellipsis", no_wrap=True)
        for name, n in s.top_tools[:7]:
            tools.append(f"{name}", style="cyan")
            tools.append(f"×{n}  ", style="grey62")
        rows.append(tools)
    return Panel(Group(*rows), title="Activity", border_style="blue", padding=(0, 1))


def panel_subagents(snap: Snapshot) -> Panel:
    s = snap.session
    if not s.subagents:
        return Panel(Text("none", style="grey50"), title="Subagents",
                     border_style="grey37", padding=(0, 1))
    t = Table.grid(padding=(0, 1))
    t.add_column(width=2)
    t.add_column(style="bold", no_wrap=True)
    t.add_column(justify="right", style="grey62", no_wrap=True)
    t.add_column(overflow="ellipsis", no_wrap=True)
    for sub in s.subagents[:10]:
        mark = Text("▶", style="yellow") if sub.running else Text("✓", style="green")
        t.add_row(mark, sub.agent_type, fmt_tokens(sub.tokens), sub.description)
    title = f"Subagents · {s.subagents_running} running / {len(s.subagents)}"
    return Panel(t, title=title, border_style="blue", padding=(0, 1))


def panel_workflows(snap: Snapshot) -> Panel:
    s = snap.session
    if not s.workflows:
        return Panel(Text("none", style="grey50"), title="Workflows",
                     border_style="grey37", padding=(0, 1))
    rows = []
    for wf in s.workflows[:6]:
        line = Text(overflow="ellipsis", no_wrap=True)
        running = wf.running_agents > 0
        line.append("⊞ ", style="yellow" if running else "blue")
        line.append(wf.name, style="bold")
        line.append(f"  {wf.completed_agents}/{wf.total_agents} done", style="grey74")
        if running:
            line.append(f" · {wf.running_agents} running", style="yellow")
        if wf.phases:
            line.append(f"  [{', '.join(wf.phases)}]", style="grey50")
        rows.append(line)
    title = f"Workflows · {s.workflows_running_agents} agents running"
    return Panel(Group(*rows), title=title, border_style="blue", padding=(0, 1))


def panel_plan(snap: Snapshot) -> Panel:
    pl = snap.plan_limits
    if pl is None:
        return Panel(Text("disabled", style="grey50"), title="Plan limits",
                     border_style="grey37", padding=(0, 1))
    if not pl.available:
        return Panel(Text(pl.error or "unavailable", style="grey50"),
                     title="Plan limits", border_style="grey37", padding=(0, 1))
    kv = _kv()
    kv.add_row("5-hour", f"{fmt_pct100(pl.five_hour_pct)}   resets {reset_in(pl.five_hour_resets_at)}")
    kv.add_row("weekly", fmt_pct100(pl.seven_day_pct))
    kv.add_row("wk opus", fmt_pct100(pl.seven_day_opus_pct))
    kv.add_row("wk sonnet", fmt_pct100(pl.seven_day_sonnet_pct))
    body = [bar((pl.five_hour_pct or 0) / 100.0, 34), kv]
    if pl.extra_enabled:
        body.append(Text(f"extra usage: {fmt_pct100(pl.extra_pct)}", style="grey62"))
    return Panel(Group(*body), title="Plan limits", border_style="blue", padding=(0, 1))


def panel_events(snap: Snapshot) -> Panel:
    s = snap.session
    rows = []
    for ev in list(s.events)[-14:]:
        line = Text(overflow="ellipsis", no_wrap=True)
        line.append(f"{fmt_clock(ev.at):>8} ", style="grey42")
        line.append(ev.text, style=_KIND_STYLE.get(ev.kind, "white"))
        rows.append(line)
    if not rows:
        rows.append(Text("waiting for activity…", style="grey50"))
    return Panel(Group(*rows), title="Recent activity", border_style="blue", padding=(0, 1))
