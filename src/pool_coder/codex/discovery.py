"""Find the rollout files of a Codex session's sub-agent threads.

Codex writes every thread to its own rollout file in the folder of the day the
thread started (``sessions/YYYY/MM/DD``). Sub-agents, guardian reviewers and
their own children point back at the session from their line-1
``session_meta``: ``session_id`` is the root thread and ``parent_thread_id``
the thread that spawned them.

``CodexDiscovery`` lists a small window of day folders — the main file's day
plus the most recent days — and attaches every new file whose meta names the
monitored thread as its root, or an already attached thread as its parent
(repeated within one scan, so grandchildren attach with their parents). Each
child becomes a ``TailerSpec("agent:<tid>")`` plus a ``SubagentReg``.

Metas are read through ``codex.paths.read_session_meta`` (read-only, cached);
a file whose first line is not complete yet is retried on later scans. Every
file is classified once, so a warm ``scan()`` (every 1.5 s) is a few
``scandir`` calls and set lookups.
"""

from __future__ import annotations

import os
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Callable

from ..discovery import DiscoveryDelta, SubagentReg, TailerSpec
from . import paths as cpaths
from .paths import ROLLOUT_RE, CodexMeta

MAX_DAYS = 8  # day folders searched per scan (the main day + the most recent)
# An unreadable first line is re-read when the file grows, and at least once
# every this many scans otherwise (~30 s at the 1.5 s discovery interval).
RETRY_SCANS = 20


def _norm(path: Path | str) -> str:
    # String-only normalization (no filesystem round trip), as codex.paths does.
    return os.path.normcase(os.path.abspath(os.fspath(path)))


def _folder_date(folder: Path) -> date | None:
    """The date of a ``…/YYYY/MM/DD`` folder, or ``None``."""
    parts = folder.parts[-3:]
    if len(parts) != 3 or not all(p.isdigit() for p in parts):
        return None
    try:
        return date(int(parts[0]), int(parts[1]), int(parts[2]))
    except ValueError:
        return None


def _size(path: Path) -> int:
    try:
        return os.stat(path).st_size
    except OSError:
        return -1


def subagent_type(meta: CodexMeta) -> str:
    """``guardian``, else the last ``agent_path`` segment, else the thread source."""
    if meta.is_guardian:
        return "guardian"
    if meta.agent_path:
        seg = meta.agent_path.rstrip("/").rsplit("/", 1)[-1]
        if seg:
            return seg
    return meta.thread_source or "subagent"


def subagent_description(meta: CodexMeta) -> str:
    """``"<nickname> · <agent_path>"`` (or whichever part exists)."""
    return " · ".join(p for p in (meta.agent_nickname, meta.agent_path) if p)


