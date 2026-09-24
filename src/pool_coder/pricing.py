"""Estimate session cost from token usage.

Rates are USD per 1,000,000 tokens. Keys are either a model *family*
(``FAMILY_KEYS``: ``opus``, ``sonnet``, ``haiku``, ``fable``, ``gpt`` and the
``default`` catch-all) or an OpenAI model-id *prefix* such as ``gpt-5.6-sol``.
A gpt-family model takes the longest prefix key matching its id at a ``-``
boundary; every model then falls back to its family key, then ``default``.
Claude models, and every model of a Claude Code session, only ever use the
family keys. Defaults are embedded (single source of truth,
mirrored in the repo's ``pricing.toml``); pass an explicit path to override.
Cost is an estimate — Claude Code mixes 5m/1h cache writes which we price with
one ``cache_write`` rate, and OpenAI rates ignore fast/priority multipliers
(and are only notional on a subscription plan).
"""

from __future__ import annotations

import tomllib
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

from .config import model_family
from .models import UsageTokens

_DEFAULT_TOML = """
[default]
input = 5.0
output = 25.0
cache_write = 6.25
cache_read = 0.5
[opus]
input = 5.0
output = 25.0
cache_write = 6.25
cache_read = 0.5
[sonnet]
input = 3.0
output = 15.0
cache_write = 3.75
cache_read = 0.3
[haiku]
input = 1.0
output = 5.0
cache_write = 1.25
cache_read = 0.1
[fable]
input = 10.0
output = 50.0
cache_write = 12.5
cache_read = 1.0
[gpt]
input = 2.0
output = 10.0
cache_write = 2.5
cache_read = 0.2
["gpt-6-astra"]
input = 10.0
output = 50.0
cache_write = 12.5
cache_read = 1.0
["gpt-6-sol"]
input = 2.0
output = 10.0
cache_write = 2.5
cache_read = 0.2
["gpt-6-luna"]
input = 0.1
output = 0.5
cache_write = 0.125
cache_read = 0.01
["gpt-5.6-sol"]
input = 4.0
output = 20.0
cache_write = 5.0
cache_read = 0.4
["gpt-5.6-terra"]
input = 2.0
output = 12.0
cache_write = 2.5
cache_read = 0.2
["gpt-5.6-luna"]
input = 0.2
output = 1.2
cache_write = 0.25
cache_read = 0.02
["gpt-5.5"]
input = 5.0
output = 30.0
cache_write = 5.0
cache_read = 0.5
["gpt-5.4"]
input = 2.5
output = 15.0
cache_write = 2.5
cache_read = 0.25
["gpt-5.3-codex"]
input = 1.75
output = 14.0
cache_write = 1.75
cache_read = 0.175
["gpt-5.2"]
input = 1.75
output = 14.0
cache_write = 1.75
cache_read = 0.175
["gpt-5.2-codex"]
input = 1.75
output = 14.0
cache_write = 1.75
cache_read = 0.175
["gpt-5"]
input = 1.25
output = 10.0
cache_write = 1.25
cache_read = 0.125
["gpt-5.1"]
input = 1.25
output = 10.0
cache_write = 1.25
cache_read = 0.125
["gpt-5-codex"]
input = 1.25
output = 10.0
cache_write = 1.25
cache_read = 0.125
["gpt-5.1-codex"]
input = 1.25
output = 10.0
cache_write = 1.25
cache_read = 0.125
["gpt-5-mini"]
input = 0.25
output = 2.0
cache_write = 0.25
cache_read = 0.025
["gpt-5.1-codex-mini"]
input = 0.25
output = 2.0
cache_write = 0.25
cache_read = 0.025
["gpt-5-codex-mini"]
input = 0.25
output = 2.0
cache_write = 0.25
cache_read = 0.025
["codex-mini-latest"]
input = 1.5
output = 6.0
cache_write = 1.5
cache_read = 0.375
["o4-mini"]
input = 1.1
output = 4.4
cache_write = 1.1
cache_read = 0.275
["o3"]
input = 2.0
output = 8.0
cache_write = 2.0
cache_read = 0.5
"""

