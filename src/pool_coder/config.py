"""Tunable constants and the runtime ``Config`` object.

Context-window sizes for Opus cannot be inferred from the JSONL ``model``
string (it lacks the ``[1m]`` marker), so the limit is configurable. We
default Opus to 1,000,000 to match this repo's Grafana dashboard convention,
with a guard that auto-bumps to 1M for any model once observed context
exceeds the standard 200K window. Fable/Mythos ship with a 1M window (it is
both the default and the maximum), so that family defaults to 1M outright.

OpenAI models used by Codex (``gpt-*``, ``codex-*``, ``o3``/``o4-mini``) form
the ``gpt`` family in Codex sessions. Codex transcripts report their own
window, so the 272K default only applies when none was reported, and it is
never auto-bumped. In Claude Code sessions such ids keep the ``default``
family they always had.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

OPUS_1M = 1_000_000
STANDARD_WINDOW = 200_000

# Per-family defaults. Opus/Fable -> 1M; GPT -> 272K; others -> 200K.
DEFAULT_WINDOWS = {
    "opus": OPUS_1M,
    "fable": OPUS_1M,
    "sonnet": STANDARD_WINDOW,
    "haiku": STANDARD_WINDOW,
    "gpt": 272_000,
    "default": STANDARD_WINDOW,
}

# Auto-bump any window to 1M once observed context passes this.
WINDOW_AUTOBUMP_THRESHOLD = STANDARD_WINDOW

# A turn whose context falls below prev_context * this fraction is treated as
# an (auto-)compaction event.
COMPACTION_DROP_FRACTION = 0.5
# Claude Code auto-compacts near this fraction of the window.
AUTO_COMPACT_FRACTION = 0.92
# Codex compacts at ~0.9 x 272K ~= 245K, which is 0.947 of the 258,400 window
# it reports (272K x 95%).
CODEX_AUTO_COMPACT_FRACTION = 0.947

# Polling cadences (seconds).
TAIL_INTERVAL = 0.25
DISCOVERY_INTERVAL = 1.5
UI_REFRESH_INTERVAL = 0.4
PLAN_LIMITS_INTERVAL = 300  # >= 180 to respect the usage endpoint rate limit
CODEX_LIMITS_INTERVAL = 30  # local rollout rescans only (no network)

# Liveness windows (seconds).
LIVE_WINDOW_SECONDS = 60      # "● live" badge
ACTIVE_WINDOW_SECONDS = 1800  # default picker "recent/active" filter (30 min)


_O_SERIES = re.compile(r"^o\d")  # o3, o4-mini, ...


def model_family(model: str | None, agent: str | None = None) -> str:
    """Map a model id (e.g. ``claude-opus-4-8``, ``gpt-5.6-sol``) to a family key.

    ``agent="claude"`` keeps the families Claude Code sessions always had: an
    OpenAI id in a Claude Code transcript (e.g. behind a gateway) stays
    ``default``, with its window, auto-bump and price.
    """
    if not model:
        return "default"
    low = model.lower()
    # Mythos 5 is the same model tier as Fable 5 (same window and pricing).
    if "fable" in low or "mythos" in low:
        return "fable"
    for fam in ("opus", "sonnet", "haiku"):
        if fam in low:
            return fam
    # OpenAI (Codex) models, checked after Claude so Claude ids never move.
    if agent != "claude" and ("gpt" in low or "codex" in low or _O_SERIES.match(low)):
        return "gpt"
    return "default"


@dataclass
class Config:
    opus_window: int = DEFAULT_WINDOWS["opus"]
    fable_window: int = DEFAULT_WINDOWS["fable"]
    sonnet_window: int = DEFAULT_WINDOWS["sonnet"]
    haiku_window: int = DEFAULT_WINDOWS["haiku"]
    gpt_window: int = DEFAULT_WINDOWS["gpt"]
    auto_bump: bool = True
    plan_limits: bool = True
    active_window_seconds: int = ACTIVE_WINDOW_SECONDS
    agent: str = "claude"  # which agent's sessions to monitor ("claude" | "codex")

    def window_for(self, model: str | None, observed_max: int = 0,
                   agent: str | None = None) -> int:
        """Effective context window for a model given the largest context seen,
        in a session of ``agent`` (default: this config's agent)."""
        fam = model_family(model, agent or self.agent)
        if fam == "gpt":
            return self.gpt_window  # fixed window: the 1M auto-bump is Claude-only
        base = {
            "opus": self.opus_window,
            "fable": self.fable_window,
            "sonnet": self.sonnet_window,
            "haiku": self.haiku_window,
        }.get(fam, DEFAULT_WINDOWS["default"])
        if self.auto_bump and observed_max > WINDOW_AUTOBUMP_THRESHOLD:
            return max(base, OPUS_1M)
        return base

    def apply_window_override(self, spec: str) -> None:
        """Apply a ``family=tokens`` override (e.g. ``opus=200000``, ``gpt=400000``)."""
        fam, _, raw = spec.partition("=")
        fam = fam.strip().lower()
        try:
            tokens = int(raw.strip())
        except ValueError:
            raise ValueError(f"invalid --window value: {spec!r}") from None
        if fam == "opus":
            self.opus_window = tokens
        elif fam in ("fable", "mythos"):
            self.fable_window = tokens
        elif fam == "sonnet":
            self.sonnet_window = tokens
        elif fam == "haiku":
            self.haiku_window = tokens
        elif fam in ("gpt", "codex"):
            self.gpt_window = tokens
        else:
            raise ValueError(f"unknown model family in --window: {fam!r}")
