"""The source contract.

A source advances on ``poll()`` and feeds the dashboard. The jsonl source
mutates the aggregator's ``SessionState``; the plan-limits source maintains its
own ``view`` that the engine reads. Phase-2 sources (Prometheus, Tempo)
implement the same shape so the engine and UI don't change.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable


@runtime_checkable
class Source(Protocol):
    def poll(self) -> None:
        """Advance the source: read new data and update wherever it writes."""
        ...
