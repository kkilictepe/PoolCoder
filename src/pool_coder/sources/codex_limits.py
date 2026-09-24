"""Codex plan-limit source: account rate limits read from local rollouts.

Codex copies the account's rate-limit snapshot (the ``x-codex-*`` response
headers) into every ``event_msg.token_count`` record as
``payload.rate_limits``::

    {"limit_id": "codex", "primary": {"used_percent", "window_minutes",
     "resets_at"} | null, "secondary": ... | null, "plan_type": "plus",
     "credits": {"has_credits", "unlimited", "balance"},
     "rate_limit_reached_type": null, ...}

Limits are account-wide, so the freshest snapshot of *any* thread counts,
sub-agents included: we tail the most recently active rollouts and keep the
``token_count`` with the latest record timestamp. No network, ``auth.json`` is
never read, files are only opened ``"rb"``, and a rescan only reopens the
rollouts that changed. Same surface as ``PlanLimitsSource`` (daemon poller +
cached ``view``); never raises.
"""

from __future__ import annotations

import math
import os
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

from ..codex import paths as cpaths
from ..codex.parser import parse_codex_line
from ..config import CODEX_LIMITS_INTERVAL
from ..snapshot import PlanLimitsView

NO_DATA = "no Codex rate-limit data yet"
RECENT_FILES = 8        # rollouts tailed per scan (most recently active first)...
MAX_FILES = 32          # ...continuing to older ones only while none had data
TAIL_BYTES = 1 << 20    # tail read per rollout
MIN_INTERVAL = 1.0      # floor so a zero/negative interval cannot busy-loop
SHORT_WINDOW_MAX = 1440  # minutes: a window up to one day uses the 5-hour slot

# Only the value token matches: an escaped mention inside some other record's
# text reads ``\"token_count\"`` and never contains this exact byte string.
_NEEDLE = b'"token_count"'
_EPOCH_MS = 1e11  # a resets_at this large is milliseconds, not seconds


