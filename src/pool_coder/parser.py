"""Turn raw JSONL lines into tolerant, read-only ``Record`` views.

The parser never raises on malformed input — bad lines become ``None`` and are
skipped by the caller. Field access is lazy and defensive so that schema drift
in future Claude Code versions degrades gracefully instead of crashing.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

from .models import ToolUse, UsageTokens


def parse_timestamp(value: object) -> datetime | None:
    """Parse an ISO-8601 timestamp (``...Z``) to a tz-aware UTC datetime."""
    if not isinstance(value, str) or not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def content_preview(content: object, limit: int = 240) -> str:
    """Collapse arbitrary tool_result / message content into a short string."""
    if isinstance(content, str):
        text = content
    elif isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict):
                parts.append(str(block.get("text") or block.get("content") or ""))
            else:
                parts.append(str(block))
        text = " ".join(parts)
    elif content is None:
        text = ""
    else:
        text = str(content)
    text = " ".join(text.split())
    return text[:limit]


class Record:
    """A lazy, defensive view over one decoded JSONL object."""

    __slots__ = ("raw", "type", "timestamp")

    def __init__(self, raw: dict):
        self.raw = raw
        self.type = raw.get("type", "")
        self.timestamp = parse_timestamp(raw.get("timestamp"))

    # -- top-level fields -------------------------------------------------
    @property
    def message(self) -> dict:
        msg = self.raw.get("message")
        return msg if isinstance(msg, dict) else {}

    @property
    def uuid(self) -> str | None:
        return self.raw.get("uuid")

    @property
    def request_id(self) -> str | None:
        return self.raw.get("requestId")

    @property
    def token_key(self) -> str:
        """Stable per-turn key for token folding (requestId, else uuid)."""
        return self.request_id or self.uuid or ""

    @property
    def model(self) -> str | None:
        return self.message.get("model")

    @property
    def stop_reason(self) -> str | None:
        return self.message.get("stop_reason")

    @property
    def git_branch(self) -> str | None:
        return self.raw.get("gitBranch")

    @property
    def cwd(self) -> str | None:
        return self.raw.get("cwd")

    @property
    def version(self) -> str | None:
        return self.raw.get("version")

    @property
    def agent_id(self) -> str | None:
        return self.raw.get("agentId")

    @property
    def is_sidechain(self) -> bool:
        return bool(self.raw.get("isSidechain"))

    # -- message body -----------------------------------------------------
    def usage(self) -> UsageTokens | None:
        usage = self.message.get("usage")
        if not isinstance(usage, dict):
            return None
        server = usage.get("server_tool_use") or {}
        return UsageTokens(
            input=int(usage.get("input_tokens") or 0),
            cache_creation=int(usage.get("cache_creation_input_tokens") or 0),
            cache_read=int(usage.get("cache_read_input_tokens") or 0),
            output=int(usage.get("output_tokens") or 0),
            web_search=int(server.get("web_search_requests") or 0),
            web_fetch=int(server.get("web_fetch_requests") or 0),
            service_tier=usage.get("service_tier"),
        )

    def _blocks(self) -> list:
        content = self.message.get("content")
        return content if isinstance(content, list) else []

    def tool_uses(self) -> list[ToolUse]:
        out = []
        for block in self._blocks():
            if isinstance(block, dict) and block.get("type") == "tool_use":
                out.append(
                    ToolUse(
                        id=block.get("id", ""),
                        name=block.get("name", ""),
                        input=block.get("input") or {},
                    )
                )
        return out

    def tool_results(self) -> list[tuple[str, bool, str]]:
        """List of ``(tool_use_id, is_error, preview)`` for result blocks."""
        out = []
        for block in self._blocks():
            if isinstance(block, dict) and block.get("type") == "tool_result":
                out.append(
                    (
                        block.get("tool_use_id", ""),
                        bool(block.get("is_error")),
                        content_preview(block.get("content")),
                    )
                )
        return out

    def text_len(self) -> int:
        return sum(
            len(b.get("text") or "")
            for b in self._blocks()
            if isinstance(b, dict) and b.get("type") == "text"
        )

    def thinking_len(self) -> int:
        return sum(
            len(b.get("thinking") or "")
            for b in self._blocks()
            if isinstance(b, dict) and b.get("type") == "thinking"
        )

    def assistant_text(self) -> str:
        """Concatenated text blocks of an assistant turn (what Claude 'says')."""
        return " ".join(
            b.get("text") or ""
            for b in self._blocks()
            if isinstance(b, dict) and b.get("type") == "text"
        ).strip()

    def user_text(self) -> str | None:
        """Plain text of a real user prompt (None for tool_result-only msgs)."""
        content = self.message.get("content")
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            parts = [
                b.get("text", "")
                for b in content
                if isinstance(b, dict) and b.get("type") == "text"
            ]
            if parts:
                return " ".join(parts)
        return None


def parse_line(line: str) -> Record | None:
    """Parse one JSONL line into a ``Record`` (``None`` if blank/invalid)."""
    line = line.strip()
    if not line:
        return None
    try:
        raw = json.loads(line)
    except (json.JSONDecodeError, ValueError):
        return None
    if not isinstance(raw, dict):
        return None
    return Record(raw)
