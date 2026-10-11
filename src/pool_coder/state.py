"""Mutable live state for one monitored session.

Owned exclusively by the reader thread. The UI never touches this — it reads
immutable snapshots built from it (see ``snapshot.py``). Token totals are kept
as a per-turn map (single source of truth) and summed lazily, which keeps
replay-after-rotation correct without running counters to unwind.

The mutators shared by every agent's fold (``record_usage``, ``drop_usage``,
``drop_source_tokens``, ``reset_main``, ``update_context``) live here so the
Claude and Codex aggregators apply identical bookkeeping.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from datetime import datetime

from .config import AUTO_COMPACT_FRACTION, COMPACTION_DROP_FRACTION
from .models import EMPTY_USAGE, ToolUse, UsageTokens

EVENT_LOG_MAX = 400
CONTEXT_HISTORY_MAX = 600  # per-turn context samples kept for the sparkline


@dataclass
class ToolStatus:
    tool: ToolUse
    source_id: str = "main"
    started_at: datetime | None = None
    done: bool = False
    is_error: bool | None = None
    ended_at: datetime | None = None
    result_preview: str | None = None


@dataclass
class SubagentStatus:
    agent_id: str
    agent_type: str = ""
    description: str = ""
    parent_tool_use_id: str = ""
    started_at: datetime | None = None
    last_activity: datetime | None = None
    finished: bool = False
    turns: int = 0
    open_tools: set[str] = field(default_factory=set)
    last_tool: str | None = None

    @property
    def in_flight_tools(self) -> int:
        return len(self.open_tools)


@dataclass
class WorkflowPhase:
    title: str
    detail: str = ""


@dataclass
class WorkflowStatus:
    run_id: str
    name: str = ""
    description: str = ""
    phases: list[WorkflowPhase] = field(default_factory=list)
    started_keys: set[str] = field(default_factory=set)
    result_keys: set[str] = field(default_factory=set)

    @property
    def running_agents(self) -> int:
        return len(self.started_keys - self.result_keys)

    @property
    def completed_agents(self) -> int:
        return len(self.result_keys)

    @property
    def total_agents(self) -> int:
        return len(self.started_keys | self.result_keys)


@dataclass
class CompactionEvent:
    at: datetime | None
    before: int
    after: int


@dataclass
class Event:
    at: datetime | None
    kind: str  # tool | result | prompt | compaction | agent | workflow
    text: str


@dataclass
class SessionState:
    session_id: str = ""
    project_hash: str = ""
    main_path: str = ""
    cwd: str | None = None
    git_branch: str | None = None
    version: str | None = None
    model: str | None = None
    mode: str | None = None
    title: str | None = None
    last_prompt: str | None = None

    started_at: datetime | None = None
    last_record_at: datetime | None = None

    # context window
    current_context_tokens: int = 0
    max_context_tokens: int = 0
    prev_context_tokens: int = 0
    latest_usage: UsageTokens = field(default_factory=UsageTokens)
    context_history: list[int] = field(default_factory=list)
    compactions: list[CompactionEvent] = field(default_factory=list)

    # token folding (single source of truth)
    tokens_by_key: dict[tuple[str, str], UsageTokens] = field(default_factory=dict)
    key_model: dict[tuple[str, str], str | None] = field(default_factory=dict)
    keys_by_source: dict[str, set[str]] = field(default_factory=dict)

    # activity
    turns: int = 0
    user_messages: int = 0
    tool_counts: dict[str, int] = field(default_factory=dict)
    tool_errors: int = 0
    tools: dict[str, ToolStatus] = field(default_factory=dict)
    files_touched: dict[str, datetime] = field(default_factory=dict)
    events: deque = field(default_factory=lambda: deque(maxlen=EVENT_LOG_MAX))

    subagents: dict[str, SubagentStatus] = field(default_factory=dict)
    workflows: dict[str, WorkflowStatus] = field(default_factory=dict)

    # which agent wrote the transcript ("claude" | "codex")
    agent: str = "claude"
    # window the transcript itself reports (0 = unknown -> Config.window_for)
    context_window: int = 0
    auto_compact_fraction: float = AUTO_COMPACT_FRACTION

    # ---- token folding ---------------------------------------------------
    def record_usage(self, source_id: str, key: str, usage: UsageTokens,
                     model: str | None) -> None:
        """Upsert one response's usage (replaying the same key is a no-op)."""
        gkey = (source_id, key)
        self.tokens_by_key[gkey] = usage
        self.key_model[gkey] = model
        self.keys_by_source.setdefault(source_id, set()).add(key)

    def drop_usage(self, source_id: str, key: str) -> None:
        """Forget one key of a source (e.g. a superseded fallback count)."""
        gkey = (source_id, key)
        self.tokens_by_key.pop(gkey, None)
        self.key_model.pop(gkey, None)
        keys = self.keys_by_source.get(source_id)
        if keys is not None:
            keys.discard(key)
            if not keys:
                del self.keys_by_source[source_id]

    def drop_source_tokens(self, source_id: str) -> None:
        """Forget every key of a source (before it is replayed from scratch)."""
        for token_key in self.keys_by_source.pop(source_id, set()):
            gkey = (source_id, token_key)
            self.tokens_by_key.pop(gkey, None)
            self.key_model.pop(gkey, None)

    def reset_main(self) -> None:
        """Clear what the main transcript accumulated, ahead of a replay."""
        self.turns = 0
        self.user_messages = 0
        self.tool_counts = {}
        self.tool_errors = 0
        self.tools = {}
        self.files_touched = {}
        self.current_context_tokens = 0
        self.prev_context_tokens = 0
        self.max_context_tokens = 0
        self.context_history = []
        self.compactions = []
        self.events.clear()

    def update_context(self, usage: UsageTokens, ts: datetime | None,
                       detect_drop: bool = True) -> None:
        """Record the latest turn's prompt size as the live context occupancy.

        With ``detect_drop`` a sharp fall is logged as a compaction; agents
        that write an explicit compaction marker pass ``False``.
        """
        ctx = usage.context_tokens
        prev = self.current_context_tokens
        if detect_drop and prev > 0 and ctx < prev * COMPACTION_DROP_FRACTION:
            self.compactions.append(CompactionEvent(at=ts, before=prev, after=ctx))
            self.push_event(ts, "compaction", f"⟳ context compacted {prev:,} → {ctx:,}")
        self.prev_context_tokens = prev
        self.current_context_tokens = ctx
        self.max_context_tokens = max(self.max_context_tokens, ctx)
        self.latest_usage = usage
        hist = self.context_history
        hist.append(ctx)
        if len(hist) > CONTEXT_HISTORY_MAX:
            del hist[: len(hist) - CONTEXT_HISTORY_MAX]

    # ---- computed views (read by the snapshot builder) -----------------
    def cumulative_tokens(self) -> UsageTokens:
        total = EMPTY_USAGE
        for usage in self.tokens_by_key.values():
            total = total + usage
        return total

    def source_tokens(self, source_id: str) -> UsageTokens:
        total = EMPTY_USAGE
        for (src, _key), usage in self.tokens_by_key.items():
            if src == source_id:
                total = total + usage
        return total

    def source_billable_totals(self) -> dict[str, int]:
        """Billable tokens per source in one pass (``source_tokens`` per agent
        would rescan every key once per sub-agent)."""
        out: dict[str, int] = {}
        for (src, _key), usage in self.tokens_by_key.items():
            out[src] = out.get(src, 0) + usage.billable_total
        return out

    def model_breakdown(self) -> dict[str, UsageTokens]:
        out: dict[str, UsageTokens] = {}
        for gkey, usage in self.tokens_by_key.items():
            model = self.key_model.get(gkey) or "unknown"
            out[model] = out.get(model, EMPTY_USAGE) + usage
        return out

    def in_flight_tools(self) -> list[ToolStatus]:
        return [t for t in self.tools.values() if not t.done]

    def current_activity(self) -> ToolStatus | None:
        live = [t for t in self.tools.values() if not t.done and t.started_at]
        if not live:
            return None
        return max(live, key=lambda t: t.started_at)

    def push_event(self, at: datetime | None, kind: str, text: str) -> None:
        self.events.append(Event(at=at, kind=kind, text=text))

    def touch_time(self, ts: datetime | None) -> None:
        if ts is None:
            return
        if self.started_at is None or ts < self.started_at:
            self.started_at = ts
        if self.last_record_at is None or ts > self.last_record_at:
            self.last_record_at = ts
