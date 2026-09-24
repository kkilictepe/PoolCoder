"""Filesystem layout of ``$CODEX_HOME`` and Codex rollout discovery.

Codex writes one rollout file per thread::

    $CODEX_HOME/sessions/YYYY/MM/DD/rollout-<local-ts>-<thread-uuid>[_<rollout-id>].jsonl

``$CODEX_HOME`` defaults to ``~/.codex``. Line 1 of every rollout is the
thread's own ``session_meta`` (~20 KB, written once). Sub-agent, guardian and
memory threads are separate rollout files that point back at their root
thread; they are hidden from the session list by default. Compressed
``.jsonl.zst`` files and ``archived_sessions/`` are out of scope.

File mtimes are unreliable on Windows (some growing files keep their creation
time), so a session's activity time is the later of its mtime and the
timestamp of its latest record. Everything here opens files read-only and
never raises on bad input.
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from ..paths import SessionInfo, read_tail_text

ROLLOUT_RE = re.compile(
    r"^rollout-(?P<ts>\d{4}-\d{2}-\d{2}T\d{2}-\d{2}-\d{2})-"
    r"(?P<tid>[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})"
    r"(?:_(?P<rid>[^.]+))?\.jsonl$"
)

# thread_source values of threads that are not user-started sessions.
SUBAGENT_THREAD_SOURCES = frozenset({"subagent", "guardian_review", "memory_consolidation"})

SESSION_INDEX = "session_index.jsonl"
META_MAX_BYTES = 1 << 20    # cap on line 1 (real session_meta lines are ~20 KB)
TAIL_BYTES = 16384          # activity scan window...
TAIL_MAX_BYTES = 262144     # ...widened once when the last line is long
NAMES_MAX_BYTES = 8 << 20   # session_index.jsonl is tiny; bound it anyway

_META_CHUNK = 65536
_EPOCH = datetime.fromtimestamp(0, tz=timezone.utc)
# A record line starts with its timestamp: ``{"timestamp":"…",…``. Only a raw
# newline can precede it (newlines inside JSON strings are escaped).
_LINE_TS_RE = re.compile(rb'\n\{\s*"timestamp"\s*:\s*"([^"\\\r\n]{1,64})"')


# -- locations ---------------------------------------------------------------
def codex_home() -> Path:
    """``$CODEX_HOME`` (``~`` expanded), else ``<USERPROFILE or HOME>/.codex``."""
    env = os.environ.get("CODEX_HOME", "").strip()
    if env:
        return Path(os.path.expanduser(env))
    base = os.environ.get("USERPROFILE") or os.environ.get("HOME") or str(Path.home())
    return Path(base) / ".codex"


def sessions_root() -> Path:
    return codex_home() / "sessions"


# -- thread identity -----------------------------------------------------------
@dataclass(frozen=True)
class CodexMeta:
    """The fields of a rollout's own ``session_meta`` (line 1) that we use."""

    thread_id: str
    session_id: str = ""                 # root thread ("" if absent)
    parent_thread_id: str | None = None
    forked_from_id: str | None = None
    thread_source: str | None = None     # user / subagent / guardian_review / ...
    source: object = field(default=None, hash=False)   # str or {"subagent": ...}
    cwd: str | None = None
    git_branch: str | None = None
    cli_version: str | None = None
    originator: str | None = None
    agent_path: str | None = None
    agent_nickname: str | None = None
    model: str | None = None             # base_instructions.provenance.model
    started_at: datetime | None = None

    @property
    def is_subagent(self) -> bool:
        if self.thread_source in SUBAGENT_THREAD_SOURCES:
            return True
        src = self.source
        if isinstance(src, dict) and ("subagent" in src or "internal" in src):
            return True
        return bool(self.parent_thread_id)

    @property
    def is_guardian(self) -> bool:
        if self.thread_source == "guardian_review":
            return True
        sub = self.source.get("subagent") if isinstance(self.source, dict) else None
        return isinstance(sub, dict) and sub.get("other") == "guardian"

    @property
    def root_id(self) -> str:
        return self.session_id or self.thread_id


# Successful metas per normalized path (line 1 is written once).
_meta_cache: dict[str, CodexMeta] = {}
# Paths whose first line already exceeds META_MAX_BYTES (it can only grow).
_meta_oversize: set[str] = set()
# path -> (size, latest record timestamp or None) for last_activity().
_activity_cache: dict[str, tuple[int, datetime | None]] = {}
# session_index.jsonl -> ((size, mtime_ns), {thread id: name})
_names_cache: dict[str, tuple[tuple[int, int], dict[str, str]]] = {}


def clear_caches() -> None:
    """Forget every cached meta, activity time and thread name (tests use this)."""
    _meta_cache.clear()
    _meta_oversize.clear()
    _activity_cache.clear()
    _names_cache.clear()


def _key(path: Path | str) -> str:
    # String-only normalization: no filesystem round trip, unlike resolve().
    return os.path.normcase(os.path.abspath(os.fspath(path)))


