"""Fold Codex rollout records into ``SessionState``.

``CodexAggregator`` is the Codex counterpart of the Claude ``Aggregator``: the
same ``FoldTarget`` surface and the same shared state mutators, but Codex's
own record types and rules (it is not a subclass). ``source_id`` routes each
record:

    "main"          -> the monitored thread's rollout
    "agent:<tid>"   -> a sub-agent / guardian thread's rollout (from discovery)

Tokens come from ``token_usage_record`` (one per model response, keyed by
``response_id``). Older rollouts only carry cumulative ``token_count`` totals;
those are folded as component-wise deltas until the source's first
``token_usage_record`` replaces them. Forked threads start with copies of
their parent's records, so anything naming another thread (``session_meta``,
``token_usage_record``, settings, items) is ignored, their copied
cumulative totals are never used, and the copied history itself (turns older
than the thread, and anything before its own first turn) is skipped.

Every counted fact is keyed (responses, prompts by ``client_id``, tools by
``call_id``, sub-agent activity, commands, file changes, compactions), so a
record applied twice changes no count (only narrative events such as ``↳``
may repeat), and ``reset_source`` + replay rebuilds the same state (a "⊕"
that only discovery produced, such as a guardian's, goes back to the top of
the event log, as after the initial catch-up). Where the
main thread and a child both speak about the child (is it running?), the newer
record decides, never the file that happened to be read last.
"""

from __future__ import annotations

import json
import ntpath
import posixpath
from dataclasses import dataclass, field
from datetime import datetime, timezone
from urllib.parse import unquote

from ..config import CODEX_AUTO_COMPACT_FRACTION, Config
from ..models import ToolUse, UsageTokens
from ..parser import content_preview
from ..state import (
    CompactionEvent,
    SessionState,
    SubagentStatus,
    ToolStatus,
    WorkflowPhase,
    WorkflowStatus,
)
from .parser import (
    CodexRecord,
    clean_codex_prompt,
    describe_call,
    exec_inner_calls,
    output_failed,
    patch_files,
    shell_label,
    usage_from,
)
from .paths import _meta_from_record

# Components of a cumulative ``token_count`` total, in ``usage`` field names.
_TOTAL_FIELDS = (
    "input_tokens",
    "cached_input_tokens",
    "cache_write_input_tokens",
    "output_tokens",
    "reasoning_output_tokens",
)
# ToolUse input key per display name (``ToolUse.target`` reads it back).
_TARGET_KEY = {"shell": "command", "apply_patch": "path", "view_image": "path",
               "web_search": "query"}
_CALLS = frozenset({"custom_tool_call", "function_call", "local_shell_call"})
_OUTPUTS = frozenset({"custom_tool_call_output", "function_call_output",
                      "local_shell_call_output"})
