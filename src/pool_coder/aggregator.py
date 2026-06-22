"""Fold the record stream into ``SessionState``.

``apply(source_id, record)`` is the single entry point. ``source_id`` routes
the update:

    "main"             -> the primary session transcript
    "agent:<id>"       -> a top-level subagent transcript
    "wfagent:<run>"    -> a workflow's agent transcripts (tokens -> workflow)
    "wfjournal:<run>"  -> a workflow's journal (started/result events)

Keyed facts use dict upserts so replay-after-rotation is a no-op; the only
summed quantity (tokens) lives in a per-turn map that ``reset_source`` clears
before a replay.
"""

from __future__ import annotations

import re

from .config import COMPACTION_DROP_FRACTION, Config
from .parser import Record, content_preview
from .state import (
    CompactionEvent,
    SessionState,
    SubagentStatus,
    ToolStatus,
    WorkflowPhase,
    WorkflowStatus,
)

_TAG_BLOCK = re.compile(r"<(ide_selection|system-reminder|command-[\w-]+|local-command-[\w-]+)\b.*?</\1>", re.DOTALL)
_ANY_TAG = re.compile(r"<[^>]+>")


def clean_prompt(text: str | None, limit: int = 200) -> str | None:
    if not text:
        return None
    text = _TAG_BLOCK.sub(" ", text)
    text = _ANY_TAG.sub(" ", text)
    text = " ".join(text.split())
    if not text:
        return None
    return text[:limit]


def _active_todo(tool_input: dict) -> str:
    """The in-progress item from a TodoWrite call — i.e. what Claude is doing now."""
    todos = tool_input.get("todos")
    if isinstance(todos, list):
        for todo in todos:
            if isinstance(todo, dict) and todo.get("status") == "in_progress":
                return str(todo.get("activeForm") or todo.get("content") or "")
    return ""


def _scope(source_id: str) -> tuple[str, str]:
    """Return ``(kind, ident)`` for a source id."""
    prefix, _, ident = source_id.partition(":")
    return prefix, ident


