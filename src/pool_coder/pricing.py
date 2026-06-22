"""Estimate session cost from token usage.

Rates are USD per 1,000,000 tokens, keyed by model family. Defaults are
embedded (single source of truth, mirrored in the repo's ``pricing.toml``);
pass an explicit path to override. Cost is an estimate — Claude Code mixes
5m/1h cache writes which we price with one ``cache_write`` rate.
"""

from __future__ import annotations

import tomllib
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
"""

_PER_TOKEN = 1_000_000.0


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
            for fam, d in data.items()
            if isinstance(d, dict)
        }
        rates.setdefault("default", Rate(5.0, 25.0, 6.25, 0.5))
        return cls(rates)

    @classmethod
    def load(cls, path: str | Path | None = None) -> "Pricing":
        if path is not None:
            try:
                return cls._from_toml_text(Path(path).read_text(encoding="utf-8"))
            except (OSError, tomllib.TOMLDecodeError, ValueError):
                pass
        return cls._from_toml_text(_DEFAULT_TOML)

    def rate_for(self, model: str | None) -> Rate:
        return self.rates.get(model_family(model)) or self.rates["default"]

    def cost(self, usage: UsageTokens, model: str | None) -> float:
        r = self.rate_for(model)
        return (
            usage.input * r.input
            + usage.output * r.output
            + usage.cache_creation * r.cache_write
            + usage.cache_read * r.cache_read
        ) / _PER_TOKEN
