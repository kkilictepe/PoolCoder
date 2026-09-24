"""Session-transcript source: discovery + tailers feeding the aggregator.

Owns one discovery object and a list of ``Tailer``s (one per file). On attach
it registers subagent/workflow side-data; on poll it drains each tailer into
the fold target, translating the ``RESET`` sentinel into a source reset.

The defaults (``SessionDiscovery`` + ``parse_line``) are Claude Code's; another
agent passes its own ``discovery=`` and ``parse=`` and reuses the drain loop.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Callable

from ..config import DISCOVERY_INTERVAL
from ..discovery import DiscoveryDelta, SessionDiscovery
from ..parser import parse_line
from ..tailer import RESET, Tailer
from .base import Discoverer, FoldTarget


class JsonlSource:
    def __init__(self, main_path: str | Path, aggregator: FoldTarget,
                 discovery_interval: float = DISCOVERY_INTERVAL, *,
                 discovery: Discoverer | None = None,
                 parse: Callable[[str], object] = parse_line):
        self.main_path = Path(main_path)
        self.agg = aggregator
        self.discovery = discovery if discovery is not None else SessionDiscovery(self.main_path)
        self.parse = parse  # line -> record (None skips the line)
        self.tailers: list[tuple[str, Tailer]] = []
        self.discovery_interval = discovery_interval
        self._last_discovery = 0.0

    @property
    def files_watched(self) -> int:
        return len(self.tailers)

    def _attach(self, delta: DiscoveryDelta) -> None:
        for reg in delta.subagents:
            self.agg.register_subagent(
                reg.agent_id, reg.agent_type, reg.description, reg.parent_tool_use_id
            )
        for reg in delta.workflows:
            self.agg.register_workflow(reg.run_id, reg.name, reg.description, reg.phases)
        for spec in delta.new_tailers:
            self.tailers.append((spec.source_id, Tailer(spec.path)))

    def _drain_once(self) -> bool:
        progressed = False
        for source_id, tailer in list(self.tailers):
            out = tailer.poll()
            if out:
                progressed = True
            for item in out:
                if item is RESET:
                    self.agg.reset_source(source_id)
                else:
                    rec = self.parse(item)
                    if rec is not None:
                        self.agg.apply(source_id, rec)
            if tailer.has_backlog:
                progressed = True
        return progressed

    def _drain_until_quiet(self, max_rounds: int = 10_000) -> None:
        rounds = 0
        while self._drain_once() and rounds < max_rounds:
            rounds += 1

    def initial_catchup(self) -> None:
        """Parse every known file from the start to build full state."""
        self._attach(self.discovery.initial())
        self._drain_until_quiet()
        # one more discovery pass to catch sidecars created during catch-up
        self._attach(self.discovery.scan())
        self._drain_until_quiet()
        self._last_discovery = time.monotonic()

    def poll(self, now: float | None = None) -> None:
        now = now if now is not None else time.monotonic()
        if now - self._last_discovery >= self.discovery_interval:
            self._attach(self.discovery.scan())
            self._last_discovery = now
        self._drain_once()