class Aggregator:
    def __init__(self, state: SessionState, config: Config | None = None):
        self.state = state
        self.config = config or Config()
        self._parent_tool_to_agent: dict[str, str] = {}

    # -- registration (side-data from discovery) -------------------------
    def register_subagent(self, agent_id: str, agent_type: str, description: str,
                          parent_tool_use_id: str) -> None:
        sub = self.state.subagents.get(agent_id)
        if sub is None:
            sub = SubagentStatus(agent_id=agent_id)
            self.state.subagents[agent_id] = sub
            self.state.push_event(None, "agent", f"⊕ subagent {agent_type or '?'}: {description[:60]}")
        sub.agent_type = agent_type or sub.agent_type
        sub.description = description or sub.description
        if parent_tool_use_id:
            sub.parent_tool_use_id = parent_tool_use_id
            self._parent_tool_to_agent[parent_tool_use_id] = agent_id
            # If the parent tool already completed, the subagent is finished.
            parent = self.state.tools.get(parent_tool_use_id)
            if parent is not None and parent.done:
                sub.finished = True

    def register_workflow(self, run_id: str, name: str, description: str,
                         phases: list[tuple[str, str]]) -> None:
        wf = self.state.workflows.get(run_id)
        if wf is None:
            wf = WorkflowStatus(run_id=run_id)
            self.state.workflows[run_id] = wf
            self.state.push_event(None, "workflow", f"⊞ workflow {name or run_id}")
        wf.name = name or wf.name
        wf.description = description or wf.description
        if phases:
            wf.phases = [WorkflowPhase(title=t, detail=d) for t, d in phases]

    # -- main dispatch ---------------------------------------------------
    def apply(self, source_id: str, record: Record) -> None:
        self.state.touch_time(record.timestamp)
        kind, ident = _scope(source_id)

        if kind == "wfjournal":
            self._apply_journal(ident, record)
            return

        if record.type == "assistant":
            self._apply_assistant(source_id, kind, ident, record)
        elif record.type == "user":
            self._apply_user(source_id, kind, ident, record)
        elif record.type == "mode" and kind == "main":
            self.state.mode = record.raw.get("mode") or record.raw.get("name") or self.state.mode
        elif record.type == "ai-title" and kind == "main":
            self.state.title = (
                record.raw.get("title") or record.raw.get("content") or self.state.title
            )

    # -- assistant -------------------------------------------------------
    def _apply_assistant(self, source_id: str, kind: str, ident: str, record: Record) -> None:
        ts = record.timestamp
        usage = record.usage()
        if usage is not None:
            gkey = (source_id, record.token_key)
            self.state.tokens_by_key[gkey] = usage
            self.state.key_model[gkey] = record.model
            self.state.keys_by_source.setdefault(source_id, set()).add(record.token_key)

        if kind == "main":
            self.state.turns += 1
            if record.model:
                self.state.model = record.model
            if record.cwd:
                self.state.cwd = record.cwd
            if record.git_branch:
                self.state.git_branch = record.git_branch
            if record.version:
                self.state.version = record.version
            if usage is not None:
                self._update_context(usage, ts)
            # surface what Claude is reasoning about / saying this turn
            thinking = record.thinking_len()
            if thinking:
                self.state.push_event(ts, "thinking", f"✎ thinking… ({thinking:,} chars)")
            said = " ".join(record.assistant_text().split())
            if said:
                self.state.push_event(ts, "text", f"↳ {said[:160]}")
            for tu in record.tool_uses():
                self.state.tools[tu.id] = ToolStatus(tool=tu, source_id=source_id, started_at=ts)
                self.state.tool_counts[tu.name] = self.state.tool_counts.get(tu.name, 0) + 1
                fp = tu.file_path
                if fp:
                    self.state.files_touched[fp] = ts
                label = _active_todo(tu.input) if tu.name == "TodoWrite" else (tu.target or "")
                self.state.push_event(ts, "tool", f"→ {tu.name} {label}".rstrip())

        elif kind == "agent":
            sub = self.state.subagents.setdefault(ident, SubagentStatus(agent_id=ident))
            sub.turns += 1
            if sub.started_at is None:
                sub.started_at = ts
            sub.last_activity = ts
            for tu in record.tool_uses():
                sub.open_tools.add(tu.id)
                sub.last_tool = tu.name
        # wfagent: tokens already folded above; nothing else tracked per-turn.

    def _update_context(self, usage, ts) -> None:
        ctx = usage.context_tokens
        prev = self.state.current_context_tokens
        if prev > 0 and ctx < prev * COMPACTION_DROP_FRACTION:
            self.state.compactions.append(CompactionEvent(at=ts, before=prev, after=ctx))
            self.state.push_event(ts, "compaction", f"⟳ context compacted {prev:,} → {ctx:,}")
        self.state.prev_context_tokens = prev
        self.state.current_context_tokens = ctx
        self.state.max_context_tokens = max(self.state.max_context_tokens, ctx)
        self.state.latest_usage = usage
        hist = self.state.context_history
        hist.append(ctx)
        if len(hist) > 600:
            del hist[: len(hist) - 600]

    # -- user / tool results ---------------------------------------------
    def _apply_user(self, source_id: str, kind: str, ident: str, record: Record) -> None:
        ts = record.timestamp
        results = record.tool_results()

        if results:
            for tid, is_error, preview in results:
                if kind == "main":
                    tool = self.state.tools.get(tid)
                    if tool is not None:
                        tool.done = True
                        tool.is_error = is_error
                        tool.ended_at = ts
                        tool.result_preview = preview
                        if is_error:
                            self.state.tool_errors += 1
                        mark = "✗" if is_error else "✓"
                        self.state.push_event(ts, "result", f"{mark} {tool.tool.name}")
                    agent_id = self._parent_tool_to_agent.get(tid)
                    if agent_id and agent_id in self.state.subagents:
                        sub = self.state.subagents[agent_id]
                        sub.finished = True
                        sub.last_activity = ts
                        self.state.push_event(ts, "agent", f"⊙ subagent done: {sub.agent_type}")
                elif kind == "agent":
                    sub = self.state.subagents.get(ident)
                    if sub is not None:
                        sub.open_tools.discard(tid)
                        sub.last_activity = ts
            return

        # real user prompt (string content, no tool_result blocks)
        if kind == "main":
            prompt = clean_prompt(record.user_text())
            if prompt:
                self.state.last_prompt = prompt
                self.state.user_messages += 1
                self.state.push_event(ts, "prompt", f"» {prompt[:80]}")

    # -- workflow journal ------------------------------------------------
    def _apply_journal(self, run_id: str, record: Record) -> None:
        wf = self.state.workflows.setdefault(run_id, WorkflowStatus(run_id=run_id))
        key = record.raw.get("key") or record.raw.get("agentId")
        if not key:
            return
        if record.type == "started":
            wf.started_keys.add(key)
        elif record.type == "result":
            wf.result_keys.add(key)

    # -- reset (rotation/truncation) -------------------------------------
    def reset_source(self, source_id: str) -> None:
        for token_key in self.state.keys_by_source.pop(source_id, set()):
            gkey = (source_id, token_key)
            self.state.tokens_by_key.pop(gkey, None)
            self.state.key_model.pop(gkey, None)

        kind, ident = _scope(source_id)
        if kind == "main":
            s = self.state
            s.turns = 0
            s.user_messages = 0
            s.tool_counts = {}
            s.tool_errors = 0
            s.tools = {}
            s.files_touched = {}
            s.current_context_tokens = 0
            s.prev_context_tokens = 0
            s.max_context_tokens = 0
            s.context_history = []
            s.compactions = []
            s.events.clear()
        elif kind == "agent":
            sub = self.state.subagents.get(ident)
            if sub is not None:
                sub.turns = 0
                sub.open_tools = set()
                sub.finished = False
        elif kind == "wfjournal":
            wf = self.state.workflows.get(ident)
            if wf is not None:
                wf.started_keys = set()
                wf.result_keys = set()