_ACTIVITY_KINDS = frozenset({"started", "completed", "interrupted"})
# sandbox policy type / permission profile id -> short name for the mode line
_SANDBOX = {
    "danger-full-access": "full-access", ":danger-full-access": "full-access",
    "workspace-write": "workspace", ":workspace": "workspace",
    "read-only": "read-only", ":read-only": "read-only",
}
_EPOCH = datetime.fromtimestamp(0, tz=timezone.utc)
_HEX = frozenset("0123456789abcdef")
# Records that carry their own thread filter and are never "copied" noise.
_UNFILTERED = frozenset({"session_meta", "token_usage_record"})


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def _str(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def _int(value: object) -> int:
    """A non-negative count (missing / garbage -> 0)."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0
    try:
        return max(int(value), 0)
    except (ValueError, OverflowError):
        return 0


def _uuid7_ms(value: object) -> str | None:
    """The 48-bit millisecond prefix of a UUIDv7 as 12 hex digits (they sort
    like the times they encode), or None for any other id."""
    if (not isinstance(value, str) or len(value) != 36 or value[8] != "-"
            or value[13] != "-" or value[14] != "7"):
        return None
    digits = (value[:8] + value[9:13]).lower()
    return digits if set(digits) <= _HEX else None


def _scope(source_id: str) -> tuple[str, str]:
    prefix, _, ident = source_id.partition(":")
    return prefix, ident


def _texts(blocks: object) -> list[str]:
    """The ``text`` of each block of a content list."""
    if not isinstance(blocks, list):
        return []
    return [b["text"] for b in blocks if isinstance(b, dict) and isinstance(b.get("text"), str)]


def _squash(texts: list[str]) -> str:
    return " ".join(" ".join(texts).split())


def _last_segment(path: str | None) -> str:
    """``/root/dependency_audit`` -> ``dependency_audit``."""
    return path.rstrip("/").rsplit("/", 1)[-1] if path else ""


def _first_line(js: str) -> str:
    for raw in js.splitlines():
        text = " ".join(raw.split())
        if text:
            return text[:200]
    return ""


def _sandbox_name(settings: dict) -> str:
    policy = settings.get("sandbox_policy")
    kind = policy.get("type") if isinstance(policy, dict) else policy
    if not _str(kind):
        profile = settings.get("active_permission_profile")
        kind = profile.get("id") if isinstance(profile, dict) else None
    if _str(kind):
        return _SANDBOX.get(kind, kind.lstrip(":"))
    perm = settings.get("permission_profile")
    if isinstance(perm, dict) and perm.get("type") == "disabled":
        return "full-access"
    return ""


def _mode_label(settings: dict) -> str:
    """``plan · ultra · full-access`` from a turn_context / thread_settings payload:
    collaboration mode (unless default), reasoning effort, short sandbox name."""
    parts: list[str] = []
    collab = settings.get("collaboration_mode")
    collab = collab if isinstance(collab, dict) else {}
    cmode = _str(collab.get("mode"))
    if cmode and cmode != "default":
        parts.append(cmode)
    cset = collab.get("settings")
    effort = (_str(settings.get("effort")) or _str(settings.get("reasoning_effort"))
              or (_str(cset.get("reasoning_effort")) if isinstance(cset, dict) else None))
    if effort:
        parts.append(effort)
    sandbox = _sandbox_name(settings)
    if sandbox:
        parts.append(sandbox)
    return " · ".join(parts)


def _local_path(value: str) -> str:
    """``file:///C:/Git/x`` -> ``C:/Git/x`` (a CommandExecution ``cwd``); other
    strings unchanged."""
    if value[:7].lower() != "file://":
        return value
    rest = unquote(value[7:])
    if rest[:1] == "/" and rest[2:3] == ":" and rest[1:2].isalpha():
        rest = rest[1:]  # /C:/... -> C:/...
    return rest


def _file_id(path: str, base: str | None) -> tuple[str, str]:
    """``(key, display)`` of a touched file.

    Codex names one file relative to the workdir (patch headers, command
    reads), with forward slashes, and absolute with backslashes (FileChange
    keys). A relative path is joined to ``base``, the result normalised (a
    drive letter upper-cased), and the key case-folded on Windows. Without a
    base a relative path is kept as written.
    """
    path = _local_path(path)
    base = _local_path(base) if base else None
    win = any("\\" in p or p[1:2] == ":" for p in (path, base) if p)
    mod = ntpath if win else posixpath
    if not mod.isabs(path):
        if not base:
            return path, path
        path = mod.join(base, path)
    display = mod.normpath(path)
    if win and display[1:2] == ":":
        display = display[0].upper() + display[1:]
    return mod.normcase(display), display


def _patch_arg_files(args: object) -> list[str]:
    """Paths of a legacy ``apply_patch`` call: ``{"input": patch}`` (dict or
    JSON text) or a freeform raw patch."""
    if isinstance(args, str):
        try:
            args = json.loads(args)
        except (ValueError, RecursionError):
            return patch_files(args)
        if isinstance(args, str):
            return patch_files(args)
    if isinstance(args, dict):
        patch = args.get("input") or args.get("patch")
        return patch_files(patch) if isinstance(patch, str) else []
    return []


def _call_info(p: dict) -> tuple[str, str, list[str], list[str]]:
    """``(display, label, names to count, files touched)`` of a tool-call payload."""
    ptype = p.get("type")
    name = _str(p.get("name")) or ""
    if ptype == "local_shell_call":
        action = p.get("action")
        argv = action.get("command") if isinstance(action, dict) else None
        return "shell", shell_label(argv), ["shell"], []
    if ptype == "custom_tool_call" and name == "exec":
        js = p.get("input") if isinstance(p.get("input"), str) else ""
        calls = exec_inner_calls(js)
        if not calls:
            return "exec", _first_line(js), ["exec"], []
        display, label = calls[0]
        if len(calls) > 1:
            label = f"{label} (+{len(calls) - 1})".strip()
        # every file of every patch in the script (the label shows the first)
        files = patch_files(js) if any(n == "apply_patch" for n, _ in calls) else []
        return display, label, [n for n, _ in calls], files
    args = p.get("arguments") if ptype == "function_call" else p.get("input")
    display, label = describe_call(name, args)
    display = display or name or str(ptype or "tool")
    files = _patch_arg_files(args) if display == "apply_patch" else []
    return display, label, [display], files


@dataclass
class _Tokens:
    """Per-source token bookkeeping."""

    primary: bool = False                       # a token_usage_record was seen
    forked: bool = False                        # own session_meta has forked_from_id:
                                                # token_count totals never count
    copying: bool = False                       # forked: still in the parent's
                                                # copied history (see _copied)
    last_total: tuple[int, ...] | None = None   # previous token_count total
    fallback_keys: set[str] = field(default_factory=set)


# ---------------------------------------------------------------------------
# the fold
# ---------------------------------------------------------------------------
class CodexAggregator:
    def __init__(self, state: SessionState, config: Config | None = None):
        self.state = state
        self.config = config or Config()
        state.agent = "codex"
        state.auto_compact_fraction = CODEX_AUTO_COMPACT_FRACTION
        self._tokens: dict[str, _Tokens] = {}
        self._child_models: dict[str, str] = {}       # tid -> latest turn model
        self._child_meta_models: dict[str, str] = {}  # tid -> session_meta model
        self._child_tasks: set[str] = set()           # children that started a task
        # A child's running flag has two writers: its own task_started and the
        # main thread's completed/interrupted activity. Main and child files are
        # drained in chunks (and replayed after a reset) in no fixed order, so
        # the newer of the two decides, never the one applied last.
        self._own_start: dict[str, datetime] = {}  # tid -> latest own task_started
        self._root_done: dict[str, datetime] = {}  # tid -> latest main done activity
        # Sub-agents discovery registered (in order). Discovery names each only
        # once, and guardians / grandchildren have no activity in the main file,
        # so a main reset re-announces these itself.
        self._registered: dict[str, None] = {}
        # main-source dedupe, cleared by reset_source("main")
        self._prompts: set[str] = set()
        self._activities: set[tuple[str, str, str]] = set()
        self._commands: set[str] = set()
        self._changes: set[str] = set()
        self._file_keys: dict[str, str] = {}  # file key -> its files_touched spelling
        self._compactions: set[str] = set()
        self._announced: set[str] = set()   # sub-agents with a "⊕" in the event log
        # First "⊕" text per sub-agent, kept across resets so a replay rebuilds
        # the same line even after discovery enriched the description.
        self._announce_texts: dict[str, str] = {}
        self._pending: CompactionEvent | None = None  # waits for its "after"

    # -- registration (side-data from discovery) -------------------------
    def register_subagent(self, agent_id: str, agent_type: str, description: str,
                          parent_tool_use_id: str) -> None:
        sub = self._subagent(agent_id)
        sub.agent_type = agent_type or sub.agent_type
        sub.description = description or sub.description
        if parent_tool_use_id:
            sub.parent_tool_use_id = parent_tool_use_id
        # Only ids discovery announced first are re-announced on a main reset;
        # one the main file's "started" activity announced (a child spawned
        # while watching) gets its timestamped "⊕" back from the replay.
        if agent_id not in self._announced:
            self._registered[agent_id] = None
        self._announce(sub, None)

    def register_workflow(self, run_id: str, name: str, description: str,
                          phases: list[tuple[str, str]]) -> None:
        # Codex has no workflows; stored like Claude's so the surface matches.
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
    def apply(self, source_id: str, record: CodexRecord) -> None:
        st = self.state
        st.touch_time(record.timestamp)
        kind, ident = _scope(source_id)
        sub: SubagentStatus | None = None
        if kind == "main":
            if record.type == "session_meta" and not st.session_id:
                st.session_id = _str(record.payload.get("id")) or ""  # line 1 names the thread
            own = st.session_id
        elif kind == "agent" and ident:
            own = ident
            sub = self._child(ident, record.timestamp)
        else:
            return

        rtype = record.type
        if rtype not in _UNFILTERED and self._copied(source_id, own, record):
            return
        if rtype == "token_usage_record":
            self._on_usage(source_id, own, sub, record)
        elif rtype == "event_msg":
            self._on_event(source_id, own, sub, record)
        elif rtype == "response_item":
            self._on_response(source_id, sub, record)
        elif rtype == "turn_context":
            self._on_settings(sub, record.payload)
        elif rtype == "session_meta":
            self._on_meta(source_id, own, sub, record)
        elif rtype == "compacted" and sub is None:
            self._on_compacted(record)

    def _copied(self, source_id: str, own: str, record: CodexRecord) -> bool:
        """A record of the parent's history copied into a forked thread's file.

        A forked rollout starts with a re-stamped copy of its parent's history
        (prompts, ``compacted`` markers, file changes, sub-agent activity, old
        turns), and Codex keeps interleaving copied items after the thread's own
        first turn. Turn ids are UUIDv7 like thread ids, so a turn older than
        the thread is the parent's (response items carry theirs in
        ``internal_chat_message_metadata_passthrough``). Only a UUIDv7 turn at
        or after the thread's birth, or the thread's own first response, ends
        the copied head: copies are also re-tagged ``rollout-N``, and a turn id
        that cannot be ordered decides nothing.
        """
        track = self._tokens.get(source_id)
        if track is None or not track.forked:
            return False
        if record.type == "event_msg" and record.ptype == "token_count":
            return False  # a forked thread's totals never count; only the window is read
        born = _uuid7_ms(own)
        if born is None:
            return False
        p = record.payload
        turn_id = p.get("turn_id")
        if not (isinstance(turn_id, str) and turn_id):
            meta = p.get("internal_chat_message_metadata_passthrough")
            turn_id = meta.get("turn_id") if isinstance(meta, dict) else None
        turn = _uuid7_ms(turn_id)
        if turn is None:
            return track.copying  # no turn id, or one that cannot be ordered
        if turn < born:
            return True
        track.copying = False  # the thread's own turn
        return False

    # -- tokens ----------------------------------------------------------
    def _track(self, source_id: str) -> _Tokens:
        track = self._tokens.get(source_id)
        if track is None:
            track = self._tokens[source_id] = _Tokens()
        return track

    def _model_of(self, sub: SubagentStatus | None) -> str | None:
        if sub is None:
            return self.state.model
        return self._child_models.get(sub.agent_id) or self._child_meta_models.get(sub.agent_id)

    def _sync_turns(self, source_id: str, sub: SubagentStatus | None) -> None:
        """Turns = distinct model responses (token keys) of the source."""
        n = len(self.state.keys_by_source.get(source_id, ()))
        if sub is None:
            self.state.turns = n
        else:
            sub.turns = n

    def _on_usage(self, source_id: str, own: str, sub: SubagentStatus | None,
                  record: CodexRecord) -> None:
        p = record.payload
        if not own or p.get("thread_id") != own:
            return  # another thread's response (copied into a forked child)
        usage = usage_from(p.get("usage"))
        if usage is None:
            return
        st = self.state
        track = self._track(source_id)
        track.copying = False  # an own response: the copied history is over
        if not track.primary:
            # per-response records supersede the cumulative fallback
            track.primary = True
            for key in track.fallback_keys:
                st.drop_usage(source_id, key)
            track.fallback_keys.clear()
        key = _str(p.get("response_id")) or f"@{record.raw.get('timestamp')}"
        if key not in st.keys_by_source.get(source_id, ()):
            st.record_usage(source_id, key, usage, self._model_of(sub) or "gpt")
            if sub is None and usage.context_tokens > 0:
                self._set_context(usage, record.timestamp)
        self._sync_turns(source_id, sub)

    def _on_token_count(self, source_id: str, sub: SubagentStatus | None,
                        record: CodexRecord) -> None:
        info = record.payload.get("info")
        if not isinstance(info, dict):
            return  # rate limits only
        track = self._track(source_id)
        if sub is None:
            self._set_window(info.get("model_context_window"))
            if self._pending is not None and not track.forked:
                self._compacted_estimate(info.get("last_token_usage"))
        if track.primary or track.forked:
            # A forked thread's totals include its parent's, and its file starts
            # with a copy of the parent's whole token_count history (hundreds of
            # totals, written in one burst): every total is a baseline there,
            # and only the thread's own token_usage_records count.
            return
        total = info.get("total_token_usage")
        if not isinstance(total, dict):
            return
        now = tuple(_int(total.get(f)) for f in _TOTAL_FIELDS)
        prev = track.last_total
        track.last_total = now
        if prev is None:
            delta = now
        elif any(n < q for n, q in zip(now, prev)):
            return  # the totals went backwards: re-baseline
        else:
            delta = tuple(n - q for n, q in zip(now, prev))
        if not any(delta):
            return  # a repeated total
        st = self.state
        key = f"tc:{_int(total.get('total_tokens')) or sum(now)}"
        if key in st.keys_by_source.get(source_id, ()):
            return
        usage = usage_from(dict(zip(_TOTAL_FIELDS, delta)))
        st.record_usage(source_id, key, usage, self._model_of(sub) or "gpt")
        track.fallback_keys.add(key)
        self._sync_turns(source_id, sub)
        if sub is None:
            last = usage_from(info.get("last_token_usage"))
            if last is not None and last.context_tokens > 0:
                self._set_context(last, record.timestamp)

    def _set_window(self, value: object) -> None:
        window = _int(value)
        if window > 0:
            self.state.context_window = window

    def _set_context(self, usage: UsageTokens, ts: datetime | None) -> None:
        # Codex writes an explicit ``compacted`` marker: no drop detection.
        self.state.update_context(usage, ts, detect_drop=False)
        if self._pending is not None:
            self._pending.after = usage.context_tokens
            self._pending = None

    def _on_compacted(self, record: CodexRecord) -> None:
        stamp = record.raw.get("timestamp")
        if isinstance(stamp, str):
            if stamp in self._compactions:
                return
            self._compactions.add(stamp)
        st = self.state
        before = st.current_context_tokens
        event = CompactionEvent(at=record.timestamp, before=before, after=0)
        st.compactions.append(event)
        st.push_event(record.timestamp, "compaction", f"⟳ context compacted (was {before:,})")
        self._pending = event
        # The old context is gone. After a manual /compact the thread may sit
        # idle until the next prompt, so do not keep showing its size: Codex's
        # estimate (the next token_count) or the next response fills it in.
        st.prev_context_tokens = before
        st.current_context_tokens = 0

    def _compacted_estimate(self, last: object) -> None:
        """Right after ``compacted`` Codex writes a token_count whose last usage
        is empty and whose ``total_tokens`` estimates the compacted context
        (its own indicator reads it). Shown until the next response; the
        compaction's ``after`` still comes from that response."""
        if not isinstance(last, dict):
            return
        if _int(last.get("input_tokens")) or _int(last.get("output_tokens")):
            return  # a real response's usage
        estimate = _int(last.get("total_tokens"))
        if estimate > 0:
            self.state.current_context_tokens = estimate

    # -- thread identity & settings ----------------------------------------
    def _on_meta(self, source_id: str, own: str, sub: SubagentStatus | None,
                 record: CodexRecord) -> None:
        meta = _meta_from_record(record.raw)
        if meta is None or meta.thread_id != own:
            return  # a forked thread's copy of its parent's meta
        track = self._track(source_id)
        forked = bool(meta.forked_from_id)
        if forked and not track.forked:
            track.copying = True  # the parent's copied history follows
        track.forked = forked
        if sub is None:
            st = self.state
            st.cwd = meta.cwd or st.cwd
            st.git_branch = meta.git_branch or st.git_branch
            st.version = meta.cli_version or st.version
            if st.model is None and meta.model:
                st.model = meta.model
            return
        if meta.model:
            self._child_meta_models[sub.agent_id] = meta.model
        if sub.agent_id not in self._child_tasks:
            # idle until its first task: guardian threads are often created
            # and never used (meta + settings only) and must not show as running
            sub.finished = True
        if not sub.agent_type:
            sub.agent_type = ("guardian" if meta.is_guardian else
                              _last_segment(meta.agent_path) or meta.thread_source or "subagent")
        if not sub.description:
            sub.description = " · ".join(x for x in (meta.agent_nickname, meta.agent_path) if x)

    def _on_settings(self, sub: SubagentStatus | None, settings: object) -> None:
        if not isinstance(settings, dict):
            return
        model = _str(settings.get("model"))
        if sub is not None:
            if model:
                self._child_models[sub.agent_id] = model
            return
        st = self.state
        if model:
            st.model = model
        st.cwd = _str(settings.get("cwd")) or st.cwd
        st.mode = _mode_label(settings) or st.mode

    # -- event_msg -------------------------------------------------------
    def _on_event(self, source_id: str, own: str, sub: SubagentStatus | None,
                  record: CodexRecord) -> None:
        p = record.payload
        tid = _str(p.get("thread_id"))
        if tid and own and tid != own:
            return  # another thread's event (copied into a forked child)
        ptype = record.ptype
        if ptype == "token_count":
            self._on_token_count(source_id, sub, record)
        elif ptype == "task_started":
            if sub is None:
                self._set_window(p.get("model_context_window"))
            else:
                self._child_tasks.add(sub.agent_id)
                self._on_child_start(sub, record.timestamp)
        elif ptype in ("task_complete", "turn_aborted"):
            self._on_turn_end(sub, record, aborted=ptype == "turn_aborted")
        elif ptype == "thread_settings_applied":
            self._on_settings(sub, p.get("thread_settings"))
        elif sub is not None:
            return  # the rest is tracked for the main thread only
        elif ptype == "user_message":
            self._on_prompt(record, _str(p.get("message")), _str(p.get("client_id")), None)
        elif ptype == "sub_agent_activity":
            self._on_activity(own, record, p.get("event_id"), p)
        elif ptype == "patch_apply_end":
            call_id = _str(p.get("call_id"))
            self._on_changes(f"pae:{call_id}" if call_id else None, p.get("changes"),
                             record.timestamp)
        elif ptype == "item_completed":
            self._on_item(own, record)

    def _on_item(self, own: str, record: CodexRecord) -> None:
        item = record.item
        itype = record.item_type
        if itype == "UserMessage":
            text = "\n".join(_texts(item.get("content")))
            self._on_prompt(record, text, _str(item.get("client_id")), _str(item.get("id")))
        elif itype == "SubAgentActivity":
            self._on_activity(own, record, item.get("id"), item)
        elif itype == "CommandExecution":
            self._on_command(record, item)
        elif itype == "FileChange":
            item_id = _str(item.get("id"))
            self._on_changes(f"fc:{item_id}" if item_id else None, item.get("changes"),
                             record.timestamp)
        # AgentMessage / Reasoning / ContextCompaction / ...: duplicates of
        # response items or of ``compacted``; ignored.

    def _on_prompt(self, record: CodexRecord, text: str | None, client_id: str | None,
                   item_id: str | None) -> None:
        if client_id:
            key = f"client:{client_id}"
        elif item_id:
            key = f"item:{item_id}"
        else:
            key = f"at:{record.raw.get('timestamp')}:{text or ''}"
        if key in self._prompts:
            return  # the same prompt in the other format
        self._prompts.add(key)
        st = self.state
        st.user_messages += 1
        prompt = clean_codex_prompt(text)
        if prompt:  # None for e.g. attachments only
            st.last_prompt = prompt
            st.push_event(record.timestamp, "prompt", f"» {prompt[:80]}")

    def _on_turn_end(self, sub: SubagentStatus | None, record: CodexRecord,
                     aborted: bool) -> None:
        if sub is not None:
            sub.finished = True
            sub.open_tools.clear()
            return
        ts = record.timestamp
        st = self.state
        for tool in st.tools.values():
            if not tool.done:  # nothing stays "in flight" past its turn
                tool.done = True
                tool.ended_at = ts
        if aborted:
            reason = _str(record.payload.get("reason")) or "?"
            st.push_event(ts, "result", f"⊘ turn aborted ({reason})")

    def _on_activity(self, own: str, record: CodexRecord, call_id: object,
                     fields: dict) -> None:
        tid = _str(fields.get("agent_thread_id"))
        kind = _str(fields.get("kind"))
        if not tid or tid == own or kind not in _ACTIVITY_KINDS:
            return  # "interacted" etc. carry nothing we show
        key = (call_id if isinstance(call_id, str) else "", kind, tid)
        if key in self._activities:
            return  # the same activity in the other format
        self._activities.add(key)
        ts = record.timestamp
        path = _str(fields.get("agent_path"))
        sub = self._subagent(tid)
        # fill only what discovery did not already name
        sub.agent_type = sub.agent_type or _last_segment(path)
        sub.description = sub.description or path or ""
        if kind == "started":
            self._announce(sub, ts)
            return
        self._on_root_done(sub, ts)
        if ts is not None and (sub.last_activity is None or ts > sub.last_activity):
            sub.last_activity = ts
        label = sub.agent_type or "?"
        if kind == "completed":
            self.state.push_event(ts, "agent", f"⊙ subagent done: {label}")
        else:
            self.state.push_event(ts, "agent", f"⊘ subagent interrupted: {label}")

    def _on_command(self, record: CodexRecord, item: dict) -> None:
        item_id = _str(item.get("id"))
        if item_id:
            if item_id in self._commands:
                return
            self._commands.add(item_id)
        ts = record.timestamp
        st = self.state
        code = item.get("exit_code")
        nonzero = isinstance(code, int) and not isinstance(code, bool) and code != 0
        if item.get("status") == "failed" or nonzero:
            st.tool_errors += 1
            st.push_event(ts, "result", f"✗ shell {shell_label(item.get('command'))[:80]}")
        parsed = item.get("parsed_cmd")
        base = _str(item.get("cwd")) or st.cwd  # read paths are relative to the command
        for entry in parsed if isinstance(parsed, list) else ():
            if isinstance(entry, dict) and entry.get("type") == "read":
                path = _str(entry.get("path"))
                if path:
                    self._touch(path, ts, base)
                elif _str(entry.get("name")):
                    self._touch(entry["name"], ts)  # a bare name: never resolved

    def _on_changes(self, key: str | None, changes: object, ts: datetime | None) -> None:
        if not isinstance(changes, dict):
            return
        if key is not None:
            if key in self._changes:
                return
            self._changes.add(key)
        for path in changes:
            if isinstance(path, str) and path:
                self._touch(path, ts, self.state.cwd)

    def _touch(self, path: str, ts: datetime | None, base: str | None = None) -> None:
        key, display = _file_id(path, base)
        display = self._file_keys.setdefault(key, display)  # the first spelling stays
        # the snapshot sorts by time, so never store None
        self.state.files_touched[display] = ts or self.state.last_record_at or _EPOCH

    # -- response_item -----------------------------------------------------
    def _on_response(self, source_id: str, sub: SubagentStatus | None,
                     record: CodexRecord) -> None:
        p = record.payload
        ptype = record.ptype
        if ptype in _CALLS:
            self._on_call(source_id, sub, record)
        elif ptype in _OUTPUTS:
            self._on_output(sub, record)
        elif sub is not None:
            return
        elif ptype == "message":
            if p.get("role") == "assistant":
                said = _squash(_texts(p.get("content")))
                if said:
                    self.state.push_event(record.timestamp, "text", f"↳ {said[:160]}")
        elif ptype == "reasoning":
            summary = _squash(_texts(p.get("summary")))
            if summary:  # otherwise the reasoning is encrypted
                self.state.push_event(record.timestamp, "thinking", f"✎ {summary[:160]}")
        # agent_message (inter-agent, encrypted), compaction, ...: ignored

    def _on_call(self, source_id: str, sub: SubagentStatus | None,
                 record: CodexRecord) -> None:
        p = record.payload
        call_id = (_str(p.get("call_id")) or _str(p.get("id"))
                   or f"@{record.raw.get('timestamp')}")
        display, label, names, files = _call_info(p)
        ts = record.timestamp
        if sub is not None:
            sub.open_tools.add(call_id)
            sub.last_tool = display
            return
        st = self.state
        if call_id in st.tools:
            return  # a duplicate or replayed call: counted once, state kept
        key = _TARGET_KEY.get(display, "description")
        st.tools[call_id] = ToolStatus(
            tool=ToolUse(id=call_id, name=display, input={key: label}),
            source_id=source_id,
            started_at=ts,
        )
        for name in names:
            st.tool_counts[name] = st.tool_counts.get(name, 0) + 1
        for path in files:
            self._touch(path, ts, st.cwd)  # patch paths are relative to the session
        st.push_event(ts, "tool", f"→ {display} {label}".rstrip())

    def _on_output(self, sub: SubagentStatus | None, record: CodexRecord) -> None:
        p = record.payload
        call_id = _str(p.get("call_id"))
        if not call_id:
            return
        if sub is not None:
            sub.open_tools.discard(call_id)
            return
        tool = self.state.tools.get(call_id)
        if tool is None or tool.done:
            return  # unknown call, or a duplicated output
        output = p.get("output")
        failed = output_failed(output)
        tool.done = True
        tool.is_error = failed
        tool.ended_at = record.timestamp
        tool.result_preview = content_preview(output)
        if failed:
            self.state.tool_errors += 1
        self.state.push_event(record.timestamp, "result",
                              f"{'✗' if failed else '✓'} {tool.tool.name}")

    # -- sub-agents --------------------------------------------------------
    def _subagent(self, agent_id: str) -> SubagentStatus:
        sub = self.state.subagents.get(agent_id)
        if sub is None:
            sub = self.state.subagents[agent_id] = SubagentStatus(agent_id=agent_id)
        return sub

    def _child(self, tid: str, ts: datetime | None) -> SubagentStatus:
        sub = self._subagent(tid)
        if ts is not None:
            if sub.started_at is None or ts < sub.started_at:
                sub.started_at = ts
            if sub.last_activity is None or ts > sub.last_activity:
                sub.last_activity = ts
        return sub

    def _on_child_start(self, sub: SubagentStatus, ts: datetime | None) -> None:
        """The child's own task_started: running, unless the main thread
        reported it done after that (a start wins a tie: the done then closes
        the task before it)."""
        tid = sub.agent_id
        if ts is not None:
            prev = self._own_start.get(tid)
            self._own_start[tid] = ts if prev is None or ts > prev else prev
        done = self._root_done.get(tid)
        if ts is None or done is None or ts >= done:
            sub.finished = False

    def _on_root_done(self, sub: SubagentStatus, ts: datetime | None) -> None:
        """The main thread's completed/interrupted: done, unless the child
        started a newer task (a follow-up the main thread logs only as
        ``interacted``)."""
        tid = sub.agent_id
        if ts is not None:
            prev = self._root_done.get(tid)
            self._root_done[tid] = ts if prev is None or ts > prev else prev
        start = self._own_start.get(tid)
        if ts is None or start is None or ts > start:
            sub.finished = True

    def _announce(self, sub: SubagentStatus, at: datetime | None) -> None:
        """Push one "⊕" per sub-agent into the current event log."""
        if sub.agent_id in self._announced:
            return
        self._announced.add(sub.agent_id)
        text = self._announce_texts.setdefault(
            sub.agent_id, f"⊕ subagent {sub.agent_type or '?'}: {sub.description[:60]}")
        self.state.push_event(at, "agent", text)

    # -- reset (rotation / rewrite) ------------------------------------------
    def reset_source(self, source_id: str) -> None:
        st = self.state
        st.drop_source_tokens(source_id)
        self._tokens.pop(source_id, None)
        kind, ident = _scope(source_id)
        if kind == "main":
            st.reset_main()
            st.last_prompt = None
            # re-derived by the replay; clearing them keeps the per-model
            # attribution of the replayed responses identical
            st.model = None
            st.mode = None
            self._prompts.clear()
            self._activities.clear()
            self._root_done.clear()  # the children's own starts are kept
            self._commands.clear()
            self._changes.clear()
            self._file_keys.clear()  # reset_main() emptied files_touched
            self._compactions.clear()
            self._announced.clear()  # reset_main() emptied the event log
            for agent_id in self._registered:  # back on top, as after the catch-up
                sub = st.subagents.get(agent_id)
                if sub is not None:
                    self._announce(sub, None)
            self._pending = None
        elif kind == "agent" and ident:
            self._child_models.pop(ident, None)
            self._child_meta_models.pop(ident, None)
            self._child_tasks.discard(ident)
            self._own_start.pop(ident, None)  # the main's done activity is kept
            sub = st.subagents.get(ident)
            if sub is not None:
                sub.turns = 0
                sub.open_tools = set()
                sub.finished = False