# Keys resolved through ``model_family``; every other key is a model-id prefix.
FAMILY_KEYS = frozenset({"default", "opus", "sonnet", "haiku", "fable", "gpt"})

_PER_TOKEN = 1_000_000.0

_RATE_FIELDS = ("input", "output", "cache_write", "cache_read")


def _rate_tables(data: dict, prefix: str = "") -> Iterator[tuple[str, dict]]:
    """Yield ``(key, table)`` for every rate table, flattening nested tables.

    An unquoted dotted header such as ``[gpt-5.6-sol]`` parses as nested tables
    (``{"gpt-5": {"6-sol": {...}}}``). A table with no rate fields whose values
    are tables is such a path segment: its key path is joined back with ``"."``,
    so the quoted and unquoted spellings give the same ``"gpt-5.6-sol"`` key
    instead of a zero-rate ``"gpt-5"``. A rate table's own sub-tables (e.g.
    ``[gpt-5]`` followed by an unquoted ``[gpt-5.1]``) are flattened the same way.
    """
    for key, table in data.items():
        if not isinstance(table, dict):
            continue
        path = f"{prefix}.{key}" if prefix else key
        children = {k: v for k, v in table.items() if isinstance(v, dict)}
        if not children or any(f in table for f in _RATE_FIELDS):
            yield path, table
        if children:
            yield from _rate_tables(children, path)


@dataclass(frozen=True)
class Rate:
    input: float = 0.0
    output: float = 0.0
    cache_write: float = 0.0
    cache_read: float = 0.0


class Pricing:
    def __init__(self, rates: dict[str, Rate]):
        self.rates = rates

    @classmethod
    def _from_toml_text(cls, text: str) -> "Pricing":
        data = tomllib.loads(text)
        rates = {
            fam: Rate(
                input=float(d.get("input", 0)),
                output=float(d.get("output", 0)),
                cache_write=float(d.get("cache_write", 0)),
                cache_read=float(d.get("cache_read", 0)),
            )
            for fam, d in _rate_tables(data)
        }
        rates.setdefault("default", Rate(5.0, 25.0, 6.25, 0.5))
        # A custom file without gpt entries must not price GPT at Opus rates.
        rates.setdefault("gpt", Rate(2.0, 10.0, 2.5, 0.2))
        return cls(rates)

    @classmethod
    def load(cls, path: str | Path | None = None) -> "Pricing":
        if path is not None:
            try:
                return cls._from_toml_text(Path(path).read_text(encoding="utf-8"))
            except (OSError, tomllib.TOMLDecodeError, ValueError, TypeError):
                pass
        return cls._from_toml_text(_DEFAULT_TOML)

    def rate_for(self, model: str | None, agent: str | None = None) -> Rate:
        """The rate of ``model`` in a session of ``agent`` (``"claude"``: the
        family lookup Claude Code sessions always had)."""
        fam = model_family(model, agent)
        if fam == "gpt":
            # Prefix keys are OpenAI model ids, never applied to Claude ids. The
            # longest at a "-" boundary wins, so "gpt-5.6-sol-2026-09-01" takes
            # "gpt-5.6-sol" while an unknown "gpt-5.7-sol" never takes "gpt-5".
            low = (model or "").lower()
            best: tuple[int, Rate] | None = None
            for key, rate in self.rates.items():
                if key in FAMILY_KEYS:
                    continue
                k = key.lower()
                if (low == k or low.startswith(k + "-")) and (best is None or len(k) > best[0]):
                    best = (len(k), rate)
            if best is not None:
                return best[1]
        return self.rates.get(fam) or self.rates["default"]

    def cost(self, usage: UsageTokens, model: str | None, agent: str | None = None) -> float:
        r = self.rate_for(model, agent)
        return (
            usage.input * r.input
            + usage.output * r.output
            + usage.cache_creation * r.cache_write
            + usage.cache_read * r.cache_read
        ) / _PER_TOKEN