# -- parsing -------------------------------------------------------------------
def _number(value: object) -> float | None:
    """A finite number (numeric strings accepted); bools and garbage -> None."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        num = float(value)
    elif isinstance(value, str):
        try:
            num = float(value.strip())
        except ValueError:
            return None
    else:
        return None
    return num if math.isfinite(num) else None


def _minutes(value: object) -> int | None:
    num = _number(value)
    if num is None or num <= 0:
        return None
    return int(num)


def window_label(minutes: int | None, default: str) -> str:
    """300 -> "5-hour", 10080 -> "weekly", else "Nd" / "Nh" / "Nm"."""
    if minutes is None:
        return default
    if minutes == 300:
        return "5-hour"
    if minutes == 10080:
        return "weekly"
    if minutes % 1440 == 0:
        return f"{minutes // 1440}d"
    if minutes % 60 == 0:
        return f"{minutes // 60}h"
    return f"{minutes}m"


def _reset_time(w: dict, at: datetime | None) -> datetime | None:
    """``resets_at`` (epoch seconds, UTC), else ``at + resets_in_seconds``."""
    raw = w.get("resets_at")
    epoch = _number(raw)
    if epoch is not None:
        if epoch > _EPOCH_MS:
            epoch /= 1000.0
        try:
            return datetime.fromtimestamp(epoch, tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            pass
    elif isinstance(raw, str) and raw.strip():
        try:
            dt = datetime.fromisoformat(raw.strip().replace("Z", "+00:00"))
        except ValueError:
            dt = None
        if dt is not None:
            return (dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)).astimezone(timezone.utc)
    secs = _number(w.get("resets_in_seconds"))
    if secs is not None and at is not None:
        try:
            return at + timedelta(seconds=secs)
        except OverflowError:
            return None
    return None


def _slots(rl: dict) -> dict[str, tuple[dict, int | None]]:
    """Place the non-null windows into the ``"five"`` / ``"seven"`` slots.

    Classified by ``window_minutes``, not by primary/secondary: one window
    goes by length (<= 1 day -> five); with two, the shorter takes five and
    the longer seven. A window without ``window_minutes`` keeps Codex's
    convention (primary short, secondary long) and takes the free slot.
    """
    entries = []
    for pos, key in enumerate(("primary", "secondary")):
        w = rl.get(key)
        if isinstance(w, dict):
            entries.append((_minutes(w.get("window_minutes")), pos, w))
    if len(entries) == 2 and all(m is not None for m, _, _ in entries):
        short, long_ = sorted(entries, key=lambda e: (e[0], e[1]))
        return {"five": (short[2], short[0]), "seven": (long_[2], long_[0])}
    slots: dict[str, tuple[dict, int | None]] = {}
    for minutes, pos, w in sorted(entries, key=lambda e: (e[0] is None, e[0] or 0, e[1])):
        if minutes is not None:
            want = "five" if minutes <= SHORT_WINDOW_MAX else "seven"
        else:
            want = "five" if pos == 0 else "seven"
        slot = want if want not in slots else ("seven" if want == "five" else "five")
        if slot not in slots:
            slots[slot] = (w, minutes)
    return slots


def _note(rl: dict) -> str | None:
    parts: list[str] = []
    credits = rl.get("credits")
    if isinstance(credits, dict):
        balance = credits.get("balance")
        if credits.get("unlimited") is True:
            parts.append("unlimited credits")
        elif balance is not None and not isinstance(balance, bool) and str(balance).strip():
            parts.append(f"credits {str(balance).strip()}")
    reached = rl.get("rate_limit_reached_type")
    if isinstance(reached, str) and reached.strip():
        parts.append(f"limit reached: {reached.strip()}")
    return " · ".join(parts) or None


def parse_rate_limits(rl: dict, at: datetime | None) -> PlanLimitsView:
    """One ``token_count.rate_limits`` snapshot -> ``PlanLimitsView``.

    ``at`` is the record's timestamp: it becomes ``as_of`` and anchors the
    older relative ``resets_in_seconds``. Both windows null (e.g. a business
    plan with unlimited credits) is still available data, carried by the note.
    """
    if not isinstance(rl, dict):
        return PlanLimitsView(available=False, error=NO_DATA)
    slots = _slots(rl)
    fields: dict[str, object] = {}
    for slot, prefix, default in (("five", "five_hour", "5-hour"),
                                  ("seven", "seven_day", "weekly")):
        if slot not in slots:
            continue
        w, minutes = slots[slot]
        fields[f"{prefix}_pct"] = _number(w.get("used_percent"))
        fields[f"{prefix}_resets_at"] = _reset_time(w, at)
        fields[f"{prefix}_label"] = window_label(minutes, default)
    plan = rl.get("plan_type")
    return PlanLimitsView(
        available=True,
        error=None,
        as_of=at,
        plan_type=plan.strip() if isinstance(plan, str) and plan.strip() else None,
        note=_note(rl),
        **fields,  # type: ignore[arg-type]
    )


# -- rollout scanning ------------------------------------------------------------
def _read_tail(path: Path, max_bytes: int) -> bytes:
    """The last ``max_bytes`` of ``path`` minus a leading partial line."""
    with open(path, "rb") as fh:
        size = fh.seek(0, os.SEEK_END)
        offset = max(size - max_bytes, 0)
        fh.seek(offset)
        data = fh.read(size - offset)
    if offset:
        nl = data.find(b"\n")
        data = data[nl + 1:] if nl != -1 else b""
    return data


def latest_rate_limits(path: Path, max_bytes: int = TAIL_BYTES) -> tuple[datetime, dict] | None:
    """``(timestamp, rate_limits)`` of the last ``token_count`` in the tail of
    ``path`` that carries a dict ``rate_limits`` and a timestamp, else None.

    Scans backwards and parses only lines holding a ``"token_count"`` token;
    unreadable files and bad lines (including a partial last line) are skipped.
    """
    try:
        data = _read_tail(path, max_bytes)
    except OSError:
        return None
    return _latest_in(data)


def _latest_in(data: bytes) -> tuple[datetime, dict] | None:
    end = len(data)
    while end > 0:
        hit = data.rfind(_NEEDLE, 0, end)
        if hit == -1:
            return None
        start = data.rfind(b"\n", 0, hit) + 1
        stop = data.find(b"\n", hit)
        end = start  # next round: everything before this line
        rec = parse_codex_line(data[start:stop if stop != -1 else len(data)])
        if rec is None or rec.type != "event_msg" or rec.ptype != "token_count":
            continue
        rl = rec.payload.get("rate_limits")
        if isinstance(rl, dict) and rec.timestamp is not None:
            return rec.timestamp, rl
    return None


class CodexLimitsSource:
    """Polls local rollouts for the account's latest rate-limit snapshot."""

    def __init__(self, interval: float = CODEX_LIMITS_INTERVAL, root: Path | None = None):
        try:
            self.interval = max(MIN_INTERVAL, float(interval))
        except (TypeError, ValueError):
            self.interval = float(CODEX_LIMITS_INTERVAL)
        self.root = root  # sessions root; None -> codex.paths.sessions_root() per scan
        self._lock = threading.Lock()
        self._view = PlanLimitsView(available=False, error="not polled yet")
        # Best snapshot seen so far: a scan only replaces it with a newer one,
        # so a burst of fresh sub-agent files without token_count yet (or a
        # vanished file) never blanks limits we already know.
        self._latest: tuple[datetime, dict] | None = None
        # path -> ((size, activity), what its tail held) from the last scan.
        # An unchanged rollout is never reopened: on Windows an open handle
        # blocks Codex from renaming (archiving) or deleting that file.
        self._seen: dict[str, tuple[tuple, tuple[datetime, dict] | None]] = {}
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    @property
    def view(self) -> PlanLimitsView:
        with self._lock:
            return self._view

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._run, name="codex-limits", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _run(self) -> None:
        while not self._stop.is_set():
            self.fetch_once()
            self._stop.wait(self.interval)

    def fetch_once(self) -> PlanLimitsView:
        try:
            found = self._scan()
        except Exception:  # never let a scan error escape the poller
            found = None
        with self._lock:
            if found is not None and (self._latest is None or found[0] >= self._latest[0]):
                self._latest = found
            if self._latest is None:
                view = PlanLimitsView(available=False, error=NO_DATA)
            else:
                view = parse_rate_limits(self._latest[1], self._latest[0])
            self._view = view
        return view

    def _scan(self) -> tuple[datetime, dict] | None:
        """Latest snapshot among the ``RECENT_FILES`` most recently active
        rollouts (sub-agents included); older ones only while none had one."""
        best: tuple[datetime, dict] | None = None
        seen: dict[str, tuple[tuple, tuple[datetime, dict] | None]] = {}
        sessions = cpaths.list_sessions(include_subagents=True, root=self.root)
        for i, info in enumerate(sessions):
            if i >= MAX_FILES or (i >= RECENT_FILES and best is not None):
                break
            key = str(info.main_path)
            stamp = (info.size, info.mtime)  # size too: a growing file's mtime can lag
            known = self._seen.get(key)
            if known is not None and known[0] == stamp:
                hit = known[1]  # "no data" is remembered as well
            else:
                try:
                    hit = _latest_in(_read_tail(info.main_path, TAIL_BYTES))
                except OSError:
                    continue  # unreadable right now: retried next scan
            seen[key] = (stamp, hit)
            if hit is not None and (best is None or hit[0] > best[0]):
                best = hit
        self._seen = seen  # files no longer scanned are forgotten
        return best
