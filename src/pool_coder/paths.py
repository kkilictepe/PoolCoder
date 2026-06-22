"""Filesystem layout of ``~/.claude/projects`` and session/sidecar discovery.

A *session* is a top-level ``<project-hash>/<session-id>.jsonl`` file. Its
sidecars live under a sibling directory ``<project-hash>/<session-id>/``:

    <session-id>/subagents/agent-<id>.jsonl            (+ .meta.json)
    <session-id>/workflows/wf_<id>.json
    <session-id>/subagents/workflows/wf_<id>/journal.jsonl   (+ agent-*.jsonl)

``tool-results/`` and project-level ``memory/`` are intentionally excluded —
they are large non-JSONL blobs.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

EXCLUDED_DIRS = {"tool-results", "memory"}


def projects_root() -> Path:
    base = os.environ.get("USERPROFILE") or os.environ.get("HOME") or str(Path.home())
    return Path(base) / ".claude" / "projects"


def encode_project_path(path: str) -> str:
    """Encode a working-directory path the way Claude Code names project dirs."""
    return re.sub(r"[^A-Za-z0-9]", "-", path)


def session_dir(main_path: Path) -> Path:
    """The sidecar directory for a session file (``<sid>.jsonl`` -> ``<sid>/``)."""
    return main_path.with_suffix("")


def subagents_dir(main_path: Path) -> Path:
    return session_dir(main_path) / "subagents"


def workflows_dir(main_path: Path) -> Path:
    return session_dir(main_path) / "workflows"


def subagent_files(main_path: Path) -> list[Path]:
    """Top-level subagent transcripts (not the workflow ones)."""
    d = subagents_dir(main_path)
    if not d.is_dir():
        return []
    return sorted(p for p in d.glob("agent-*.jsonl") if p.is_file())


def workflow_meta_files(main_path: Path) -> list[Path]:
    d = workflows_dir(main_path)
    if not d.is_dir():
        return []
    return sorted(p for p in d.glob("wf_*.json") if p.is_file())


def workflow_dirs(main_path: Path) -> list[Path]:
    d = subagents_dir(main_path) / "workflows"
    if not d.is_dir():
        return []
    return sorted(p for p in d.glob("wf_*") if p.is_dir())


def workflow_journal(wf_dir: Path) -> Path:
    return wf_dir / "journal.jsonl"


def workflow_agent_files(wf_dir: Path) -> list[Path]:
    return sorted(p for p in wf_dir.glob("agent-*.jsonl") if p.is_file())


@dataclass
class SessionInfo:
    main_path: Path
    session_id: str
    project_hash: str
    mtime: datetime
    size: int

    def age_seconds(self, now: datetime | None = None) -> float:
        now = now or datetime.now(timezone.utc)
        return (now - self.mtime).total_seconds()


def list_sessions(root: Path | None = None) -> list[SessionInfo]:
    """All sessions across all projects, newest first."""
    root = root or projects_root()
    out: list[SessionInfo] = []
    if not root.exists():
        return out
    for proj in root.iterdir():
        if not proj.is_dir():
            continue
        for f in proj.glob("*.jsonl"):
            try:
                st = f.stat()
            except OSError:
                continue
            out.append(
                SessionInfo(
                    main_path=f,
                    session_id=f.stem,
                    project_hash=proj.name,
                    mtime=datetime.fromtimestamp(st.st_mtime, tz=timezone.utc),
                    size=st.st_size,
                )
            )
    out.sort(key=lambda s: s.mtime, reverse=True)
    return out


def find_session(session_id: str, root: Path | None = None) -> SessionInfo | None:
    for s in list_sessions(root):
        if s.session_id == session_id:
            return s
    return None


def read_tail_text(path: Path, max_bytes: int = 65536) -> str:
    """Read up to the last ``max_bytes`` of a file as UTF-8 (read-only).

    Drops a leading partial line so callers parse only complete records.
    """
    try:
        size = path.stat().st_size
        with open(path, "rb") as fh:
            if size > max_bytes:
                fh.seek(size - max_bytes)
            data = fh.read()
    except OSError:
        return ""
    text = data.decode("utf-8", errors="replace")
    if size > max_bytes:
        nl = text.find("\n")
        if nl != -1:
            text = text[nl + 1:]
    return text
