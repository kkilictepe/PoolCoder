"""The source contract.

A source advances on ``poll()`` and feeds the dashboard. The jsonl source
mutates the aggregator's ``SessionState``; the plan-limits source maintains its
own ``view`` that the engine reads. Phase-2 sources (Prometheus, Tempo)
implement the same shape so the engine and UI don't change.

``FoldTarget`` is what the jsonl source folds records into: the Claude
``Aggregator`` and the Codex ``CodexAggregator`` both satisfy it, so the same
drain loop (including ``RESET`` handling) serves every agent.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol, runtime_checkable

if TYPE_CHECKING:  # pragma: no cover - typing only
    from ..discovery import DiscoveryDelta


@runtime_checkable
class Source(Protocol):
    def poll(self) -> None:
        """Advance the source: read new data and update wherever it writes."""
        ...


@runtime_checkable
class FoldTarget(Protocol):
    """Folds parsed records (and discovery side-data) into live state."""

    def apply(self, source_id: str, record) -> None:
        """Fold one parsed record from ``source_id`` (``"main"``, ``"agent:<id>"``, ...)."""
        ...

    def reset_source(self, source_id: str) -> None:
        """Forget what ``source_id`` contributed; a full replay follows."""
        ...

    def register_subagent(self, agent_id: str, agent_type: str, description: str,
                          parent_tool_use_id: str) -> None:
        ...

    def register_workflow(self, run_id: str, name: str, description: str,
                          phases: list[tuple[str, str]]) -> None:
        ...


@runtime_checkable
class Discoverer(Protocol):
    """Finds a session's files: ``initial()`` once, then ``scan()`` for new ones."""

    def initial(self) -> DiscoveryDelta:
        ...

    def scan(self) -> DiscoveryDelta:
        ...
