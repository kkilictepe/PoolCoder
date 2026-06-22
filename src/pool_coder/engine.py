"""Orchestrator: owns the sources + aggregator on a reader thread.

The reader thread is the *only* thing that touches the mutable ``SessionState``.
It publishes immutable ``Snapshot``s under a lock; the UI calls
``get_snapshot()`` and never sees partial state. This is the entire core->UI
contract (plus ``pricing``, which is pure).
"""

from __future__ import annotations

import threading
from datetime import datetime, timezone
from pathlib import Path

from .aggregator import Aggregator
from .config import Config, TAIL_INTERVAL
from .paths import SessionInfo
from .pricing import Pricing
from .snapshot import Snapshot, build_session_snapshot
from .sources.jsonl_source import JsonlSource
from .sources.plan_limits import PlanLimitsSource
from .state import SessionState


class Engine:
    def __init__(self, session: SessionInfo, config: Config | None = None,
                 pricing: Pricing | None = None, enable_plan_limits: bool = True):
        self.config = config or Config()
        self.pricing = pricing or Pricing.load()
        self.state = SessionState(
            session_id=session.session_id,
            project_hash=session.project_hash,
            main_path=str(session.main_path),
        )
        self.agg = Aggregator(self.state, self.config)
        self.jsonl = JsonlSource(session.main_path, self.agg)
        self.plan = PlanLimitsSource() if (enable_plan_limits and self.config.plan_limits) else None

        self._lock = threading.Lock()
        self._snapshot: Snapshot | None = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._loading = True

    # -- snapshot access -------------------------------------------------
    def _build(self) -> Snapshot:
        session = build_session_snapshot(self.state, self.config, self.pricing)
        return Snapshot(
            generated_at=datetime.now(timezone.utc),
            session=session,
            plan_limits=self.plan.view if self.plan else None,
            loading=self._loading,
            files_watched=self.jsonl.files_watched,
        )

    def _publish(self) -> None:
        snap = self._build()
        with self._lock:
            self._snapshot = snap

    def get_snapshot(self) -> Snapshot | None:
        with self._lock:
            return self._snapshot

    # -- lifecycle -------------------------------------------------------
    def snapshot_once(self) -> Snapshot:
        """Headless: build full state once and return a single snapshot."""
        self.jsonl.initial_catchup()
        if self.plan:
            self.plan.fetch_once()
        self._loading = False
        self._publish()
        return self.get_snapshot()  # type: ignore[return-value]

    def start(self) -> None:
        self._publish()  # immediate loading snapshot so the UI has something
        if self.plan:
            self.plan.start()
        self._thread = threading.Thread(target=self._run, name="reader", daemon=True)
        self._thread.start()

    def _run(self) -> None:
        try:
            self.jsonl.initial_catchup()
        except Exception:
            pass
        self._loading = False
        self._publish()
        while not self._stop.is_set():
            try:
                self.jsonl.poll()
            except Exception:
                pass
            self._publish()
            self._stop.wait(TAIL_INTERVAL)

    def stop(self) -> None:
        self._stop.set()
        if self.plan:
            self.plan.stop()
        if self._thread is not None:
            self._thread.join(timeout=2.0)
