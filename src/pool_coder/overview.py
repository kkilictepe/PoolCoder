"""Cheap per-session summaries for the picker and ``--list``.

Reads only the tail of each session file (not the whole thing) to extract the
latest model, context size, cwd and most recent prompt — enough to choose a
session without a full parse.
"""

from __future__ import annotations

from dataclasses import dataclass

from .aggregator import clean_prompt
from .config import Config, LIVE_WINDOW_SECONDS
from .parser import parse_line
from .paths import SessionInfo, read_tail_text


@dataclass
class SessionOverview:
    info: SessionInfo
    cwd: str | None
    model: str | None
    git_branch: str | None
    context_tokens: int
    occupancy: float
    last_text: str | None
    is_live: bool

    @property
    def label(self) -> str:
        if self.cwd:
            return self.cwd.replace("\\", "/").rstrip("/").split("/")[-1] or self.cwd
        return self.info.project_hash


def peek_session(info: SessionInfo, config: Config | None = None,
                 tail_bytes: int = 131072) -> SessionOverview:
    config = config or Config()
    model = cwd = branch = last_text = None
    ctx = max_ctx = 0
    for line in read_tail_text(info.main_path, tail_bytes).splitlines():
        rec = parse_line(line)
        if rec is None:
            continue
        if rec.cwd:
            cwd = rec.cwd
        if rec.git_branch:
            branch = rec.git_branch
        if rec.type == "assistant":
            usage = rec.usage()
            if usage:
                ctx = usage.context_tokens
                max_ctx = max(max_ctx, ctx)
            if rec.model:
                model = rec.model
        elif rec.type == "user":
            text = clean_prompt(rec.user_text())
            if text:
                last_text = text
    window = config.window_for(model, max_ctx)
    return SessionOverview(
        info=info,
        cwd=cwd,
        model=model,
        git_branch=branch,
        context_tokens=ctx,
        occupancy=(ctx / window) if window else 0.0,
        last_text=last_text,
        is_live=info.age_seconds() <= LIVE_WINDOW_SECONDS,
    )
