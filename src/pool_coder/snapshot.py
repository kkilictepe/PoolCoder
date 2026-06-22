"""Immutable, point-in-time views handed to the UI.

The reader thread builds a ``Snapshot`` from the live ``SessionState`` and
hands it off; the UI only ever reads these frozen objects, so it never races
the mutating state. Nothing here does I/O.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone

from .config import AUTO_COMPACT_FRACTION, Config, LIVE_WINDOW_SECONDS
from .models import UsageTokens
from .pricing import Pricing
from .state import SessionState


def _elapsed(start: datetime | None, now: datetime) -> float:
    return (now - start).total_seconds() if start else 0.0


@dataclass(frozen=True)
class ToolView:
    name: str
    target: str
    elapsed_s: float
    source_id: str


@dataclass(frozen=True)
class SubagentView:
    agent_id: str
    agent_type: str
    description: str
    running: bool
    turns: int
    in_flight_tools: int
    last_tool: str | None
    tokens: int
    elapsed_s: float


@dataclass(frozen=True)
class WorkflowView:
    run_id: str
    name: str
    description: str
    phases: tuple[str, ...]
    running_agents: int
    completed_agents: int
    total_agents: int
    tokens: int


@dataclass(frozen=True)
class EventView:
    at: datetime | None
    kind: str
    text: str


@dataclass(frozen=True)
class CostView:
    total_usd: float
    per_min_usd: float
    by_model: tuple[tuple[str, float], ...]


@dataclass(frozen=True)
class PlanLimitsView:
    available: bool = False
    error: str | None = None
    five_hour_pct: float | None = None
    five_hour_resets_at: datetime | None = None
    seven_day_pct: float | None = None
    seven_day_opus_pct: float | None = None
    seven_day_sonnet_pct: float | None = None
    seven_day_resets_at: datetime | None = None
    extra_enabled: bool = False
    extra_pct: float | None = None


@dataclass(frozen=True)
class SessionSnapshot:
    session_id: str
    project_hash: str
    cwd: str | None
    git_branch: str | None
    version: str | None
    model: str | None
    mode: str | None
    title: str | None
    last_prompt: str | None
    started_at: datetime | None
    last_record_at: datetime | None
    duration_s: float
    idle_s: float
    is_live: bool
    # context window
    current_context: int
    max_context: int
    effective_window: int
    occupancy: float
    tokens_to_limit: int
    auto_compact_headroom: int
    compaction_count: int
    last_compaction: datetime | None
    context_history: tuple[int, ...]
    # tokens & cost
    cumulative: UsageTokens
    latest: UsageTokens
    cache_hit_ratio: float
    cost: CostView
    web_search: int
    web_fetch: int
    # activity
    turns: int
    user_messages: int
    tokens_per_min: float
    msgs_per_min: float
    top_tools: tuple[tuple[str, int], ...]
    tool_call_total: int
    tool_errors: int
    in_flight: tuple[ToolView, ...]
    current_activity: ToolView | None
    files_touched: tuple[str, ...]
    files_count: int
    events: tuple[EventView, ...]
    subagents: tuple[SubagentView, ...]
    subagents_running: int
    workflows: tuple[WorkflowView, ...]
    workflows_running_agents: int


@dataclass(frozen=True)
class Snapshot:
    generated_at: datetime
    session: SessionSnapshot
    plan_limits: PlanLimitsView | None = None
    loading: bool = False
    files_watched: int = 0
    notes: tuple[str, ...] = field(default_factory=tuple)


def build_session_snapshot(
    state: SessionState,
    config: Config,
    pricing: Pricing,
    now: datetime | None = None,
) -> SessionSnapshot:
    now = now or datetime.now(timezone.utc)

    window = config.window_for(state.model, state.max_context_tokens)
    current = state.current_context_tokens
    occupancy = (current / window) if window else 0.0
    auto_compact_at = int(window * AUTO_COMPACT_FRACTION)

    cumulative = state.cumulative_tokens()
    duration_s = _elapsed(state.started_at, state.last_record_at or now)
    minutes = max(duration_s / 60.0, 1 / 60.0)

    by_model = state.model_breakdown()
    cost_by_model = sorted(
        ((m, pricing.cost(u, m)) for m, u in by_model.items()),
        key=lambda kv: kv[1],
        reverse=True,
    )
    total_cost = sum(c for _, c in cost_by_model)
    cost = CostView(
        total_usd=total_cost,
        per_min_usd=total_cost / minutes,
        by_model=tuple((m, c) for m, c in cost_by_model),
    )

    in_flight = tuple(
        ToolView(t.tool.name, t.tool.target or "", _elapsed(t.started_at, now), t.source_id)
        for t in sorted(state.in_flight_tools(), key=lambda t: t.started_at or now)
    )
    activity = state.current_activity()
    current_activity = (
        ToolView(activity.tool.name, activity.tool.target or "",
                 _elapsed(activity.started_at, now), activity.source_id)
        if activity else None
    )

    top_tools = tuple(sorted(state.tool_counts.items(), key=lambda kv: kv[1], reverse=True))
    files_sorted = tuple(
        f for f, _ in sorted(state.files_touched.items(), key=lambda kv: kv[1], reverse=True)
    )
    events = tuple(EventView(e.at, e.kind, e.text) for e in state.events)

    subs = []
    for sub in state.subagents.values():
        subs.append(SubagentView(
            agent_id=sub.agent_id,
            agent_type=sub.agent_type or "?",
            description=sub.description,
            running=not sub.finished,
            turns=sub.turns,
            in_flight_tools=sub.in_flight_tools,
            last_tool=sub.last_tool,
            tokens=state.source_tokens(f"agent:{sub.agent_id}").billable_total,
            elapsed_s=_elapsed(sub.started_at, sub.last_activity or now),
        ))
    subs.sort(key=lambda s: (not s.running, -s.tokens))
    subagents_running = sum(1 for s in subs if s.running)

    wfs = []
    for wf in state.workflows.values():
        wfs.append(WorkflowView(
            run_id=wf.run_id,
            name=wf.name or wf.run_id,
            description=wf.description,
            phases=tuple(p.title for p in wf.phases),
            running_agents=wf.running_agents,
            completed_agents=wf.completed_agents,
            total_agents=wf.total_agents,
            tokens=state.source_tokens(f"wfagent:{wf.run_id}").billable_total,
        ))
    wfs.sort(key=lambda w: (-w.running_agents, -w.total_agents))
    wf_running = sum(w.running_agents for w in wfs)

    idle_s = _elapsed(state.last_record_at, now)
    last_compaction = state.compactions[-1].at if state.compactions else None

    return SessionSnapshot(
        session_id=state.session_id,
        project_hash=state.project_hash,
        cwd=state.cwd,
        git_branch=state.git_branch,
        version=state.version,
        model=state.model,
        mode=state.mode,
        title=state.title,
        last_prompt=state.last_prompt,
        started_at=state.started_at,
        last_record_at=state.last_record_at,
        duration_s=duration_s,
        idle_s=idle_s,
        is_live=idle_s <= LIVE_WINDOW_SECONDS,
        current_context=current,
        max_context=state.max_context_tokens,
        effective_window=window,
        occupancy=occupancy,
        tokens_to_limit=max(window - current, 0),
        auto_compact_headroom=max(auto_compact_at - current, 0),
        compaction_count=len(state.compactions),
        last_compaction=last_compaction,
        context_history=tuple(state.context_history[-120:]),
        cumulative=cumulative,
        latest=state.latest_usage,
        cache_hit_ratio=cumulative.cache_hit_ratio,
        cost=cost,
        web_search=cumulative.web_search,
        web_fetch=cumulative.web_fetch,
        turns=state.turns,
        user_messages=state.user_messages,
        tokens_per_min=cumulative.billable_total / minutes,
        msgs_per_min=state.turns / minutes,
        top_tools=top_tools,
        tool_call_total=sum(state.tool_counts.values()),
        tool_errors=state.tool_errors,
        in_flight=in_flight,
        current_activity=current_activity,
        files_touched=files_sorted,
        files_count=len(state.files_touched),
        events=events,
        subagents=tuple(subs),
        subagents_running=subagents_running,
        workflows=tuple(wfs),
        workflows_running_agents=wf_running,
    )
