"""Immutable, typed value objects shared across the core.

These hold *data only* — no I/O, no UI. The parser produces them from raw
JSONL dicts; the aggregator folds them into live state.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class UsageTokens:
    """Token counts from one ``message.usage`` block.

    ``context_tokens`` is the prompt side (what fills the model's context
    window for that turn); ``output`` is the response and is excluded from
    occupancy but included in billing.
    """

    input: int = 0
    cache_creation: int = 0
    cache_read: int = 0
    output: int = 0
    web_search: int = 0
    web_fetch: int = 0
    service_tier: str | None = None

    @property
    def context_tokens(self) -> int:
        """Prompt-side tokens = live context-window occupancy for this turn."""
        return self.input + self.cache_creation + self.cache_read

    @property
    def billable_total(self) -> int:
        return self.input + self.cache_creation + self.cache_read + self.output

    @property
    def cache_hit_ratio(self) -> float:
        """Fraction of prompt tokens served from cache (0..1)."""
        denom = self.input + self.cache_creation + self.cache_read
        return (self.cache_read / denom) if denom else 0.0

    def __add__(self, other: "UsageTokens") -> "UsageTokens":
        if not isinstance(other, UsageTokens):
            return NotImplemented
        return UsageTokens(
            input=self.input + other.input,
            cache_creation=self.cache_creation + other.cache_creation,
            cache_read=self.cache_read + other.cache_read,
            output=self.output + other.output,
            web_search=self.web_search + other.web_search,
            web_fetch=self.web_fetch + other.web_fetch,
            service_tier=other.service_tier or self.service_tier,
        )


EMPTY_USAGE = UsageTokens()


# Tools that act on a concrete file we want to track as "files touched".
FILE_TOOLS = {"Read", "Edit", "Write", "NotebookEdit", "MultiEdit"}


@dataclass(frozen=True)
class ToolUse:
    """A single ``tool_use`` content block."""

    id: str
    name: str
    input: dict = field(default_factory=dict)

    @property
    def target(self) -> str | None:
        """Best-effort label of what the tool acts on (file, command, query)."""
        for key in ("file_path", "path", "notebook_path", "pattern",
                    "command", "url", "description", "query", "prompt"):
            value = self.input.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        return None

    @property
    def file_path(self) -> str | None:
        if self.name not in FILE_TOOLS:
            return None
        for key in ("file_path", "notebook_path", "path"):
            value = self.input.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        return None
