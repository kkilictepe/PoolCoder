"""Mutable live state for one monitored session.

Owned exclusively by the reader thread. The UI never touches this — it reads
immutable snapshots built from it (see ``snapshot.py``). Token totals are kept
as a per-turn map (single source of truth) and summed lazily, which keeps
replay-after-rotation correct without running counters to unwind.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from datetime import datetime

from .models import EMPTY_USAGE, ToolUse, UsageTokens

EVENT_LOG_MAX = 400
CONTEXT_HISTORY_MAX = 500


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
