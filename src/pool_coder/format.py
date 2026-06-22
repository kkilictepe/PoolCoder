"""Small humanizing helpers shared by the CLI and the TUI."""

from __future__ import annotations

from datetime import datetime, timezone


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