def _str(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def _parse_ts(value: object) -> datetime | None:
    """ISO-8601 (``...Z``) -> UTC-aware datetime; mirrors
    ``pool_coder.parser.parse_timestamp`` so this module stays import-light."""
    if not isinstance(value, str) or not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _meta_from_record(obj: object) -> CodexMeta | None:
    if not isinstance(obj, dict) or obj.get("type") != "session_meta":
        return None
    p = obj.get("payload")
    if not isinstance(p, dict):
        return None
    tid = _str(p.get("id"))
    if tid is None:
        return None
    source = p.get("source")
    # Sub-agent threads repeat parent/path/nickname under source.subagent.thread_spawn.
    spawn: dict = {}
    if isinstance(source, dict):
        sub = source.get("subagent")
        if isinstance(sub, dict) and isinstance(sub.get("thread_spawn"), dict):
            spawn = sub["thread_spawn"]
    git = p.get("git")
    base = p.get("base_instructions")
    prov = base.get("provenance") if isinstance(base, dict) else None
    return CodexMeta(
        thread_id=tid,
        session_id=_str(p.get("session_id")) or "",
        parent_thread_id=_str(p.get("parent_thread_id")) or _str(spawn.get("parent_thread_id")),
        forked_from_id=_str(p.get("forked_from_id")),
        thread_source=_str(p.get("thread_source")),
        source=source,
        cwd=_str(p.get("cwd")),
        git_branch=_str(git.get("branch")) if isinstance(git, dict) else None,
        cli_version=_str(p.get("cli_version")),
        originator=_str(p.get("originator")),
        agent_path=_str(p.get("agent_path")) or _str(spawn.get("agent_path")),
        agent_nickname=_str(p.get("agent_nickname")) or _str(spawn.get("agent_nickname")),
        model=_str(prov.get("model")) if isinstance(prov, dict) else None,
        started_at=_parse_ts(p.get("timestamp")) or _parse_ts(obj.get("timestamp")),
    )


def _read_first_line(path: Path) -> tuple[bytes | None, bool]:
    """Line 1 without its newline, as ``(line, too_long)``.

    ``(None, False)``: unreadable, empty, or not newline-terminated yet (Codex
    may still be writing it). ``(None, True)``: longer than ``META_MAX_BYTES``.
    """
    buf = bytearray()
    try:
        with open(path, "rb") as fh:
            while len(buf) < META_MAX_BYTES:
                chunk = fh.read(min(_META_CHUNK, META_MAX_BYTES - len(buf)))
                if not chunk:
                    return None, False
                nl = chunk.find(b"\n")
                if nl != -1:
                    buf += chunk[:nl]
                    return bytes(buf), False
                buf += chunk
    except OSError:
        return None, False
    return None, True


def read_session_meta(path: Path) -> CodexMeta | None:
    """The rollout's own ``session_meta`` from line 1, or ``None``.

    Successful reads are cached per path. A missing, partial or garbled first
    line is not cached, so a file Codex is still creating is picked up later.
    """
    key = _key(path)
    hit = _meta_cache.get(key)
    if hit is not None:
        return hit
    if key in _meta_oversize:
        return None
    line, too_long = _read_first_line(path)
    if too_long:
        _meta_oversize.add(key)
        return None
    if line is None:
        return None
    try:
        obj = json.loads(line)
    except (ValueError, RecursionError):  # ValueError covers bad UTF-8 too
        return None
    meta = _meta_from_record(obj)
    if meta is not None:
        _meta_cache[key] = meta
    return meta


# -- activity time ---------------------------------------------------------------
def _latest_line_ts(data: bytes) -> datetime | None:
    best: datetime | None = None
    for m in _LINE_TS_RE.finditer(data):
        dt = _parse_ts(m.group(1).decode("ascii", errors="replace"))
        if dt is not None and (best is None or dt > best):
            best = dt
    return best


def _tail_timestamp(path: Path, size: int) -> tuple[datetime | None, bool]:
    """Latest line-leading record timestamp near the end, as ``(ts, ok)``.

    Scans the last ``TAIL_BYTES``, then once more over ``TAIL_MAX_BYTES`` if
    that window held no line start (a long final record). ``ok`` is False when
    the file could not be read.
    """
    try:
        with open(path, "rb") as fh:
            for window in (TAIL_BYTES, TAIL_MAX_BYTES):
                offset = max(size - window, 0)
                fh.seek(offset)
                data = fh.read(size - offset)
                if offset == 0:
                    data = b"\n" + data  # the file start is a line start
                latest = _latest_line_ts(data)
                if latest is not None or offset == 0:
                    return latest, True
    except OSError:
        return None, False
    return None, True


def _mtime(st: os.stat_result) -> datetime:
    try:
        return datetime.fromtimestamp(st.st_mtime, tz=timezone.utc)
    except (OverflowError, OSError, ValueError):
        return _EPOCH


def last_activity(path: Path, st: os.stat_result | None = None) -> datetime:
    """``max(mtime, latest record timestamp in the tail)``, UTC-aware.

    The tail timestamp is cached by ``(path, size)``; the mtime is re-read
    from ``st`` every call. Falls back to the mtime when nothing parses.
    """
    if st is None:
        try:
            st = os.stat(path)
        except OSError:
            return _EPOCH
    mtime = _mtime(st)
    key = _key(path)
    size = st.st_size
    hit = _activity_cache.get(key)
    if hit is not None and hit[0] == size:
        latest = hit[1]
    else:
        latest, ok = _tail_timestamp(path, size)
        if ok:
            _activity_cache[key] = (size, latest)
    return max(mtime, latest) if latest is not None else mtime


# -- discovery -----------------------------------------------------------------
def _subdirs(path: str) -> list[str]:
    try:
        with os.scandir(path) as it:
            return [e.path for e in it if e.is_dir()]
    except OSError:
        return []


def _iter_rollouts(base: Path) -> Iterator[tuple[Path, str, re.Match[str]]]:
    """``(path, "YYYY/MM/DD", name match)`` for ``base/*/*/*/rollout-*.jsonl``.

    Exactly three folder levels, so ``.jsonl.zst`` files, stray files and
    anything outside ``sessions/`` (``archived_sessions/``) never show up.
    """
    for year in _subdirs(os.fspath(base)):
        for month in _subdirs(year):
            for day in _subdirs(month):
                try:
                    with os.scandir(day) as it:
                        entries = list(it)
                except OSError:
                    continue
                folder = "/".join(Path(day).parts[-3:])
                for entry in entries:
                    m = ROLLOUT_RE.match(entry.name)
                    if m is None:
                        continue
                    try:
                        if not entry.is_file():
                            continue
                    except OSError:
                        continue
                    yield Path(entry.path), folder, m


def list_sessions(include_subagents: bool = False, root: Path | None = None) -> list[SessionInfo]:
    """Codex threads, newest activity first, one entry per thread id.

    Sub-agent, guardian and memory threads are hidden unless
    ``include_subagents``. Files whose first line is not readable yet are
    skipped (and retried on the next call).
    """
    base = root or sessions_root()
    best: dict[str, SessionInfo] = {}
    for path, folder, _m in _iter_rollouts(base):
        meta = read_session_meta(path)
        if meta is None or (meta.is_subagent and not include_subagents):
            continue
        try:
            # os.stat, not the scandir entry: on Windows the directory entry
            # can lag behind a file another process is still writing.
            st = os.stat(path)
        except OSError:
            continue
        info = SessionInfo(
            main_path=path,
            session_id=meta.thread_id,
            project_hash=folder,
            mtime=last_activity(path, st),
            size=st.st_size,
        )
        prev = best.get(info.session_id)
        if prev is None or info.mtime > prev.mtime:
            best[info.session_id] = info
    return sorted(best.values(), key=lambda s: s.mtime, reverse=True)


def normalize_id(session_id: str) -> str:
    """A thread id as ``find_session`` matches it: blanks trimmed, lower-case."""
    return (session_id or "").strip().lower()


def find_session(session_id: str, root: Path | None = None) -> SessionInfo | None:
    """The rollout of thread ``session_id`` (sub-agent threads included).

    Matches the thread id in the file name exactly (case-insensitive); when
    several files carry it, the most recently active wins.
    """
    sid = normalize_id(session_id)
    if not sid:
        return None
    best: SessionInfo | None = None
    for path, folder, m in _iter_rollouts(root or sessions_root()):
        if m.group("tid").lower() != sid:
            continue
        try:
            st = os.stat(path)
        except OSError:
            continue
        meta = read_session_meta(path)
        info = SessionInfo(
            main_path=path,
            session_id=meta.thread_id if meta else m.group("tid"),
            project_hash=folder,
            mtime=last_activity(path, st),
            size=st.st_size,
        )
        if best is None or info.mtime > best.mtime:
            best = info
    return best


def thread_names() -> dict[str, str]:
    """``{thread id: name}`` from ``session_index.jsonl``; the last entry wins.

    Missing file -> ``{}``; malformed lines are skipped. Cached until the
    file's size or mtime changes.
    """
    path = codex_home() / SESSION_INDEX
    try:
        st = os.stat(path)
    except OSError:
        return {}
    key = _key(path)
    stamp = (st.st_size, st.st_mtime_ns)
    hit = _names_cache.get(key)
    if hit is not None and hit[0] == stamp:
        return dict(hit[1])
    names: dict[str, str] = {}
    for raw in read_tail_text(path, NAMES_MAX_BYTES).splitlines():
        raw = raw.strip()
        if not raw:
            continue
        try:
            obj = json.loads(raw)
        except (ValueError, RecursionError):
            continue
        if not isinstance(obj, dict):
            continue
        tid, name = obj.get("id"), obj.get("thread_name")
        if isinstance(tid, str) and tid and isinstance(name, str) and name.strip():
            names[tid] = name.strip()
    _names_cache[key] = (stamp, names)
    return dict(names)
