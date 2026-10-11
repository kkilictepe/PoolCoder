"""Cheap per-thread summaries of Codex rollouts for the picker, ``--list`` and
the web session list.

The Codex counterpart of ``pool_coder.overview.peek_session``; it returns the
same ``SessionOverview``. The branch (and a fallback cwd) come from the
rollout's own ``session_meta`` (line 1, cached by ``codex.paths``); the model,
cwd, context size, reported window and latest prompt come from the last
256 KB. A long agentic turn easily pushes every ``turn_context`` out of that
tail, so the model and window then fall back to the file's head (first
256 KB, read once and cached) and finally to the meta's model. A
``compacted`` marker discards the context samples before it (Codex's
estimate right after it, then the next response, give the new size), as in
the dashboard. Without a prompt in the tail, the thread's name from
``session_index.jsonl`` is shown instead, flagged ``last_is_title`` (the
lists bracket it).

Results are cached by ``(path, size)``: the web list re-peeks every session
every 3 s, and only files that grew are read again. Bulky records (tool
calls and outputs, reasoning, compaction history) are recognised from their
first bytes and never JSON-decoded. Files are opened read-only; nothing here
raises on bad input.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path

from ..config import LIVE_WINDOW_SECONDS, Config
from ..overview import SessionOverview
from ..paths import SessionInfo, read_tail_text
from . import paths as cpaths
from .parser import CodexRecord, clean_codex_prompt, parse_codex_line, usage_from

TAIL_BYTES = 262144   # default tail window
HEAD_BYTES = 262144   # head window for the model/window fallback
NAME_MAX = 200        # thread names are shown like prompts: one line, capped

# The top-level type (and an event's payload type) from a line's first bytes:
# ``{"timestamp":"…","ordinal":N,"type":"event_msg","payload":{"type":"token_count"``.
# Lines of any other layout are simply decoded.
_LINE_HEAD = re.compile(
    r'\s*\{\s*"timestamp"\s*:\s*"[^"\\]*"\s*,\s*(?:"ordinal"\s*:\s*-?\d+\s*,\s*)?'
    r'"type"\s*:\s*"(?P<type>\w+)"'
    r'(?:\s*,\s*"payload"\s*:\s*\{\s*"type"\s*:\s*"(?P<ptype>\w+)")?'
)
_RECORDS = frozenset({"turn_context", "token_usage_record", "event_msg"})
_EVENTS = frozenset({"token_count", "task_started", "thread_settings_applied",
                     "user_message", "item_completed"})


@dataclass(frozen=True, slots=True)
class _Facts:
    """What a rollout of a given size says (``window`` 0 = none reported)."""

    thread_id: str
    cwd: str | None
    git_branch: str | None
    model: str | None
    context_tokens: int
    window: int
    prompt: str | None


# normalized path -> ((size, tail_bytes), facts)
_cache: dict[str, tuple[tuple[int, int], _Facts]] = {}
# normalized path -> (bytes read, model, window) of the file's head
_head_cache: dict[str, tuple[int, str | None, int]] = {}


def clear_caches() -> None:
    """Forget every cached summary and head (tests use this)."""
    _cache.clear()
    _head_cache.clear()


def _key(path: Path) -> str:
    return os.path.normcase(os.path.abspath(os.fspath(path)))


def _str(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def _wanted(line: str) -> bool:
    """Could this line matter here? (Unrecognised layouts: yes.)"""
    return _wanted_head(line, _LINE_HEAD.match(line))


def _wanted_head(line: str, m: re.Match[str] | None) -> bool:
    if m is None:
        return bool(line.strip())
    rtype = m.group("type")
    if rtype not in _RECORDS:
        return False
    if rtype != "event_msg":
        return True
    ptype = m.group("ptype")
    if ptype is None:
        return True
    if ptype == "item_completed":
        return "UserMessage" in line  # only prompts; skip commands, reasoning, ...
    return ptype in _EVENTS


def _item_text(item: dict) -> str:
    """Text of an ``item_completed{UserMessage}`` (its text parts, joined)."""
    content = item.get("content")
    if not isinstance(content, list):
        return ""
    return "\n".join(part["text"] for part in content
                     if isinstance(part, dict) and isinstance(part.get("text"), str))


class _Scan:
    """One pass over rollout text; the latest value of each kind wins."""

    __slots__ = ("own", "model", "cwd", "window", "usage_ctx", "count_ctx", "compact_ctx",
                 "compacting", "prompt")

    def __init__(self, own: str):
        self.own = own
        self.model: str | None = None
        self.cwd: str | None = None
        self.window = 0       # model_context_window reported by the transcript
        self.usage_ctx = 0    # prompt size of the last own-thread token_usage_record
        self.count_ctx = 0    # ... of the last token_count (older versions)
        self.compact_ctx = 0  # Codex's estimate right after the last compaction
        self.compacting = False  # a `compacted` marker with no response after it yet
        self.prompt: str | None = None

    @property
    def context(self) -> int:
        return self.usage_ctx or self.count_ctx or self.compact_ctx

    def feed(self, text: str) -> _Scan:
        # split("\n"), not splitlines(): JSON strings may hold a raw U+2028
        for line in text.split("\n"):
            m = _LINE_HEAD.match(line)
            if m is not None and m.group("type") == "compacted":
                self._compacted()  # never decoded: it holds the whole history
            elif _wanted_head(line, m):
                rec = parse_codex_line(line)
                if rec is not None:
                    self._apply(rec)
        return self

    def _compacted(self) -> None:
        # the samples before the marker describe the old, replaced context
        self.usage_ctx = self.count_ctx = self.compact_ctx = 0
        self.compacting = True

    def _other_thread(self, payload: dict) -> bool:
        tid = payload.get("thread_id")
        return isinstance(tid, str) and bool(tid) and tid != self.own

    def _settings(self, settings: dict) -> None:
        self.model = _str(settings.get("model")) or self.model
        self.cwd = _str(settings.get("cwd")) or self.cwd

    def _window(self, value: object) -> None:
        if isinstance(value, int) and not isinstance(value, bool) and value > 0:
            self.window = value

    def _apply(self, rec: CodexRecord) -> None:
        p = rec.payload
        if rec.type == "turn_context":
            self._settings(p)
        elif rec.type == "token_usage_record":
            if p.get("thread_id") == self.own:  # never another thread's usage
                usage = usage_from(p.get("usage"))
                if usage is not None and usage.context_tokens > 0:
                    self.usage_ctx = usage.context_tokens
                    self.compacting = False
        elif rec.type == "event_msg":
            ptype = rec.ptype
            if ptype == "token_count":
                info = p.get("info")
                if isinstance(info, dict):
                    self._window(info.get("model_context_window"))
                    last = info.get("last_token_usage")
                    usage = usage_from(last)
                    if usage is not None and usage.context_tokens > 0:
                        self.count_ctx = usage.context_tokens
                        self.compacting = False
                    elif self.compacting and usage is not None and not usage.output:
                        # the estimate after a compaction (see CodexAggregator)
                        total = last.get("total_tokens")
                        if isinstance(total, int) and not isinstance(total, bool) and total > 0:
                            self.compact_ctx = total
            elif ptype == "task_started":
                self._window(p.get("model_context_window"))
            elif ptype == "thread_settings_applied":
                settings = p.get("thread_settings")
                if isinstance(settings, dict) and not self._other_thread(p):
                    self._settings(settings)
            elif ptype == "user_message":
                self._prompt(p.get("message"))
            elif ptype == "item_completed" and rec.item_type == "UserMessage":
                if not self._other_thread(p):
                    self._prompt(_item_text(rec.item))

    def _prompt(self, text: object) -> None:
        cleaned = clean_codex_prompt(text) if isinstance(text, str) else None
        if cleaned:
            self.prompt = cleaned


def _read_head(path: Path) -> tuple[str, int] | None:
    """The first ``HEAD_BYTES`` as text and their byte count (``None`` if unreadable)."""
    try:
        with open(path, "rb") as fh:
            data = fh.read(HEAD_BYTES)
    except OSError:
        return None
    # a cut-off last line is invalid JSON and gets skipped
    return data.decode("utf-8", errors="replace"), len(data)


def _head(path: Path, key: str, size: int, own: str) -> tuple[str | None, int]:
    """``(model, window)`` from the file's head, cached once the head is
    complete (it never changes after that) or while the file keeps its size."""
    hit = _head_cache.get(key)
    if hit is not None and hit[0] <= size and (hit[0] >= HEAD_BYTES or hit[0] == size):
        return hit[1], hit[2]
    head = _read_head(path)
    if head is None:
        return None, 0
    scan = _Scan(own).feed(head[0])
    _head_cache[key] = (head[1], scan.model, scan.window)
    return scan.model, scan.window


def _read_facts(path: Path, key: str, info: SessionInfo, size: int,
                tail_bytes: int) -> _Facts:
    meta = cpaths.read_session_meta(path)
    own = meta.thread_id if meta else info.session_id
    tail = _Scan(own).feed(read_tail_text(path, tail_bytes))
    model, window = tail.model, tail.window
    if (model is None or not window) and size > tail_bytes:
        head_model, head_window = _head(path, key, size, own)
        model = model or head_model
        window = window or head_window
    return _Facts(
        thread_id=own,
        # a resumed thread's latest turn_context wins over its creation cwd
        cwd=tail.cwd or (meta.cwd if meta else None),
        git_branch=meta.git_branch if meta else None,
        model=model or (meta.model if meta else None),
        context_tokens=tail.context,
        window=window,
        prompt=tail.prompt,
    )


def peek_codex_session(info: SessionInfo, config: Config | None = None,
                       tail_bytes: int = TAIL_BYTES) -> SessionOverview:
    """Summarise one Codex thread for a session list (cheap, cached)."""
    config = config or Config()
    path = Path(info.main_path)
    key = _key(path)
    try:
        size = os.stat(path).st_size  # info.size may be stale
    except OSError:
        size = None
    stamp = (info.size if size is None else size, tail_bytes)
    hit = _cache.get(key)
    if hit is not None and hit[0] == stamp:
        facts = hit[1]
    else:
        facts = _read_facts(path, key, info, stamp[0], tail_bytes)
        if size is not None:
            _cache[key] = (stamp, facts)

    last_text = facts.prompt
    if last_text is None:
        # No prompt in the tail (common on long agentic turns): the thread's
        # name, marked as such. Looked up on every call: a rename does not
        # change the rollout.
        name = cpaths.thread_names().get(facts.thread_id) or ""
        last_text = " ".join(name.split())[:NAME_MAX] or None
    # Codex models are all gpt-family; an unknown model still gets its window
    window = facts.window or config.window_for(facts.model or "gpt", agent="codex")
    ctx = facts.context_tokens
    return SessionOverview(
        info=info,
        cwd=facts.cwd,
        model=facts.model,
        git_branch=facts.git_branch,
        context_tokens=ctx,
        occupancy=(ctx / window) if window else 0.0,
        last_text=last_text,
        is_live=info.age_seconds() <= LIVE_WINDOW_SECONDS,
        last_is_title=facts.prompt is None and last_text is not None,
    )
