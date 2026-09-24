"""Pluggable data sources feeding the dashboard.

v1: ``jsonl_source`` (session transcripts, with a per-agent parser and
sidecar discovery), ``plan_limits`` (Claude: OAuth usage endpoint) and
``codex_limits`` (Codex: rate limits from local rollout records, no network).
Phase 2 adds Prometheus + Tempo sources behind the same seam.
"""
