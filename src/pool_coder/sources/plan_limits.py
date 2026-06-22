"""Plan-limit source: 5-hour + weekly quota utilization via the OAuth endpoint.

Mirrors the repo's ``usage-exporter``: read the OAuth access token from
``~/.claude/.credentials.json`` and GET the (undocumented) usage endpoint that
powers Claude Code's ``/usage`` command. Runs on its own daemon thread so a
slow/blocking network call never stalls the dashboard's render loop; the engine
only reads the cached ``view``. Never raises — failures become an
``unavailable`` view.
"""

from __future__ import annotations

import json
import os
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

from ..config import PLAN_LIMITS_INTERVAL
from ..snapshot import PlanLimitsView

USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
DEFAULT_USER_AGENT = "claude-code/2.1.178"  # UA is required or the endpoint 429s
HTTP_TIMEOUT = 30


def credentials_path() -> Path:
    env = os.environ.get("POOL_CODER_CREDENTIALS")
    if env:
        return Path(env)
    base = os.environ.get("USERPROFILE") or os.environ.get("HOME") or str(Path.home())
    return Path(base) / ".claude" / ".credentials.json"


def _iso(value: object) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


class PlanLimitsSource:
    def __init__(self, interval: float = PLAN_LIMITS_INTERVAL,
                 user_agent: str = DEFAULT_USER_AGENT):
        self.interval = max(180, interval)
        self.user_agent = user_agent
        self._lock = threading.Lock()
        self._view = PlanLimitsView(available=False, error="not polled yet")
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    @property
    def view(self) -> PlanLimitsView:
        with self._lock:
            return self._view

    def _set(self, view: PlanLimitsView) -> None:
        with self._lock:
            self._view = view

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._run, name="plan-limits", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _run(self) -> None:
        while not self._stop.is_set():
            self.fetch_once()
            self._stop.wait(self.interval)

    def fetch_once(self) -> PlanLimitsView:
        view = self._fetch()
        self._set(view)
        return view

    # -- internals -------------------------------------------------------
    def _read_token(self) -> tuple[str, float]:
        data = json.loads(credentials_path().read_text(encoding="utf-8"))
        oauth = data["claudeAiOauth"]
        return oauth["accessToken"], float(oauth.get("expiresAt", 0)) / 1000.0

    def _request(self, token: str) -> dict:
        req = urllib.request.Request(USAGE_URL, method="GET")
        req.add_header("Authorization", f"Bearer {token}")
        req.add_header("anthropic-beta", "oauth-2025-04-20")
        req.add_header("User-Agent", self.user_agent)
        req.add_header("Content-Type", "application/json")
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
            return json.loads(resp.read().decode("utf-8"))

    def _fetch(self) -> PlanLimitsView:
        try:
            token, expires_at = self._read_token()
        except (OSError, KeyError, json.JSONDecodeError, ValueError):
            return PlanLimitsView(available=False, error="credentials unavailable")
        if expires_at and expires_at <= time.time():
            return PlanLimitsView(available=False, error="token expired — open Claude Code")
        try:
            usage = self._request(token)
        except urllib.error.HTTPError as exc:
            hint = " (rate limited)" if exc.code == 429 else ""
            return PlanLimitsView(available=False, error=f"HTTP {exc.code}{hint}")
        except Exception:
            return PlanLimitsView(available=False, error="request failed")
        return self._parse(usage)

    @staticmethod
    def _parse(usage: dict) -> PlanLimitsView:
        def window(key: str) -> dict:
            value = usage.get(key)
            return value if isinstance(value, dict) else {}

        extra = window("extra_usage")
        return PlanLimitsView(
            available=True,
            error=None,
            five_hour_pct=window("five_hour").get("utilization"),
            five_hour_resets_at=_iso(window("five_hour").get("resets_at")),
            seven_day_pct=window("seven_day").get("utilization"),
            seven_day_resets_at=_iso(window("seven_day").get("resets_at")),
            seven_day_opus_pct=window("seven_day_opus").get("utilization"),
            seven_day_sonnet_pct=window("seven_day_sonnet").get("utilization"),
            extra_enabled=bool(extra.get("is_enabled")),
            extra_pct=extra.get("utilization"),
        )