class CodexDiscovery:
    """Discovery for one Codex thread: its own rollout plus its descendants."""

    def __init__(self, main_path: str | Path, session_id: str, *, root: Path | None = None,
                 today: Callable[[], date] | None = None, max_days: int = MAX_DAYS):
        self.main_path = Path(main_path)
        self.root = Path(root) if root is not None else cpaths.sessions_root()
        self.today = today or date.today
        self.max_days = max(1, int(max_days))
        sid = (session_id or "").strip()
        if not sid:  # fall back to the thread id in the file name
            m = ROLLOUT_RE.match(self.main_path.name)
            sid = m.group("tid") if m else ""
        self.session_id = sid
        self._sid = sid.lower()
        self._main_key = _norm(self.main_path)
        self._main_day = _folder_date(self.main_path.parent)
        # Thread ids (lower-case) whose children attach: the session + attached.
        self._known: set[str] = {self._sid} if self._sid else set()
        self.children: dict[str, Path] = {}  # attached thread id -> rollout path
        self._done: set[str] = set()  # file keys attached or ignored for good
        # Readable metas of files not related (yet): they attach later if a
        # thread they descend from attaches.
        self._others: dict[str, tuple[Path, CodexMeta]] = {}
        # Files whose meta was unreadable: key -> (size at last read, scans skipped).
        self._pending: dict[str, tuple[int | None, int]] = {}

    # -- discovery contract -------------------------------------------------
    def initial(self) -> DiscoveryDelta:
        delta = DiscoveryDelta()
        delta.new_tailers.append(TailerSpec("main", self.main_path))
        self._scan_into(delta)
        return delta

    def scan(self) -> DiscoveryDelta:
        delta = DiscoveryDelta()
        self._scan_into(delta)
        return delta

    def day_folders(self) -> list[Path]:
        """The main file's folder, then today backwards to (not including)
        the main day — ``max_days`` folders at most."""
        folders = [self.main_path.parent]
        day = self.today()
        if isinstance(day, datetime):
            day = day.date()
        while len(folders) < self.max_days and (self._main_day is None or day > self._main_day):
            folders.append(self.root / f"{day.year:04d}" / f"{day.month:02d}" / f"{day.day:02d}")
            try:
                day -= timedelta(days=1)
            except OverflowError:
                break
        return folders

    # -- internals --------------------------------------------------------------
    def _scan_into(self, delta: DiscoveryDelta) -> None:
        if not self._sid:
            return
        fresh: list[tuple[str, str, Path]] = []
        folder_keys: set[str] = set()
        for folder in self.day_folders():
            fkey = _norm(folder)
            if fkey in folder_keys:
                continue
            folder_keys.add(fkey)
            try:
                with os.scandir(folder) as it:
                    names = [e.name for e in it]
            except OSError:
                continue  # the day folder does not exist (yet)
            for name in names:
                key = os.path.join(fkey, os.path.normcase(name))
                if key in self._done or key in self._others:
                    continue
                m = ROLLOUT_RE.match(name)
                if m is None or key == self._main_key or m.group("tid").lower() == self._sid:
                    self._done.add(key)  # not a rollout, or the main thread's own file
                    continue
                fresh.append((name, key, folder / name))

        added = False
        fresh.sort()  # names start with the local start time: oldest first
        for _name, key, path in fresh:
            meta = self._read_meta(key, path)
            if meta is None:
                continue
            tid = meta.thread_id.lower()
            if tid == self._sid or tid in self._known:
                self._done.add(key)  # the main thread, or a second file of a child
                continue
            self._others[key] = (path, meta)
            added = True
        if added:
            self._resolve(delta)

    def _read_meta(self, key: str, path: Path) -> CodexMeta | None:
        """The file's meta; an unreadable first line is only re-read when the
        file has grown since the last try (or every ``RETRY_SCANS`` scans)."""
        pend = self._pending.get(key)
        size: int | None = None
        if pend is not None:
            # stat before reading: bytes written after the stat change the size.
            size = _size(path)
            last, skipped = pend
            if size == last and skipped < RETRY_SCANS:
                self._pending[key] = (last, skipped + 1)
                return None
        meta = cpaths.read_session_meta(path)
        if meta is None:
            self._pending[key] = (size, 0)
        else:
            self._pending.pop(key, None)
        return meta

    def _related(self, meta: CodexMeta) -> bool:
        if meta.session_id.lower() == self._sid:
            return True
        parent = meta.parent_thread_id
        return bool(parent) and parent.lower() in self._known

    def _resolve(self, delta: DiscoveryDelta) -> None:
        """Attach every related file; repeat until a pass attaches nothing, so
        a grandchild attaches once its parent has."""
        progress = True
        while progress:
            progress = False
            for key, (path, meta) in list(self._others.items()):
                if not self._related(meta):
                    continue
                del self._others[key]
                self._done.add(key)
                tid = meta.thread_id
                if tid.lower() in self._known:
                    continue  # a second file of a thread we already tail
                self._known.add(tid.lower())
                self.children[tid] = path
                delta.new_tailers.append(TailerSpec(f"agent:{tid}", path))
                delta.subagents.append(
                    SubagentReg(tid, subagent_type(meta), subagent_description(meta), "")
                )
                progress = True
