"""Small humanizing helpers shared by the CLI and the TUI."""

from __future__ import annotations

from datetime import datetime, timezone

from .config import model_family


def fmt_int(n: float) -> str:
    return f"{int(n):,}"


def fmt_tokens(n: float) -> str:
    n = int(n)
    if n < 1000:
        return str(n)
    if n < 1_000_000:
        return f"{n / 1000:.1f}k"
    return f"{n / 1_000_000:.2f}M"


def fmt_usd(x: float | None) -> str:
    if x is None:
        return "—"
    if 0 < x < 0.01:
        return f"${x:.4f}"
    return f"${x:,.2f}"


def fmt_pct(frac: float | None, digits: int = 0) -> str:
    """Format a 0..1 fraction as a percentage."""
    if frac is None:
        return "—"
    return f"{frac * 100:.{digits}f}%"


def fmt_pct100(value: float | None, digits: int = 0) -> str:
    """Format a value already expressed as a 0..100 percentage."""
    if value is None:
        return "—"
    return f"{value:.{digits}f}%"


def fmt_duration(seconds: float) -> str:
    seconds = int(max(seconds, 0))
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}h{m:02d}m"
    if m:
        return f"{m}m{s:02d}s"
    return f"{s}s"


def fmt_age(seconds: float) -> str:
    seconds = int(max(seconds, 0))
    if seconds < 60:
        return f"{seconds}s ago"
    if seconds < 3600:
        return f"{seconds // 60}m ago"
    if seconds < 86400:
        return f"{seconds // 3600}h ago"
    return f"{seconds // 86400}d ago"


def fmt_clock(dt: datetime | None) -> str:
    if dt is None:
        return "—"
    return dt.astimezone().strftime("%H:%M:%S")


def reset_in(dt: datetime | None) -> str:
    if dt is None:
        return "—"
    delta = (dt - datetime.now(timezone.utc)).total_seconds()
    if delta <= 0:
        return "now"
    return fmt_duration(delta)


def as_of_text(at: datetime | None, now: datetime | None = None) -> str | None:
    """``HH:MM (3m ago)``: Codex limits are only as fresh as the last response."""
    if at is None:
        return None
    now = now or datetime.now(timezone.utc)
    return f"{at.astimezone().strftime('%H:%M')} ({fmt_age((now - at).total_seconds())})"


def limit_window(pct: float | None, resets_at: datetime | None,
                 now: datetime | None = None) -> tuple[str, str | None, float | None]:
    """One Codex rate-limit window: ``(percent, reset phrase, percent for a bar)``.

    Codex's numbers come from the newest local record, however old. Once the
    window's reset time has passed they describe a finished window: it shows
    "—" and "reset, no newer data", and no bar. An unknown reset never expires.
    """
    if resets_at is None:
        return fmt_pct100(pct), None, pct
    left = (resets_at - (now or datetime.now(timezone.utc))).total_seconds()
    if left <= 0:
        return "—", "reset, no newer data", None
    return fmt_pct100(pct), f"resets {fmt_duration(left)}", pct  # as reset_in()


def short_model(model: str | None, agent: str | None = None) -> str:
    """Compact model label: ``claude-opus-4-8`` -> ``opus``, ``gpt-5.6-sol`` -> ``5.6-sol``
    (in a Claude Code session every id keeps the ``opus`` rule)."""
    if not model:
        return "?"
    if model_family(model, agent) == "gpt":
        # Keep the version: it is what tells GPT models apart.
        if model.lower().startswith("gpt-"):
            return model[4:] or model
        return model
    return model.split("-")[1] if "-" in model else model


def short_id(session_id: str, agent: str = "claude") -> str:
    """Eight-char session label.

    Codex thread ids are UUIDv7 (time-ordered): ids created within about a
    minute share their first 8 chars, so Codex uses the *last* 8 instead.
    """
    session_id = session_id or ""
    return session_id[-8:] if agent == "codex" else session_id[:8]
