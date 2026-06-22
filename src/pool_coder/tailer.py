"""Read-only, byte-offset tailer for append-only JSONL files.

Safety contract (the #1 requirement): we only ever open files read-only in
binary mode and never write/flush/truncate them. CPython's default ``open``
on Windows shares read+write+delete, so this cannot block Claude Code's
appends.

Each ``poll()`` does: ``stat`` -> (maybe) ``open`` -> ``seek`` -> ``read`` a
bounded chunk -> ``close``. We never hold a handle between polls. Complete,
newline-terminated lines are returned; a partial trailing line is buffered as
*bytes* until its newline arrives (this also structurally excludes
half-written JSON and rejoins UTF-8 sequences split across reads).
"""

from __future__ import annotations

import os
from pathlib import Path

# Sentinel yielded (first) when a file is truncated or replaced; the consumer
# must drop any state it accumulated from this file and replay from scratch.
RESET = object()

DEFAULT_MAX_READ = 4 * 1024 * 1024  # cap bytes consumed per poll (huge catch-up)


def _identity(st: os.stat_result) -> tuple[int, int, int]:
    return (st.st_dev, st.st_ino, getattr(st, "st_ctime_ns", 0))


class Tailer:
    """Tails one file, surfacing complete decoded lines since the last poll."""

    def __init__(self, path: str | os.PathLike, max_read: int = DEFAULT_MAX_READ):
        self.path = Path(path)
        self.max_read = max_read
        self.offset = 0
        self.size = 0
        self.partial = b""
        self.identity: tuple[int, int, int] | None = None

    @property
    def has_backlog(self) -> bool:
        """True when the file holds more bytes than we've consumed."""
        return self.offset < self.size

    def poll(self) -> list:
        """Return new complete lines (``str``); a leading ``RESET`` on rotation."""
        try:
            st = os.stat(self.path)
        except OSError:
            return []

        out: list = []
        ident = _identity(st)
        rotated = False
        if self.identity is None:
            self.identity = ident
        elif ident != self.identity:
            rotated = True
            self.identity = ident
        if st.st_size < self.offset:
            rotated = True
        if rotated:
            out.append(RESET)
            self.offset = 0
            self.partial = b""

        self.size = st.st_size
        if st.st_size <= self.offset:
            return out

        to_read = min(st.st_size - self.offset, self.max_read)
        try:
            with open(self.path, "rb") as fh:
                fh.seek(self.offset)
                chunk = fh.read(to_read)
        except OSError:
            return out
        if not chunk:
            return out
        self.offset += len(chunk)

        buf = self.partial + chunk
        *complete, self.partial = buf.split(b"\n")
        for raw in complete:
            if not raw:
                continue
            text = raw.decode("utf-8", errors="replace").lstrip(chr(0xFEFF))
            out.append(text)
        return out
